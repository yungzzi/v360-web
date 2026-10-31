"""Bounded browser-compatible previews generated from verified SD recordings."""

import math
import re
import subprocess
import time
from pathlib import Path

from .config import Settings, read_private_json, write_private_json
from .errors import CameraError
from .maintenance import cleanup, protect
from .media import find_tool, probe, validate_decode
from .progress import check_cancelled, report
from .storage import admit, check_free
from .store import RecordingStore


def paths(root, key):
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise CameraError("recording_missing", "Recording identifier is invalid.")
    folder = root / "previews"
    targets = [folder / (key + suffix) for suffix in (".mp4", ".json", ".partial.mp4")]
    if any(not p.resolve().is_relative_to(root.resolve()) for p in targets):
        raise CameraError("preview_path", "Preview path is outside its private directory.")
    return targets


def ready(root: Path, key: str):
    try:
        target, manifest, _ = paths(root, key)
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


def prepare(config: Path, key: str):
    settings = Settings.load(config)
    root = settings.data_root
    store = RecordingStore(root)
    item = store.get(key)
    source = store.media_path(key)
    if not source or not item or item["device_id"] != settings.did:
        raise CameraError("recording_missing", "Download and verify the recording first.")
    if ready(root, key):
        return {"id": key, "state": "ready", "already_present": True}
    report("preparing_playback")
    protect(root, key)
    info = probe(source)
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise CameraError("preview_duration", "Recording duration is unsupported for preview.")
    reservation = math.ceil(duration * 200000 * 1.3) + 1024**2
    target, manifest, temporary = paths(root, key)
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
        str(temporary),
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 120
    try:
        while process.poll() is None:
            check_cancelled()
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
    if temporary.stat().st_size > reservation:
        raise CameraError("storage_limit", "Playback exceeded its storage reservation.")
    report("validating_playback")
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
        {"verified": True, "source_sha256": item["sha256"], "bytes": target.stat().st_size},
    )
    temporary_manifest.replace(manifest)
    return {
        "id": key,
        "state": "ready",
        "seconds": duration,
        "audio": any(s["codec_type"] == "audio" for s in result["streams"]),
    }
