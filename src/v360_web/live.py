"""Bounded video-only live capture; independent of SD recording downloads."""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
import time
import uuid
from pathlib import Path

from . import protocol as wire
from .client import CameraClient
from .config import Settings, read_private_json, write_private_json
from .errors import CameraError
from .media import make_live_preview, probe, validate_decode

MAX_CAPTURE_BYTES = 64 * 1024**2


def validate_options(seconds: float, resolution: int):
    if not math.isfinite(seconds) or not 1 <= seconds <= 60:
        raise CameraError(
            "live_duration", "Live capture duration must be between 1 and 60 seconds."
        )
    wire.live_payload(resolution, 0)


def capture_video(camera: CameraClient, target: Path, seconds: float, resolution: int = 2):
    """Capture verified-order Annex B HEVC packets, always requesting stop on exit.

    This device reports codec=5 for HEVC while its frame types are 1/2.
    Do not infer H.264 from those frame types. Other codecs are unsupported here.
    """
    validate_options(seconds, resolution)
    user = 0 if camera.settings.shared else camera.settings.user_id
    payload = wire.live_payload(resolution, user)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation prevents accidental overwriting, before waking the video stream.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    frames = written = 0
    previous = first_pts = last_pts = None
    result = {"stop_requested": False}
    with os.fdopen(fd, "wb") as output:
        started = time.monotonic()
        end = started + seconds
        keep = started
        try:
            camera.send(wire.LIVE_START, payload)
            while time.monotonic() < end:
                # Complete the current packet, with at most two seconds of stop slack.
                deadline = min(end + 2, time.monotonic() + 5)
                raw = camera.bridge.read_exact(
                    camera.session, camera.settings.live_channel, 32, deadline
                )
                header = wire.parse_live_header(raw)
                if header.flag == wire.LIVE_END:
                    raise CameraError(
                        "live_ended", "Device ended the stream before capture finished."
                    )
                if written + header.size > MAX_CAPTURE_BYTES or frames >= 100000:
                    raise CameraError("live_size", "Live capture exceeded its storage limit.")
                data = camera.bridge.read_exact(
                    camera.session, camera.settings.live_channel, header.size, deadline
                )
                if (
                    header.flag == wire.LIVE_AUDIO
                    or header.frame_type not in (1, 2, 3, 17, 18, 19)
                    or header.codec != 5
                    or header.subchannel != 0
                ):
                    raise CameraError(
                        "live_format", "This live codec or subchannel is not supported."
                    )
                if not data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
                    raise CameraError("live_format", "Live video does not use Annex B framing.")
                if previous is None:
                    if header.flag != wire.LIVE_FIRST or header.frame_type not in (1, 17):
                        raise CameraError("live_start", "Live stream did not start at a key frame.")
                    first_pts = header.pts_raw
                elif header.sequence != (previous + 1) & 0xFFFFFFFF:
                    raise CameraError(
                        "live_sequence", "Live video frame sequence is discontinuous."
                    )
                previous, last_pts = header.sequence, header.pts_raw
                output.write(data)
                written += len(data)
                frames += 1
                if time.monotonic() - keep >= 5:
                    camera.send(wire.KEEP_ALIVE, struct.pack("<I", 10))
                    keep = time.monotonic()
            if not frames:
                raise CameraError("live_empty", "Device did not return video frames.")
            output.flush()
            os.fsync(output.fileno())
        finally:
            try:
                camera.send(wire.LIVE_STOP, bytes(4))
                result["stop_requested"] = True
            except (CameraError, OSError):
                # Session ownership is still released by CameraClient's context manager.
                pass
    return {
        **result,
        "frames": frames,
        "bytes": written,
        "codec": "hevc",
        "resolution_option": resolution,
        "pts_first_raw": first_pts,
        "pts_last_raw": last_pts,
        "capture_elapsed_seconds": round(time.monotonic() - started, 3),
    }


def capture(config_path: Path, seconds: float = 10, resolution: int = 2):
    validate_options(seconds, resolution)
    settings = Settings.load(config_path)
    key = uuid.uuid4().hex
    directory = settings.data_root / "live" / key
    directory.mkdir(parents=True, mode=0o700)
    raw = directory / "video.hevc"
    try:
        with CameraClient(settings) as camera:
            summary = capture_video(camera, raw, seconds, resolution)
        summary["sdk_cleanup"] = getattr(camera.bridge, "last_cleanup", None)
        # Release the battery camera before media validation/transcoding.
        info = probe(raw)
        if info["streams"][0].get("codec_name") != "hevc":
            raise CameraError("live_codec", "Captured stream did not decode as HEVC.")
        validate_decode(raw)
        target = directory / "preview.mp4"
        conversion = make_live_preview(raw, target, info)
        preview = probe(target)
        validate_decode(target)
        digest = hashlib.sha256()
        with target.open("rb") as file:
            for chunk in iter(lambda: file.read(1024**2), b""):
                digest.update(chunk)
        summary.update(
            id=key,
            state="verified",
            source="live",
            audio=False,
            transport=settings.bridge_kind,
            preview="preview.mp4",
            preview_sha256=digest.hexdigest(),
            preview_seconds=float(preview["format"]["duration"]),
            **conversion,
        )
        write_private_json(directory / "capture.json", summary)
        return summary
    except CameraError as error:
        write_private_json(directory / "failure.json", {"state": "failed", "error": error.code})
        raise


def preview_path(root: Path, key: str):
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        return None
    directory = root / "live" / key
    target, manifest = directory / "preview.mp4", directory / "capture.json"
    base = root.resolve()
    if not target.resolve().is_relative_to(base) or not manifest.resolve().is_relative_to(base):
        return None
    try:
        info = read_private_json(manifest)
        if info.get("state") == "verified" and target.is_file():
            return target
    except CameraError:
        pass
    return None
