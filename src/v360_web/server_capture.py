"""Server-side fallback recording from the shared live output.

The recorder is a passive consumer of ``StreamManager.publish``. It never opens a
second camera session and never starts a second encoder: the browser and the
recorder receive the same init segment and the same complete MP4 fragments.
Each delivered file is written as a temporary file, decoded once, and only then
renamed and indexed as a verified recording.
"""

from __future__ import annotations

import hashlib
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import maintenance, storage
from .config import Settings
from .errors import CameraError
from .media import probe, validate_decode
from .store import CAPTURE_SOURCES, SCHEDULED_SOURCE, WATCH_SOURCE, RecordingStore

ZONE = ZoneInfo("Asia/Hong_Kong")
SEGMENT_SECONDS = 60
RESERVATION = 8 * 1024**2
CAPTURE_QUEUE_SIZE = 8


def now_local():
    return datetime.now(ZONE).replace(tzinfo=None)


def next_slot(now: datetime):
    """Return the next local-time sampling boundary and its duration."""
    if 6 <= now.hour < 18:
        interval = 10
        base = now.replace(minute=0, second=0, microsecond=0)
    else:
        interval = 30
        base = (
            now.replace(hour=18, minute=0, second=0, microsecond=0)
            if now.hour >= 18
            else (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        )
    elapsed = (now - base).total_seconds()
    steps = int(elapsed // (interval * 60)) + 1
    due = base + timedelta(minutes=steps * interval)
    return due, 30 if 6 <= due.hour < 18 else 15


class CaptureSink:
    """One shared-live recording session, split at complete fragment boundaries."""

    def __init__(self, manager, source, did, duration=None):
        self.manager = manager
        self.source = source
        self.did = did
        self.duration = duration
        self.root = manager.root
        self.created = now_local()
        self.started_at = None
        self.started_mono = None
        self.deadline = None
        self.segment_start = None
        self.last_at = None
        self.handoff_at = None
        self.handoff_source = None
        self.init = None
        self.audio = False
        self.path = None
        self.file = None
        self.digest = None
        self.written = 0
        self.fragments = 0
        self.stopping = False
        self.stop_requested = None
        self.done = False
        self.error = None
        self.owner = None
        self.guard = threading.RLock()

    @property
    def source_now(self):
        if self.handoff_source and self.fragments == 0:
            return self.handoff_source
        return self.source

    def note_video(self, at, mono):
        with self.guard:
            self._note_video(at, mono)

    def _note_video(self, at, mono):
        if self.done or self.started_at is not None:
            if self.started_at is not None and not (
                self.handoff_source and self.source == SCHEDULED_SOURCE
            ):
                self.last_at = at
            return
        self.started_at = self.handoff_at or at
        self.started_mono = mono
        self.segment_start = self.started_at
        self.last_at = at
        if self.source == SCHEDULED_SOURCE and self.duration is not None:
            self.deadline = mono + self.duration

    def handoff(self, source, at, mono):
        with self.guard:
            self._handoff(source, at, mono)

    def _handoff(self, source, at, mono):
        if self.done or source not in CAPTURE_SOURCES or source == self.source:
            return
        self.handoff_source = source
        self.handoff_at = at
        self.deadline = None
        if self.fragments == 0:
            self.source = source
            self.started_at = at
            self.started_mono = mono
            self.segment_start = at
        self.manager.note_handoff(source)

    def request_stop(self):
        with self.guard:
            self._request_stop()

    def _request_stop(self):
        if not self.done and not self.stopping:
            self.stopping = True
            self.stop_requested = time.monotonic()

    def publish(self, event, value, at, mono):
        with self.guard:
            self._publish(event, value, at, mono)
            finished = self.done
        if finished:
            self.manager.note_done(self.owner or self)

    def _publish(self, event, value, at, mono):
        if self.done:
            return False
        try:
            if event == "init":
                mime, data = value
                self.audio = "mp4a.40.2" in mime
                self.init = data
                return
            if event != "fragment":
                return
            if self.init is None:
                return
            if self.started_at is None:
                self._note_video(at, mono)
            if self.handoff_source and self.source == SCHEDULED_SOURCE and self.fragments:
                # The receiving fragment is the first complete GOP after handoff.
                self._rotate(at, self.handoff_source, self.handoff_at)
            elif self.segment_start and (at - self.segment_start).total_seconds() >= SEGMENT_SECONDS:
                self._rotate(at, self.source_now, at)
            if self.path is None:
                self._open(at)
            data = value
            if self.written + len(data) > RESERVATION:
                raise CameraError("capture_size", "Live segment exceeded its storage reservation.")
            self.file.write(data)
            self.digest.update(data)
            self.written += len(data)
            self.fragments += 1
            storage.check_free(self.root, max(0, RESERVATION - self.written))
            finished = self.stopping or (
                self.deadline is not None and mono >= self.deadline and self.fragments
            )
            if finished:
                self._finalize()
                self.done = True
                return True
        except CameraError as error:
            self._fail(error.code)
        except OSError:
            self._fail("capture_io")
        return False

    def _open(self, at):
        self.manager.prepare(RESERVATION, protected=getattr(self, "key", None))
        self.key = secrets.token_hex(16)
        directory = self.root / "captures"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / (self.key + ".part")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.path = temporary
        self.file = os.fdopen(fd, "wb")
        self.file.write(self.init)
        self.digest = hashlib.sha256()
        self.digest.update(self.init)
        self.written = len(self.init)
        self.fragments = 0
        self.segment_start = self.segment_start or at

    def _rotate(self, at, source, start):
        if self.path is None:
            self.source = source
            self.segment_start = start
            self.handoff_source = None
            return
        self._finalize()
        self.source = source
        self.handoff_source = None
        self.segment_start = start
        self.last_at = start

    def _finalize(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None
        if self.path is None:
            return
        temporary = self.path
        self.path = None
        if not self.fragments:
            temporary.unlink(missing_ok=True)
            return
        try:
            info = probe(temporary)
            streams = info.get("streams", [])
            if not any(stream.get("codec_type") == "video" for stream in streams):
                raise CameraError("capture_unrecognized", "Captured MP4 has no video stream.")
            if self.audio and not any(
                stream.get("codec_type") == "audio" for stream in streams
            ):
                raise CameraError("capture_audio", "Captured MP4 lost its audio stream.")
            validate_decode(temporary)
        except (CameraError, OSError):
            temporary.unlink(missing_ok=True)
            raise
        target = temporary.with_suffix(".mp4")
        os.replace(temporary, target)
        target.chmod(0o600)
        self.path = target
        duration = max(
            1,
            round(
                (
                    (self.last_at or self.segment_start or now_local()) - self.segment_start
                ).total_seconds()
            ),
        )
        self.manager.register(
            self.key,
            self.source,
            (self.segment_start or self.created).isoformat(),
            duration,
            "captures/" + target.name,
            target.stat().st_size,
            self.digest.hexdigest(),
        )
        self.path = None
        self.key = None
        self.written = 0
        self.fragments = 0
        self.digest = None

    def close(self, reason=None, wait=True):
        with self.guard:
            self._close(reason)
            finished = self.done
        if finished:
            self.manager.note_done(self.owner or self)

    def _close(self, reason=None):
        if self.done:
            return False
        if self.handoff_source and self.source == SCHEDULED_SOURCE and self.fragments:
            self._rotate(now_local(), self.handoff_source, self.handoff_at)
        try:
            if self.path is not None:
                self._finalize()
        except CameraError as error:
            self._fail(error.code)
        except OSError:
            self._fail("capture_io")
        self.done = True
        return True

    def abort(self, code):
        with self.guard:
            self._fail(code)
        self.manager.note_done(self.owner or self)

    def _fail(self, code):
        if self.path is not None:
            if self.file is not None:
                try:
                    self.file.close()
                except OSError:
                    pass
                self.file = None
            self.path.unlink(missing_ok=True)
            self.path = None
        self.error = code
        self.done = True
        self.manager.pause(code)


class AsyncCaptureSink:
    """Bounded hand-off from live output threads to the recording writer."""

    def __init__(self, manager, source, did, duration=None):
        self.writer = CaptureSink(manager, source, did, duration=duration)
        self.writer.owner = self
        self.manager = manager
        self.queue = queue.Queue(maxsize=CAPTURE_QUEUE_SIZE)
        self.finished = threading.Event()
        self.overflow = False
        self.flushes = []
        self.worker = threading.Thread(target=self._run, name="v360-capture-writer", daemon=True)
        self.worker.start()

    @property
    def source(self):
        return self.writer.source

    @property
    def source_now(self):
        return self.writer.source_now

    @property
    def handoff_source(self):
        return self.writer.handoff_source

    @property
    def fragments(self):
        return self.writer.fragments

    @property
    def error(self):
        return self.writer.error

    @property
    def done(self):
        return self.writer.done or self.finished.is_set()

    def note_video(self, at, mono):
        self._put(("video", at, mono))

    def publish(self, event, value, at, mono):
        self._put(("event", event, value, at, mono))

    def handoff(self, source, at, mono):
        self._put(("handoff", source, at, mono))

    def request_stop(self):
        self._put(("stop",))

    def abort(self, code):
        self._put(("abort", code))

    def flush(self, timeout=15):
        event = threading.Event()
        self.flushes.append(event)
        if not self._put(("flush", event), control=True):
            event.set()
            return
        event.wait(timeout)

    def close(self, reason=None, wait=True):
        self._put(("close", reason), control=True)
        if wait:
            self.worker.join(timeout=60)

    def _put(self, item, *, control=False):
        if self.done:
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            # Never stall live delivery: drop capture work and stop this session.
            self.overflow = True
            if control:
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(item)
                    return True
                except (queue.Empty, queue.Full):
                    pass
            return False

    def _run(self):
        try:
            while True:
                try:
                    item = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if self.overflow:
                        self.writer.abort("capture_backlog")
                        break
                    continue
                command = item[0]
                if self.overflow:
                    self.writer.abort("capture_backlog")
                    break
                if command == "close":
                    self.writer.close(item[1] if len(item) > 1 else None)
                    break
                if command == "abort":
                    self.writer.abort(item[1])
                    break
                if command == "flush":
                    item[1].set()
                    continue
                if command == "event":
                    self.writer.publish(item[1], item[2], item[3], item[4])
                elif command == "video":
                    self.writer.note_video(item[1], item[2])
                elif command == "handoff":
                    self.writer.handoff(item[1], item[2], item[3])
                elif command == "stop":
                    self.writer.request_stop()
                if self.writer.done:
                    break
        finally:
            for event in self.flushes:
                event.set()
            if not self.writer.done:
                self.writer.abort("capture_backlog" if self.overflow else "capture_closed")
            self.finished.set()


class CaptureManager:
    """Own storage admission, scheduling, and status for fallback recording."""

    def __init__(self, root, config_path, *, enabled=False, yield_callback=None):
        self.root = root.resolve()
        self.config_path = config_path
        self.enabled = bool(enabled and config_path is not None)
        self.yield_callback = yield_callback
        self.guard = threading.RLock()
        self.streams = None
        self.thread = None
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.paused = not self.enabled
        self.error = "disabled" if not self.enabled else None
        self.did = None
        self.next_due, self.next_duration = next_slot(now_local())
        self.last_source = None
        self.last_finished = None
        self.active = None
        self.sources = {WATCH_SOURCE: 0, SCHEDULED_SOURCE: 0}
        self.recover()

    @property
    def recording(self):
        return self.active is not None and not self.active.done

    @property
    def recording_source(self):
        sink = self.active
        if sink is None or sink.done:
            return None
        return sink.handoff_source or sink.source

    def attach(self, streams):
        self.streams = streams

    def recover(self):
        directory = self.root / "captures"
        if not directory.is_dir():
            return
        for path in directory.glob("*.part"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def available(self):
        if not self.enabled:
            return False
        if self.paused:
            try:
                maintenance.cleanup_captures(self.root, RESERVATION)
            except CameraError as error:
                self.error = error.code
                return False
            with self.guard:
                self.paused = False
                self.error = None
        if self.did is None:
            try:
                self.did = Settings.load(self.config_path).did
            except (CameraError, OSError, ValueError):
                self.error = "device_not_configured"
                return False
        return True

    def prepare(self, additional, protected=None):
        maintenance.cleanup_captures(self.root, additional, protected=protected)
        storage.admit(self.root, additional, "capture")

    def new_watch(self, at=None):
        if not self.available():
            return None
        with self.guard:
            if self.active is not None and not self.active.done:
                return None
            sink = AsyncCaptureSink(self, WATCH_SOURCE, self.did)
            self.active = sink
            return sink

    def new_scheduled(self, duration, at=None):
        if not self.available():
            return None
        with self.guard:
            if self.active is not None and not self.active.done:
                return None
            sink = AsyncCaptureSink(self, SCHEDULED_SOURCE, self.did, duration=duration)
            self.active = sink
            return sink

    def register(self, key, source, start, duration, path, size, sha256):
        RecordingStore(self.root).add_capture(
            self.did or "unknown", key, source, start, duration, path, size, sha256
        )
        with self.guard:
            if source in self.sources:
                self.sources[source] += 1
            self.last_source = source
            self.last_finished = time.time()

    def pause(self, code):
        with self.guard:
            self.paused = True
            self.error = code

    def note_handoff(self, source):
        with self.guard:
            self.last_source = source

    def note_done(self, sink):
        with self.guard:
            if self.active is sink:
                self.active = None
            if sink.error:
                self.error = sink.error
        if self.streams is not None:
            self.streams.recording_done(sink)

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        self.next_due, self.next_duration = next_slot(now_local())
        self.paused = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name="v360-server-capture", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopping.wait(1):
            now = now_local()
            if now < self.next_due:
                continue
            due_duration = self.next_duration
            self.next_due, self.next_duration = next_slot(now)
            if not self.available():
                continue
            if self.streams is None:
                continue
            snapshot = self.streams.snapshot()
            if snapshot["viewers"] or snapshot["state"] not in ("stopped", "failed"):
                continue
            if self.yield_callback:
                self.yield_callback()
            try:
                self.streams.start_capture(due_duration, SCHEDULED_SOURCE)
            except CameraError:
                # A busy camera or a failed connection only skips this slot.
                continue

    def snapshot(self):
        with self.guard:
            return {
                "enabled": self.enabled,
                "state": "paused" if self.paused else ("running" if self.thread else "waiting"),
                "paused": self.paused,
                "error": self.error,
                "next_run": self.next_due.isoformat(),
                "last_source": self.last_source,
                "last_finished": self.last_finished,
                "segments": dict(self.sources),
                "bytes": storage.usage(self.root / "captures"),
                "limit": storage.CAPTURE_LIMIT,
                "scheduler_started": bool(self.thread and self.thread.is_alive()),
            }

    def close(self):
        self.stopping.set()
        self.wake.set()
        thread = self.thread
        if thread:
            thread.join(timeout=3)
        self.thread = None
