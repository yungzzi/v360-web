"""Admission limits after owned-file retention has run."""

import shutil
from pathlib import Path

from .backup_policy import BackupPolicy
from .errors import CameraError

GIB = 1024**3
ARCHIVE_LIMIT = 15 * GIB
SERVER_TOTAL_LIMIT = 15 * GIB
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
    limit = (
        min(ARCHIVE_LIMIT, BackupPolicy(root).policy()["archive_limit"])
        if kind == "archive"
        else CACHE_LIMIT
    )
    if BackupPolicy(root).policy()["mode"] == "server":
        combined = usage(root / "media") + usage(root / "previews") + usage(root / "live")
        if combined - previous + additional > SERVER_TOTAL_LIMIT:
            raise CameraError("storage_limit", "Combined recording budget is full.")
        if kind == "cache":
            limit = SERVER_TOTAL_LIMIT
    if used - previous + additional > limit:
        raise CameraError("storage_limit", "Storage budget is full after safe retention checks.")
    if shutil.disk_usage(root).free - max(0, additional - previous) < FREE_RESERVE:
        raise CameraError("disk_reserve", "Insufficient free disk space above the system reserve.")


def summary(root):
    return {
        "archive_bytes": usage(root / "media"),
        "archive_limit": min(ARCHIVE_LIMIT, BackupPolicy(root).policy()["archive_limit"]),
        "cache_bytes": usage(root / "previews") + usage(root / "live"),
        "cache_limit": SERVER_TOTAL_LIMIT
        if BackupPolicy(root).policy()["mode"] == "server"
        else CACHE_LIMIT,
        "combined_bytes": usage(root / "media") + usage(root / "previews") + usage(root / "live"),
        "combined_limit": SERVER_TOTAL_LIMIT
        if BackupPolicy(root).policy()["mode"] == "server"
        else None,
        "free_bytes": shutil.disk_usage(root).free,
        "free_reserve": FREE_RESERVE,
        "automatic_cleanup": True,
    }
