import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from v360_web import maintenance, progress, service, storage
from v360_web.api import create_app
from v360_web.backup import BackupManager
from v360_web.backup_policy import BackupPolicy
from v360_web.errors import CameraError
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore

TOKEN = "automatic-backup-test-token-1234567890123456"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(autouse=True)
def free_disk(monkeypatch):
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))


def record(root, minutes=0, size=30, state="verified"):
    store = RecordingStore(root)
    start = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None) - timedelta(
        minutes=minutes + 5
    )
    key = store.discover("test-device", 0, [RecordingEvent(start, 0, 2, 1, 1)])[0]
    path = root / "media" / (key + ".media")
    path.parent.mkdir(exist_ok=True)
    if state == "verified":
        path.write_bytes(b"x" * size)
    store.update(key, state, path="media/" + path.name, bytes=size)
    return key, path


def policy(root, **values):
    db = BackupPolicy(root)
    db.write("policy", {**db.policy(), **values})
    return db


def test_rotation_preserves_pin_lease_and_foreign_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "ARCHIVE_LIMIT", 100)
    old, old_path = record(tmp_path, 30)
    pinned, pinned_path = record(tmp_path, 20)
    leased, leased_path = record(tmp_path, 10)
    store = RecordingStore(tmp_path)
    store.pin(pinned, True)
    store.lease(leased)
    foreign = tmp_path / "media" / "foreign.bin"
    foreign.write_bytes(b"keep")
    policy(tmp_path)
    assert maintenance.cleanup(tmp_path)["removed_recordings"] == 1
    assert not old_path.exists() and store.get(old)["state"] == "evicted"
    assert pinned_path.exists() and leased_path.exists() and foreign.read_bytes() == b"keep"


def test_impossible_or_oversize_admission_deletes_no_original(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "ARCHIVE_LIMIT", 100)
    key, path = record(tmp_path, size=80)
    RecordingStore(tmp_path).pin(key, True)
    for additional in (30, 101):
        with pytest.raises(CameraError):
            maintenance.cleanup(tmp_path, additional)
        assert path.exists()


