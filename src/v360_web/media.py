from __future__ import annotations

import json
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

from .errors import CameraError
from .progress import cancel_event, check_cancelled


def find_tool(name: str):
    found = shutil.which(name)
    local = Path(__file__).resolve().parents[2] / ".tools/bin" / name
    return found or (str(local) if local.is_file() else None)


def probe(path: Path):
    executable = find_tool("ffprobe")
    if executable is None:
        raise CameraError("ffprobe_missing", "ffprobe is required to validate downloaded media.")
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            check=False,
            timeout=30,
        )
        value = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise CameraError("media_probe", "Media validation did not complete.") from exc
    if result.returncode or not any(
        s.get("codec_type") == "video" for s in value.get("streams", [])
    ):
        raise CameraError("media_unrecognized", "Downloaded bytes are not recognized as video.")
    return value


def validate_decode(path: Path):
    executable = find_tool("ffmpeg")
    if executable is None:
        raise CameraError("ffmpeg_missing", "ffmpeg is required for an independent decode check.")
    check_cancelled()
    if cancel_event.get() is not None:
        process = subprocess.Popen(
            [
                executable,
                "-v",
                "error",
                "-xerror",
                "-threads",
                "1",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 120
            while process.poll() is None:
                check_cancelled()
                if time.monotonic() > deadline:
                    raise CameraError("media_decode", "Decode check timed out.")
                time.sleep(0.2)
            if process.returncode:
                raise CameraError("media_decode", "Video did not pass decode validation.")
            return
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CameraError(
            "media_decode", "Decode check failed or exceeded its time limit."
        ) from exc
    if result.returncode:
        raise CameraError("media_decode", "Video did not pass an independent decode check.")


def make_live_preview(source: Path, target: Path, info: dict):
    """Time raw HEVC using its bitstream frame rate; wire PTS are coarse device time."""
    executable = find_tool("ffmpeg")
    if executable is None:
        raise CameraError("ffmpeg_missing", "ffmpeg is required for the live preview.")
    try:
        video = next(s for s in info["streams"] if s.get("codec_type") == "video")
        rate = Fraction(video["r_frame_rate"])
        if not 1 <= rate <= 120:
            raise ValueError
    except (KeyError, StopIteration, ValueError, ZeroDivisionError) as exc:
        raise CameraError(
            "live_timing", "Live bitstream does not declare a supported frame rate."
        ) from exc
    try:
        result = subprocess.run(
            [
                executable,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-n",
                "-r",
                str(rate),
                "-f",
                "hevc",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CameraError("live_preview", "Live preview conversion failed or timed out.") from exc
    if result.returncode:
        raise CameraError("live_preview", "Live preview conversion failed.")
    target.chmod(0o600)
    return {"preview_frame_rate": str(rate), "preview_timing": "bitstream_frame_rate"}
