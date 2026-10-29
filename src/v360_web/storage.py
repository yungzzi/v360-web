"""Admission limits for recording downloads and playback cache. No deletions."""

import shutil
from pathlib import Path

from .errors import CameraError

GIB = 1024**3
ARCHIVE_LIMIT = 10 * GIB
CACHE_LIMIT = GIB
FREE_RESERVE = 8 * GIB


def usage(directory: Path):
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            # A different request may atomically finalize a partial file.
            continue
    return total


def check_free(root, remaining=0):
    if shutil.disk_usage(root).free - max(0, remaining) < FREE_RESERVE:
        raise CameraError("disk_reserve", "Insufficient free disk space above the system reserve.")


def admit(root: Path, additional: int, kind: str, replacing: Path | None = None):
    if additional < 0:
        raise ValueError("Negative storage reservation")
    previous = replacing.stat().st_size if replacing and replacing.is_file() else 0
    used = (
        usage(root / "media")
        if kind == "archive"
        else (usage(root / "previews") + usage(root / "live"))
    )
    limit = ARCHIVE_LIMIT if kind == "archive" else CACHE_LIMIT
    if used - previous + additional > limit:
        raise CameraError("storage_limit", "Storage budget is full; no files were deleted.")
    if shutil.disk_usage(root).free - max(0, additional - previous) < FREE_RESERVE:
        raise CameraError("disk_reserve", "Insufficient free disk space above the system reserve.")


def summary(root):
    return {
        "archive_bytes": usage(root / "media"),
        "archive_limit": ARCHIVE_LIMIT,
        "cache_bytes": usage(root / "previews") + usage(root / "live"),
        "cache_limit": CACHE_LIMIT,
        "free_bytes": shutil.disk_usage(root).free,
        "free_reserve": FREE_RESERVE,
        "automatic_cleanup": False,
    }
