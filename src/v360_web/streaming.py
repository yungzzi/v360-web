"""One owned camera/encoder pipeline shared by bounded browser subscribers."""

from __future__ import annotations

import os
import queue
import secrets
import select
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as wire
from .client import CameraClient
from .config import Settings
from .controls import CameraControls
from .errors import CameraError
from .media import find_tool

MAX_BOX = 2 * 1024**2
MAX_VIEWERS = 3


def camera_frames(config: Path, stop: threading.Event, controls=None):
    settings = Settings.load(config)
    client = CameraClient(settings)
    client.bridge.timeout = min(client.bridge.timeout, 10)
    with client as camera:
        previous = None
        keep = time.monotonic()
        try:
            camera.send(
                wire.LIVE_START, wire.live_payload(2, 0 if settings.shared else settings.user_id)
            )
            if controls:
                controls.attach(camera)
            while not stop.is_set():
                deadline = time.monotonic() + 5
                header = wire.parse_live_header(
                    camera.bridge.read_exact(camera.session, settings.live_channel, 32, deadline)
                )
                if (
                    header.flag not in (wire.LIVE_FIRST, wire.LIVE_VIDEO, wire.LIVE_TARGET)
                    or header.codec != 5
                    or header.subchannel != 0
                    or header.frame_type not in (1, 2, 3, 17, 18, 19)
                ):
                    raise CameraError("live_format", "Unsupported live video format.")
                if previous is None:
                    if header.flag != wire.LIVE_FIRST or header.frame_type not in (1, 17):
                        raise CameraError("live_start", "Live stream did not start at a key frame.")
                elif header.sequence != (previous + 1) & 0xFFFFFFFF:
                    raise CameraError(
                        "live_sequence", "Live video frame sequence is discontinuous."
                    )
                data = camera.bridge.read_exact(
                    camera.session, settings.live_channel, header.size, deadline
                )
                if not data.startswith((b"\0\0\1", b"\0\0\0\1")):
                    raise CameraError("live_format", "Live video is not Annex B.")
                previous = header.sequence
                if time.monotonic() - keep >= 5:
                    camera.send(wire.KEEP_ALIVE, struct.pack("<I", 10))
                    keep = time.monotonic()
                yield data
        finally:
            if controls:
                controls.close()
            try:
                camera.send(wire.LIVE_STOP, bytes(4))
            except (CameraError, OSError):
                pass


def encoder_command():
    executable = find_tool("ffmpeg")
    if not executable:
        raise CameraError("ffmpeg_missing", "FFmpeg is required for browser playback.")
    return [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-threads",
        "1",
        "-probesize",
        "65536",
        "-analyzeduration",
        "100000",
        "-r",
        "15",
        "-f",
        "hevc",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "baseline",
        "-level:v",
        "3.0",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-keyint_min",
        "15",
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-b:v",
        "384k",
        "-maxrate",
        "512k",
        "-bufsize",
        "1024k",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets",
        "1",
        "-f",
        "mp4",
        "pipe:1",
    ]


class FragmentReader:
    """Parse complete bounded MP4 boxes; each published fragment begins at a GOP."""

    def __init__(self, reader):
        self.reader = reader

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.reader(size - len(data))
            if not part:
                raise CameraError("encoder_eof", "Video encoder stopped.")
            data.extend(part)
        return bytes(data)

    def events(self):
        initialization = b""
        pending = None
        ready = False
        while True:
            header = self.exact(8)
            size, kind = struct.unpack(">I4s", header)
            if not 8 <= size <= MAX_BOX:
                raise CameraError("encoder_frame", "Invalid video encoder box size.")
            box = header + self.exact(size - 8)
            if not ready:
                if kind not in (b"ftyp", b"moov") or len(initialization) + size > MAX_BOX:
                    raise CameraError("encoder_frame", "Invalid video initialization.")
                initialization += box
                if kind == b"moov":
                    at = initialization.find(b"avcC")
                    if at < 0 or len(initialization) < at + 8:
                        raise CameraError("encoder_codec", "Missing AVC configuration.")
                    codec = initialization[at + 5 : at + 8].hex()
                    ready = True
                    yield "init", (f'video/mp4; codecs="avc1.{codec}"', initialization)
            elif kind == b"moof":
                if pending is not None:
                    raise CameraError("encoder_frame", "Incomplete media fragment.")
                pending = box
            elif kind == b"mdat":
                if pending is None or len(pending) + size > MAX_BOX:
                    raise CameraError("encoder_frame", "Invalid media fragment.")
                yield "fragment", pending + box
                pending = None
            elif kind != b"mfra":
                raise CameraError("encoder_frame", "Unexpected encoder output.")


@dataclass(eq=False)
class Subscriber:
    identity: str = field(default_factory=lambda: secrets.token_hex(16))
    messages: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=4))
    closed: threading.Event = field(default_factory=threading.Event)


