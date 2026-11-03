"""Owned-file retention. Foreign files, pinned recordings and media leases are preserved."""

import re
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi.responses import FileResponse

from . import storage
from .backup_policy import BackupPolicy
from .errors import CameraError
from .store import RecordingStore

GUARD = threading.RLock()
ACTIVE_GUARD = threading.Lock()
ACTIVE = {}


def active(root, key):
    with ACTIVE_GUARD:
        return ACTIVE.get((str(root.resolve()), key), 0) > 0


class HeldFileResponse(FileResponse):
    def __init__(self, path, *, root, key, **kwargs):
        self.owner = (str(root.resolve()), key)
        super().__init__(path, **kwargs)

    async def __call__(self, scope, receive, send):
        with ACTIVE_GUARD:
            ACTIVE[self.owner] = ACTIVE.get(self.owner, 0) + 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            with ACTIVE_GUARD:
                ACTIVE[self.owner] -= 1
                if not ACTIVE[self.owner]:
                    del ACTIVE[self.owner]


def owned(root, folder, key, suffix):
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        return None
    base = root / folder
    path = base / (key + suffix)
    if base.is_symlink() or path.is_symlink():
        return None
    if path.resolve().parent != base.resolve() or not path.resolve().is_relative_to(root.resolve()):
        return None
    return path


def protect(root, key):
    with GUARD:
        store = RecordingStore(root)
        if store.get(key):
            store.lease(key)


def pin(root, key, value):
    with GUARD:
        return RecordingStore(root).pin(key, value)


def cleanup(root, additional=0, kind="archive", protected=None):
    with GUARD:
        config = BackupPolicy(root)
        policy = config.policy()
        store = RecordingStore(root)
        if policy.get("requires_profile"):
            raise CameraError("storage_profile", "Choose a storage profile before cleanup.")
        rows = sorted(store.list(), key=lambda r: r["saved_at"] or r["updated_at"])
        if policy["mode"] == "server":
            return cleanup_combined(root, store, rows, policy, additional, protected)
        limit = min(storage.ARCHIVE_LIMIT, policy["archive_limit"])
        if additional > (limit if kind == "archive" else storage.CACHE_LIMIT):
            raise CameraError("storage_limit", "Incoming file exceeds the storage budget.")
        used = storage.usage(root / "media")
        if config.read("policy") is None and used > policy["archive_limit"]:
            raise CameraError(
                "storage_limit", "Choose a storage profile before reducing an existing archive."
            )
        extra = additional if kind == "cache" else 0
        cache_used = storage.usage(root / "previews") + storage.usage(root / "live")
        cache_pressure = cache_used + extra >= int(storage.CACHE_LIMIT * 0.9)
        cache_target = max(0, int(storage.CACHE_LIMIT * 0.8) - extra)
        removed_cache = 0
        # Regenerable previews are reclaimed before any original recording.
        for row in rows:
            low_disk = storage.shutil.disk_usage(root).free - additional < storage.FREE_RESERVE
            if not low_disk and not (cache_pressure and cache_used > cache_target):
                break
            key = row["id"]
            if key == protected or store.leased(key) or active(root, key):
                continue
            for suffix in preview_suffixes(root, key):
                path = owned(root, "previews", key, suffix)
                if path and path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    cache_used -= size
                    removed_cache += size
        if kind == "cache":
            storage.admit(root, additional, "cache")
            return {"removed_recordings": 0, "removed_cache_bytes": removed_cache}
        eligible = []
        for row in rows:
            key = row["id"]
            path = owned(root, "media", key, ".media")
            if (
                key == protected
                or row["pinned"]
                or store.leased(key)
                or active(root, key)
                or row["state"] != "verified"
                or not row["saved_at"]
                or path is None
                or not row["path"]
                or (root / row["path"]).resolve() != path.resolve()
            ):
                continue
            eligible.append((row, path, path.stat().st_size if path.is_file() else 0))
        required = max(
            0,
            used + additional - limit,
            storage.FREE_RESERVE + additional - storage.shutil.disk_usage(root).free,
        )
        if required > sum(size for _, _, size in eligible):
            raise CameraError(
                "storage_limit", "Pinned, active or unmanaged files prevent safe cleanup."
            )
        cutoff = (datetime.now(UTC) - timedelta(days=policy["retention_days"])).isoformat()
        pressure = used + additional >= int(limit * 0.9)
        target = max(0, int(limit * 0.8) - additional)
        removed = []
        for row, path, size in eligible:
            expired = row["saved_at"] < cutoff
            low_disk = storage.shutil.disk_usage(root).free - additional < storage.FREE_RESERVE
            if not expired and not low_disk and not (pressure and used > target):
                continue
            path.unlink(missing_ok=True)
            used -= size
            store.update(row["id"], "evicted", path=None, error=None)
            removed.append(row["id"])
            for suffix in preview_suffixes(root, row["id"]):
                preview = owned(root, "previews", row["id"], suffix)
                if preview:
                    preview.unlink(missing_ok=True)
        with store.connection() as db:
            db.execute("DELETE FROM media_leases WHERE until < ?", (time.time(),))
        return {"removed_recordings": len(removed), "removed_cache_bytes": removed_cache}


