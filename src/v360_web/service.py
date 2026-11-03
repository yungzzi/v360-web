import hashlib
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from .client import CameraClient
from .config import Settings
from .diagnostics import record
from .errors import CameraError
from .maintenance import cleanup
from .media import probe, validate_decode
from .media_recovery import playable_source
from .progress import report
from .storage import admit, check_free
from .store import RecordingStore


def discover(config_path: Path, channel: int, start: datetime, end: datetime, prime_calendar=False):
    settings = Settings.load(config_path)
    report("querying")
    with CameraClient(settings) as camera:
        calendar = {}
        if prime_calendar:
            from .camera_probe import monthly_index

            calendar = monthly_index(camera, start.year, start.month, channel)
        events = camera.list_recordings(channel, start, end)
    store = RecordingStore(settings.data_root)
    keys = store.discover(settings.did, channel, events)
    diagnostics = camera.last_list_diagnostics
    store.save_catalog(settings.did, start, end, diagnostics)
    return {
        **calendar,
        "records": len(keys),
        "ids": keys,
        "camera_validation": (
            "list_received_with_invalid_records"
            if diagnostics["invalid_records"]
            else "list_received"
        ),
        **diagnostics,
    }


def download(config_path: Path, key: str, *, local_only=False, camera=None, defer_validation=False):
    settings = Settings.load(config_path)
    store = RecordingStore(settings.data_root)
    item = store.get(key)
    if not item or item["device_id"] != settings.did:
        raise CameraError(
            "recording_missing", "Recording is not indexed for the configured camera."
        )
    if item["state"] == "verified" and store.media_path(key):
        return {"id": key, "state": "verified", "already_present": True}
    if item["state"] == "downloaded_unverified" and item["path"]:
        local = (store.root / item["path"]).resolve()
        if (
            local.is_relative_to(store.root)
            and local.is_file()
            and local.stat().st_size == item["bytes"]
        ):
            digest = hashlib.sha256()
            with local.open("rb") as file:
                for chunk in iter(lambda: file.read(1024**2), b""):
                    digest.update(chunk)
            if digest.hexdigest() == item["sha256"]:
                report("validating")
                try:
                    with playable_source(local, item, store.root, probe(local)) as (usable, _, _):
                        validate_decode(usable)
                except CameraError:
                    store.update(key, "failed", error="local_media_invalid")
                    if local_only:
                        raise CameraError(
                            "local_media_invalid",
                            "Local original requires a fresh device transfer.",
                        ) from None
                else:
                    store.update(key, "verified", error=None)
                    return {"id": key, "state": "verified", "rechecked_local_file": True}
    if local_only:
        raise CameraError(
            "local_original_invalid",
            "Local original cannot be validated without fetching it again.",
        )
    temporary = store.root / "media" / f"{key}.part"
    last_report = 0

    def progress(written, total):
        nonlocal last_report
        now = time.monotonic()
        if not written or written == total or now - last_report >= 0.5:
            report("downloading", bytes=written, total=total)
            if not written:
                cleanup(
                    store.root,
                    max(0, total - (temporary.stat().st_size if temporary.exists() else 0)),
                    protected=key,
                )
                admit(store.root, total, "archive", replacing=temporary)
            else:
                check_free(store.root, total - written)
            last_report = now

    report("connecting")
    if item.get("sha256"):
        record(store.root, "refetch_started", key=key, source_sha256=item["sha256"])
    store.update(key, "downloading", error=None)
    try:
        with nullcontext(camera) if camera is not None else CameraClient(settings) as peer:
            result = peer.download(
                item["channel"],
                datetime.fromisoformat(item["start_local"]),
                temporary,
                item["weekday"],
                progress=progress,
            )
        # Release the battery camera before CPU/media validation.
        destination = temporary.with_suffix(".media")
        temporary.replace(destination)
        store.update(
            key,
            "downloaded_unverified",
            path=str(destination.relative_to(store.root)),
            **{k: result[k] for k in ["bytes", "sha256"]},
        )
        record(store.root, "downloaded", key=key, ok=True)
        if defer_validation:
            return {"id": key, "state": "downloaded_unverified", **result}
        info = probe(destination)
        report("validating")
        with playable_source(destination, store.get(key), store.root, info) as (usable, info, _):
            validate_decode(usable)
        store.update(key, "verified", error=None)
        return {
            "id": key,
            "state": "verified",
            **result,
            "formats": info.get("format", {}).get("format_name"),
        }
    except CameraError as error:
        state = (
            "downloaded_unverified"
            if store.get(key)["state"] == "downloaded_unverified"
            else "failed"
        )
        store.update(key, state, error=error.code)
        raise
    except OSError as error:
        store.update(key, "failed", error="local_storage")
        raise CameraError(
            "local_storage", "Could not write or finalize the local recording."
        ) from error


class DownloadBatch:
    """Own one short camera session; published raw files validate independently."""

    def __init__(self, config):
        self.config = config
        self.camera = None
        self.count = 0
        self.started = 0

    def close(self):
        if self.camera is not None:
            self.camera.close()
            self.camera = None
        self.count = 0

    def download(self, key):
        settings = Settings.load(self.config)
        try:
            if self.camera is not None and (
                self.count >= 3 or time.monotonic() - self.started >= 120
            ):
                self.close()
            if self.camera is None:
                self.camera = CameraClient(settings)
                self.camera.__enter__()
                self.started = time.monotonic()
            result = download(self.config, key, camera=self.camera, defer_validation=True)
            self.count += 1
            if self.count >= 3:
                self.close()
            return result
        except BaseException as error:
            self.close()
            if isinstance(error, CameraError):
                store = RecordingStore(settings.data_root)
                item = store.get(key)
                if item and item["state"] != "downloaded_unverified":
                    store.update(key, "failed", error=error.code)
            raise