def test_retention_age_and_active_file(tmp_path):
    key, path = record(tmp_path)
    store = RecordingStore(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE recordings SET saved_at='2000-01-01T00:00:00+00:00' WHERE id=?", (key,))
    owner = (str(tmp_path.resolve()), key)
    maintenance.ACTIVE[owner] = 1
    try:
        assert maintenance.cleanup(tmp_path)["removed_recordings"] == 0
        assert path.exists()
    finally:
        del maintenance.ACTIVE[owner]
    assert maintenance.cleanup(tmp_path)["removed_recordings"] == 1
    assert not path.exists()


def test_symlink_never_deleted(tmp_path):
    key, path = record(tmp_path)
    outside = tmp_path / "outside"
    path.replace(outside)
    path.symlink_to(outside)
    with RecordingStore(tmp_path).connection() as db:
        db.execute("UPDATE recordings SET saved_at='2000-01-01' WHERE id=?", (key,))
    assert maintenance.cleanup(tmp_path)["removed_recordings"] == 0
    assert outside.exists() and path.is_symlink()


def test_cache_pressure_never_deletes_original(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CACHE_LIMIT", 10)
    key, path = record(tmp_path)
    preview = tmp_path / "previews" / (key + ".mp4")
    preview.parent.mkdir()
    preview.write_bytes(b"0123456789")
    assert maintenance.cleanup(tmp_path, kind="cache")["removed_cache_bytes"] == 10
    assert path.exists() and not preview.exists()


def manager(root, monkeypatch):
    monkeypatch.setattr(
        "v360_web.backup.Settings.load", lambda _: SimpleNamespace(did="test-device")
    )
    monkeypatch.setattr(
        service, "discover", lambda *args: {"metadata_complete": True, "records": 5}
    )
    return BackupManager(root, root / "not-read.json", threading.Lock())


def test_automatic_cycle_bounds_dedup_and_persistence(tmp_path, monkeypatch):
    keys = [record(tmp_path, i, state="discovered")[0] for i in range(5)]
    saved, _ = record(tmp_path, 10)
    evicted, _ = record(tmp_path, 11, state="evicted")
    calls = []

    def download(config, key):
        progress.report("downloading", bytes=0, total=100)
        calls.append(key)
        RecordingStore(tmp_path).update(key, "verified")

    monkeypatch.setattr(service, "download", download)
    backup = manager(tmp_path, monkeypatch)
    backup.cycle()
    assert calls == keys[:2]
    assert saved not in calls and evicted not in calls
    assert backup.snapshot()["last_saved"] == 2
    assert backup.db.read("daily")["bytes"] == 200
    backup.cycle()
    assert calls == keys[:4]
    restarted = manager(tmp_path, monkeypatch)
    assert restarted.snapshot()["last_saved"] == 2
    assert restarted.db.read("daily")["bytes"] == 400


def test_camera_busy_paused_and_retry_backoff(tmp_path, monkeypatch):
    key, _ = record(tmp_path, state="discovered")
    backup = manager(tmp_path, monkeypatch)

    def fail(*args):
        raise CameraError("read_timeout", "test")

    monkeypatch.setattr(service, "download", fail)
    backup.camera_lock.acquire()
    backup.cycle()
    backup.camera_lock.release()
    assert backup.snapshot()["state"] == "waiting_for_camera"
    backup.cycle()
    assert not backup.retry_ready(key, time.time())
    assert backup.retry_ready(key, time.time() + 3601)
    policy(tmp_path, enabled=False)
    previous = backup.snapshot()["last_started"]
    backup.cycle()
    assert backup.snapshot()["last_started"] == previous


def test_budget_and_cooperative_yield(tmp_path, monkeypatch):
    _key, _ = record(tmp_path, state="discovered")
    backup = manager(tmp_path, monkeypatch)

    def too_large(config, key):
        progress.report("downloading", bytes=0, total=9 * 1024**2)
        pytest.fail("Oversize file must be rejected before transferring")

    monkeypatch.setattr(service, "download", too_large)
    backup.cycle()
    assert backup.snapshot()["last_saved"] == 0
    assert backup.db.read("daily", {"bytes": 0})["bytes"] == 0

    def yield_now(config, key):
        backup.request_yield()
        progress.check_cancelled()

    with backup.db.connection() as db:
        db.execute("DELETE FROM backup_retries")
    monkeypatch.setattr(service, "download", yield_now)
    backup.cycle()
    assert backup.snapshot()["error"] == "backup_yielded"
    assert not backup.running and backup.camera_lock.acquire(blocking=False)
    backup.camera_lock.release()


def test_auth_failure_remains_paused_across_restart(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)

    def fail(*args):
        raise CameraError("device_authentication", "test")

    monkeypatch.setattr(service, "discover", fail)
    backup.cycle()
    assert manager(tmp_path, monkeypatch).snapshot()["state"] == "authentication_required"


def test_backup_api_requires_auth_and_persists_policy(tmp_path, monkeypatch):
    key, _ = record(tmp_path)
    app = create_app(tmp_path, TOKEN)
    monkeypatch.setattr(app.state.backup, "start", lambda: None)
    with TestClient(app) as client:
        assert client.get("/api/backup").status_code == 401
        assert client.put("/api/backup", json={}).status_code == 401
        body = {"enabled": False, "mode": "server", "interval_minutes": 60}
        value = client.put("/api/backup", headers=HEADERS, json=body).json()
        assert value["policy"]["archive_limit"] == 10 * 1024**3
        assert client.post("/api/backup/run", headers=HEADERS).status_code == 409
        assert (
            client.put("/api/backup", headers=HEADERS, json={**body, "mode": "bad"}).status_code
            == 422
        )
        assert client.put(
            f"/api/recordings/{key}/pin", headers=HEADERS, json={"pinned": True}
        ).json()["pinned"]
        assert client.post(f"/api/recordings/{key}/lease", headers=HEADERS).status_code == 200
    assert BackupPolicy(tmp_path).policy()["enabled"] is False
    assert RecordingStore(tmp_path).get(key)["pinned"]
    assert RecordingStore(tmp_path).leased(key)


def test_existing_large_library_requires_explicit_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "usage", lambda _: 129 * 1024**2)
    backup = manager(tmp_path, monkeypatch)
    assert backup.snapshot()["policy"]["requires_profile"]
    assert not backup.snapshot()["policy"]["enabled"]
    with pytest.raises(CameraError) as error:
        maintenance.cleanup(tmp_path)
    assert error.value.code == "storage_profile"


def test_cancel_interrupts_wait_for_missing_packet():
    from v360_web.bridge import AndroidBridge

    event = threading.Event()
    event.set()
    context = progress.cancel_event.set(event)
    try:
        bridge = AndroidBridge("127.0.0.1", 1, TOKEN)
        with pytest.raises(CameraError) as error:
            bridge.read_exact(1, 0, 24, time.monotonic() + 120)
        assert error.value.code == "backup_yielded"
    finally:
        progress.cancel_event.reset(context)


def test_scheduler_runs_without_browser_or_api_calls(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    queried = threading.Event()

    def discover(*args):
        queried.set()
        return {"metadata_complete": True, "records": 0}

    monkeypatch.setattr(service, "discover", discover)
    backup.update(next_run=time.time() - 1)
    backup.start()
    try:
        assert queried.wait(timeout=3)
    finally:
        backup.close()
    assert not backup.thread.is_alive()
    assert not backup.running