def preview_suffixes(root, key, windows=None):
    """Only owned, bounded window names share the parent recording lease/quota."""
    yield ".mp4"
    yield ".json"
    yield ".repair.hevc"
    yield ".repair.alaw"
    yield ".repair.mp4"
    candidates = (root / "previews").glob(key + ".s*") if windows is None else windows.get(key, ())
    for path in candidates:
        suffix = path.name[len(key) :]
        if re.fullmatch(r"\.s[0-9]{4}\.(mp4|json|partial\.mp4)", suffix):
            yield suffix


def cleanup_combined(root, store, rows, policy, additional, protected):
    """Server originals and web files are one retained unit under one total budget."""
    rows = sorted(rows, key=lambda row: row["start_local"])
    limit = storage.SERVER_TOTAL_LIMIT
    if additional > limit:
        raise CameraError("storage_limit", "Incoming file exceeds combined budget.")
    used = sum(storage.usage(root / folder) for folder in ("media", "previews", "live"))
    required = max(
        0,
        used + additional - limit,
        storage.FREE_RESERVE + additional - storage.shutil.disk_usage(root).free,
    )
    cutoff = (datetime.now(UTC) - timedelta(days=policy["retention_days"])).isoformat()
    if required <= 0 and not any(
        row["state"] == "verified" and row["saved_at"] and row["saved_at"] < cutoff
        for row in rows
    ):
        storage.check_free(root, additional)
        return {"removed_recordings": 0, "removed_cache_bytes": 0}
    # Enumerate window files once, rather than scan the entire folder per record.
    windows = {}
    for path in (root / "previews").glob("*.s*"):
        if re.fullmatch(r"[a-f0-9]{32}\.s[0-9]{4}\.(mp4|json|partial\.mp4)", path.name):
            windows.setdefault(path.name[:32], []).append(path)
    eligible = []
    for row in rows:
        key = row["id"]
        original = owned(root, "media", key, ".media")
        if (
            key == protected
            or row["pinned"]
            or store.leased(key)
            or active(root, key)
            or row["state"] != "verified"
            or not row["saved_at"]
            or original is None
            or not row["path"]
            or (root / row["path"]).resolve() != original.resolve()
        ):
            continue
        files = [original]
        files += [
            p
            for suffix in preview_suffixes(root, key, windows)
            if (p := owned(root, "previews", key, suffix)) is not None
        ]
        sizes = [(p, p.stat().st_size) for p in files if p.is_file()]
        eligible.append((row, sizes, sum(size for _, size in sizes)))
    if required > sum(size for _, _, size in eligible):
        raise CameraError("storage_limit", "Protected or unmanaged files prevent safe cleanup.")
    removed, cache_bytes = [], 0
    for row, files, size in eligible:
        if required <= 0 and row["saved_at"] >= cutoff:
            continue
        for path, count in files:
            path.unlink(missing_ok=True)
            if path.parent.name == "previews":
                cache_bytes += count
        store.update(row["id"], "evicted", path=None, error=None)
        removed.append(row["id"])
        required -= size
    storage.check_free(root, additional)
    return {"removed_recordings": len(removed), "removed_cache_bytes": cache_bytes}
