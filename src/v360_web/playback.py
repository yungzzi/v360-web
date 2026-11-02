"""Bounded browser-compatible previews generated from verified SD recordings."""

import math
import os
import re
import subprocess
import threading
import time
from functools import wraps
from pathlib import Path

from .config import Settings, read_private_json, write_private_json
from .errors import CameraError
from .maintenance import cleanup, protect
from .media import find_tool, probe, validate_decode
from .progress import check_cancelled, report
from .storage import admit, check_free
from .store import RecordingStore


def paths(root, key, start=None):
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise CameraError("recording_missing", "Recording identifier is invalid.")
    folder = root / "previews"
    name = key if start is None else key + f".s{start:04d}"
    targets = [folder / (name + suffix) for suffix in (".mp4", ".json", ".partial.mp4")]
    if any(not p.resolve().is_relative_to(root.resolve()) for p in targets):
        raise CameraError("preview_path", "Preview path is outside its private directory.")
    return targets


def ready(root: Path, key: str, start=None):
    try:
        target, manifest, _ = paths(root, key, start)
        item = RecordingStore(root).get(key)
        if not item or not RecordingStore(root).media_path(key):
            return None
        info = read_private_json(manifest)
        if (
            info.get("verified")
            and info.get("source_sha256") == item["sha256"]
            and target.is_file()
            and target.stat().st_size == info.get("bytes")
        ):
            return target
    except (CameraError, OSError):
        pass
    return None


ENCODER_LOCK = threading.Lock()


def serial_encoder(function):
    @wraps(function)
    def serialized(*args, **kwargs):
        while not ENCODER_LOCK.acquire(timeout=0.2):
            check_cancelled()
        try:
            check_cancelled()
            return function(*args, **kwargs)
        finally:
            ENCODER_LOCK.release()

    return serialized


@serial_encoder
def prepare(config: Path, key: str, start=None):
    if start is not None and (type(start) is not int or not 0 <= start < 3600 or start % 30):
        raise CameraError("preview_start", "Invalid playback window.")
    settings = Settings.load(config)
    root = settings.data_root
    store = RecordingStore(root)
    item = store.get(key)
    source = store.media_path(key)
    if not source or not item or item["device_id"] != settings.did:
        raise CameraError("recording_missing", "Download and verify the recording first.")
    if ready(root, key):
        return {"id": key, "state": "ready", "already_present": True, "full": True, "start": 0}
    if start is not None and ready(root, key, start):
        _, manifest, _ = paths(root, key, start)
        return {"id": key, "state": "ready", **read_private_json(manifest), "full": False}
    report("preparing_playback")
    protect(root, key)
    info = probe(source)
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise CameraError("preview_duration", "Recording duration is unsupported for preview.")
    source_duration = duration
    if start is not None:
        if start >= source_duration:
            raise CameraError("preview_start", "Playback window is outside recording.")
        duration = min(30.0, source_duration - start)
    reservation = math.ceil(duration * 200000 * 1.3) + 1024**2
    target, manifest, temporary = paths(root, key, start)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cleanup(
        root,
        max(0, reservation - (temporary.stat().st_size if temporary.exists() else 0)),
        kind="cache",
        protected=key,
    )
    admit(root, reservation, "cache", replacing=temporary)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    scale = min(1, 1280 / int(video["width"]), 720 / int(video["height"]))
    width = max(2, int(int(video["width"]) * scale) // 2 * 2)
    height = max(2, int(int(video["height"]) * scale) // 2 * 2)
    executable = find_tool("ffmpeg")
    if not executable:
        raise CameraError("ffmpeg_missing", "FFmpeg is required to prepare playback.")
    command = [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-y",
        "-threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        f"scale={width}:{height}",
        "-filter_threads",
        "1",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        "1000k",
        "-maxrate",
        "1500k",
        "-bufsize",
        "2000k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-stats_period",
        "1",
        str(temporary),
    ]
    if start is not None:
        position = command.index("-i")
        command[position:position] = ["-ss", str(start)]
        command[-1:-1] = ["-t", str(duration)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    os.set_blocking(process.stdout.fileno(), False)
    started = time.monotonic()
    deadline = started + 120
    progress_buffer = b""
    processed = 0.0
    next_report = started
    try:
        while process.poll() is None:
            check_cancelled()
            try:
                progress_buffer += os.read(process.stdout.fileno(), 65536)
            except BlockingIOError:
                pass
            lines = progress_buffer.split(b"\n")
            progress_buffer = lines.pop()[-1024:]
            for line in lines:
                if line.startswith(b"out_time="):
                    processed = parse_progress_time(
                        line[9:].decode("ascii", errors="ignore"), processed
                    )
            now = time.monotonic()
            if now >= next_report:
                report(
                    "preparing_playback",
                    seconds=min(duration, processed),
                    total_seconds=duration,
                    elapsed=round(now - started, 1),
                )
                next_report = now + 1
            if time.monotonic() > deadline:
                raise CameraError(
                    "preview_timeout", "Playback preparation exceeded its time limit."
                )
            if temporary.exists() and temporary.stat().st_size > reservation:
                raise CameraError("storage_limit", "Playback exceeded its storage reservation.")
            written = temporary.stat().st_size if temporary.exists() else 0
            check_free(root, reservation - written)
            time.sleep(0.2)
        if process.returncode:
            raise CameraError("preview_failed", "Could not prepare browser playback.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdout.close()
    conversion_seconds = time.monotonic() - started
    if temporary.stat().st_size > reservation:
        raise CameraError("storage_limit", "Playback exceeded its storage reservation.")
    report("validating_playback", elapsed=round(conversion_seconds, 1))
    validation_started = time.monotonic()
    result = probe(temporary)
    if abs(float(result["format"]["duration"]) - duration) > 0.25:
        raise CameraError("preview_truncated", "Playback duration differs from the recording.")
    validate_decode(temporary)
    temporary.chmod(0o600)
    temporary.replace(target)
    temporary_manifest = manifest.with_suffix(".new")
    temporary_manifest.unlink(missing_ok=True)
    write_private_json(
        temporary_manifest,
        {
            "verified": True,
            "source_sha256": item["sha256"],
            "bytes": target.stat().st_size,
            "start": start or 0,
            "seconds": duration,
            "source_seconds": source_duration,
        },
    )
    temporary_manifest.replace(manifest)
    return {
        "id": key,
        "state": "ready",
        "seconds": duration,
        "start": start or 0,
        "source_seconds": source_duration,
        "full": start is None,
        "conversion_seconds": round(conversion_seconds, 3),
        "validation_seconds": round(time.monotonic() - validation_started, 3),
        "audio": any(s["codec_type"] == "audio" for s in result["streams"]),
    }


def parse_progress_time(value, previous=0.0):
    """Read FFmpeg's out_time field without allowing malformed progress to fail a job."""
    try:
        hour, minute, second = (float(part) for part in value.strip().split(":"))
        result = hour * 3600 + minute * 60 + second
        return max(previous, result) if math.isfinite(result) and result >= 0 else previous
    except (ValueError, TypeError):
        return previous
