import hashlib
import time
from datetime import datetime
from pathlib import Path

from .client import CameraClient
from .config import Settings
from .errors import CameraError
from .media import probe, validate_decode
from .progress import report
from .storage import admit, check_free
from .store import RecordingStore


def discover(config_path: Path, channel: int, start: datetime, end: datetime):
    settings = Settings.load(config_path)
    with CameraClient(settings) as camera:
        events = camera.list_recordings(channel, start, end)
    store = RecordingStore(settings.data_root)
    keys = store.discover(settings.did, channel, events)
    diagnostics = camera.last_list_diagnostics
    store.save_catalog(settings.did, start, end, diagnostics)
    return {
        "records": len(keys),
        "ids": keys,
        "camera_validation": (
            "list_received_with_invalid_records"
            if diagnostics["invalid_records"]
            else "list_received"
        ),
        **diagnostics,
    }


def download(config_path: Path, key: str):
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
                probe(local)
                validate_decode(local)
                store.update(key, "verified", error=None)
                return {"id": key, "state": "verified", "rechecked_local_file": True}
    temporary = store.root / "media" / f"{key}.part"
    last_report = 0

    def progress(written, total):
        nonlocal last_report
        now = time.monotonic()
        if not written or written == total or now - last_report >= 0.5:
            if not written:
                admit(store.root, total, "archive", replacing=temporary)
            else:
                check_free(store.root, total - written)
            report("downloading", bytes=written, total=total)
            last_report = now

    report("connecting")
    store.update(key, "downloading", error=None)
    try:
        with CameraClient(settings) as camera:
            result = camera.download(
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
        info = probe(destination)
        report("validating")
        validate_decode(destination)
        store.update(key, "verified")
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