class StreamManager:
    def __init__(
        self, config: Path | None, camera_lock, source_factory=camera_frames, before_subscribe=None
    ):
        self.config = config
        self.camera_lock = camera_lock
        self.source_factory = source_factory
        self.guard = threading.Lock()
        self.viewers = set()
        self.thread = None
        self.stop = threading.Event()
        self.initialization = None
        self.state = "stopped"
        self.error = None
        self.frames = self.fragments = 0
        self.encoder_pid = None
        self.shutdown_requested = False
        self.controls = CameraControls()
        self.before_subscribe = before_subscribe

    def snapshot(self):
        with self.guard:
            return {
                "state": self.state,
                "viewers": len(self.viewers),
                "error": self.error,
                "frames": self.frames,
                "fragments": self.fragments,
                "audio": False,
                "session_limit_seconds": 1800,
                "control_error": self.controls.error,
                "ptz_active": bool(self.controls.sent),
            }

    def subscribe(self):
        if self.before_subscribe:
            self.before_subscribe()
        with self.guard:
            if self.shutdown_requested or self.config is None:
                raise CameraError("stream_unavailable", "Live service is not configured.")
            if len(self.viewers) >= MAX_VIEWERS:
                raise CameraError("viewer_limit", "Live viewer limit reached.")
            active = self.thread is not None and self.thread.is_alive()
            if active and self.stop.is_set():
                raise CameraError("camera_busy", "Previous live session is stopping.")
            if not active:
                if not self.camera_lock.acquire(blocking=False):
                    raise CameraError("camera_busy", "A camera operation is already running.")
                self.stop.clear()
                self.initialization = None
                self.state, self.error = "starting", None
                self.frames = self.fragments = 0
            viewer = Subscriber()
            self.viewers.add(viewer)
            if self.initialization:
                viewer.messages.put_nowait(("init", self.initialization))
            if not active:
                self.thread = threading.Thread(target=self._run, name="v360-live", daemon=True)
                self.thread.start()
            return viewer

    def unsubscribe(self, viewer):
        with self.guard:
            self.controls.release(viewer.identity)
            self.viewers.discard(viewer)
            viewer.closed.set()
            if not self.viewers:
                self.stop.set()

    def move(self, identity, direction):
        with self.guard:
            if not any(v.identity == identity and not v.closed.is_set() for v in self.viewers):
                raise CameraError("viewer_missing", "Start watching before moving the camera.")
            self.controls.request_move(identity, direction)

    def publish(self, event, value):
        with self.guard:
            if event == "init":
                self.initialization = value
                self.state = "live"
            else:
                self.fragments += 1
            for viewer in list(self.viewers):
                try:
                    viewer.messages.put_nowait((event, value))
                except queue.Full:
                    viewer.closed.set()
                    self.viewers.remove(viewer)
            if not self.viewers:
                self.stop.set()

    def _read_output(self, process):
        def read(size):
            deadline = time.monotonic() + 15
            while not self.stop.is_set():
                if select.select([process.stdout], [], [], 0.2)[0]:
                    return os.read(process.stdout.fileno(), size)
                if time.monotonic() >= deadline:
                    raise CameraError("encoder_timeout", "Video encoder produced no data.")
            return b""

        try:
            for event, value in FragmentReader(read).events():
                self.publish(event, value)
        except (CameraError, OSError) as error:
            if not self.stop.is_set():
                self.error = getattr(error, "code", "encoder_io")
                self.stop.set()

    def _write(self, process, data):
        offset = 0
        deadline = time.monotonic() + 5
        while offset < len(data) and not self.stop.is_set():
            if time.monotonic() >= deadline:
                raise CameraError("encoder_timeout", "Video encoder input stalled.")
            if select.select([], [process.stdin], [], 0.2)[1]:
                try:
                    offset += os.write(process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue

    def _run(self):
        process = output = source = None
        started = time.monotonic()
        try:
            source = (
                camera_frames(self.config, self.stop, self.controls)
                if self.source_factory is camera_frames
                else self.source_factory(self.config, self.stop)
            )
            for data in source:
                if self.stop.is_set():
                    break
                if time.monotonic() - started >= 1800:
                    self.error = "session_limit"
                    break
                if process is None:
                    process = subprocess.Popen(
                        encoder_command(),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    self.encoder_pid = process.pid
                    os.set_blocking(process.stdin.fileno(), False)
                    output = threading.Thread(
                        target=self._read_output, args=(process,), daemon=True
                    )
                    output.start()
                self._write(process, data)
                self.frames += 1
        except CameraError as error:
            if not self.stop.is_set():
                self.error = error.code
        except Exception:  # noqa: BLE001 -- never expose private SDK/configuration data
            if not self.stop.is_set():
                self.error = "stream_failed"
        finally:
            self.stop.set()
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001 -- finish encoder and lock cleanup too
                    self.error = self.error or "camera_cleanup"
            if process:
                process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                if output:
                    output.join(timeout=2)
                process.stdout.close()
                self.encoder_pid = None
            with self.guard:
                self.state = "failed" if self.error else "stopped"
                for viewer in self.viewers:
                    viewer.closed.set()
                self.viewers.clear()
                self.initialization = None
            self.camera_lock.release()

    def close(self):
        with self.guard:
            self.shutdown_requested = True
            self.stop.set()
            thread = self.thread
        if thread:
            thread.join(timeout=50)
