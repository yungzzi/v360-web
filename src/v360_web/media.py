from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .errors import CameraError


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
