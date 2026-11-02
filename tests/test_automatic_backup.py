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
    monkeypatch.setattr("v360_web.backup.playback.prepare", lambda *args: {"state": "ready"})
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
        assert value["policy"]["archive_limit"] == 15 * 1024**3
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


def test_server_backfills_one_day_and_persists_cursor(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    queries = []

    def discover(config, channel, start, end):
        queries.append((start, end))
        return {"metadata_complete": False, "records": 0}

    monkeypatch.setattr(service, "discover", discover)
    backup.cycle()
    assert len(queries) == 2
    assert queries[1][0].hour == 0 and queries[1][1].hour == 23
    assert not backup.snapshot()["backfill_complete"]
    restarted = manager(tmp_path, monkeypatch)
    restarted.update(next_discovery=0)
    monkeypatch.setattr(service, "discover", discover)
    restarted.cycle()
    assert queries[-1][0].date() == queries[1][0].date() - timedelta(days=1)


def test_new_queue_precedes_history_and_summary_counts(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    for i in range(10):
        record(tmp_path, i, state="discovered")
    record(tmp_path, 15, state="failed")
    record(tmp_path, 16, state="verified", size=99)
    queries = []
    monkeypatch.setattr(
        service,
        "discover",
        lambda *a: queries.append(a) or {"metadata_complete": True, "records": 0},
    )
    monkeypatch.setattr(service, "download", lambda *a: None)
    backup.cycle()
    assert len(queries) == 1
    assert backup.snapshot()["archive"] == {
        "recent_pending": 12,
        "overdue_pending": 0,
        "retry_pending": 0,
        "ready": 0,
        "awaiting_playback": 12,
        "pending_in_scope": 11,
        "oldest_ready": None,
        "ready_span_days": 0,
        "saved": 1,
        "saved_bytes": 99,
        "pending": 11,
        "failed": 1,
        "evicted": 0,
    }


def test_history_outside_three_days_is_not_downloaded(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    _key, _ = record(tmp_path, 10 * 24 * 60, state="discovered")
    calls = []
    monkeypatch.setattr(service, "download", lambda config, key: calls.append(key))
    backup.cycle()
    assert calls == []


def test_failed_history_query_preserves_cursor(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    calls = []

    def discover(*args):
        calls.append(args)
        if len(calls) == 2:
            raise CameraError("sdk_connect", "unavailable")
        return {"metadata_complete": True, "records": 0}

    monkeypatch.setattr(service, "discover", discover)
    backup.cycle()
    assert backup.db.read("backfill3:test-device") is None
    assert not backup.running


def test_history_failure_does_not_block_known_downloads(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    key, _ = record(tmp_path, state="discovered")
    calls = []
    queries = []

    def discover(*args):
        queries.append(args)
        if len(queries) == 2:
            raise CameraError("sdk_connect", "unavailable")
        return {"metadata_complete": True, "records": 0}

    monkeypatch.setattr(service, "discover", discover)
    monkeypatch.setattr(service, "download", lambda config, key: calls.append(key))
    backup.cycle()
    assert calls == [key]
    assert len(queries) == 1
    assert backup.snapshot().get("backfill_error") is None
    assert backup.snapshot()["error"] is None


def test_server_pipeline_exceeds_ten_and_finishes_each_record_before_next_download(
    tmp_path, monkeypatch
):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    for i in range(11):
        record(tmp_path, i, state="discovered")

    def save(config, key):
        progress.report("downloading", bytes=0, total=100)
        RecordingStore(tmp_path).update(key, "verified")

    from v360_web import playback

    events = []

    def download(config, key):
        events.append(("download", key))
        save(config, key)

    monkeypatch.setattr(service, "download", download)
    monkeypatch.setattr(playback, "prepare", lambda config, key: events.append(("prepare", key)))
    backup.cycle()
    state = backup.snapshot()
    assert state["archive"]["pending"] == 0
    assert 50 < state["next_run"] - time.time() <= 60
    assert len(events) == 22
    for i in range(0, 22, 2):
        assert events[i][0] == "download" and events[i + 1] == ("prepare", events[i][1])
    assert state["policy"]["daily_bytes"] is None
    assert backup.db.read("daily")["bytes"] == 1100


def test_server_prepares_saved_recordings_without_downloading_them(tmp_path, monkeypatch):
    from v360_web import playback

    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    key, _ = record(tmp_path)
    prepared = []
    monkeypatch.setattr(playback, "ready", lambda *args: None)
    monkeypatch.setattr(playback, "prepare", lambda config, key: prepared.append(key))
    monkeypatch.setattr(
        service,
        "download",
        lambda *args: pytest.fail("Saved original must not be downloaded again"),
    )
    backup.db.write(
        "daily",
        {"day": datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat(), "bytes": 1024**4},
    )
    backup.cycle()
    assert prepared == [key]
    assert backup.snapshot()["last_prepared"] == 1
    assert backup.snapshot()["state"] == "waiting"


def test_combined_server_budget_removes_old_pair_and_preserves_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "SERVER_TOTAL_LIMIT", 100)
    old, old_path = record(tmp_path, 30)
    pin, pin_path = record(tmp_path, 20)
    recent, recent_path = record(tmp_path, 10)
    policy(tmp_path, mode="server")
    store = RecordingStore(tmp_path)
    store.pin(pin, True)
    previews = tmp_path / "previews"
    previews.mkdir()
    for key in [old, pin, recent]:
        (previews / (key + ".mp4")).write_bytes(b"x" * 10)
    value = maintenance.cleanup(tmp_path, kind="cache")
    assert value["removed_recordings"] == 1
    assert not old_path.exists() and not (previews / (old + ".mp4")).exists()
    assert pin_path.exists() and recent_path.exists()
    assert storage.summary(tmp_path)["combined_bytes"] == 80
    with pytest.raises(CameraError):
        storage.admit(tmp_path, 21, "cache")
    assert pin_path.exists() and recent_path.exists()


def test_server_preparation_failures_stop_at_work_window(tmp_path, monkeypatch):
    from v360_web import playback

    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    for i in range(15):
        record(tmp_path, i)
    calls = []
    monkeypatch.setattr(playback, "ready", lambda *args: None)

    clock = [0]
    monkeypatch.setattr("v360_web.backup.time.monotonic", lambda: clock[0])

    def fail(config, key):
        clock[0] += 151
        calls.append(key)
        raise CameraError("preview_failed", "fixture")

    monkeypatch.setattr(playback, "prepare", fail)
    backup.cycle()
    assert len(calls) == 2


def test_connection_outage_stops_batch_and_shortens_legacy_backoff(tmp_path, monkeypatch):
    keys = [record(tmp_path, i, state="discovered")[0] for i in range(4)]
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    calls = []

    def fail(config, key):
        calls.append(key)
        RecordingStore(tmp_path).update(key, "failed", error="sdk_connect")
        raise CameraError("sdk_connect", "offline", -3)

    monkeypatch.setattr(service, "download", fail)
    backup.cycle()
    assert calls == keys[-1:]
    assert backup.snapshot()["last_file_sdk_code"] == -3
    assert not backup.retry_ready(keys[-1], time.time())
    assert backup.retry_ready(keys[-1], time.time() + 601)
    # New retry deadlines are independent of mutable catalog timestamps.
    with backup.db.connection() as db:
        db.execute(
            "UPDATE backup_retries SET next_try=? WHERE id=?", (time.time() + 86400, keys[-1])
        )
    assert not backup.retry_ready(keys[-1], time.time() + 601)
    assert RecordingStore(tmp_path).get(keys[1])["state"] == "discovered"


def test_archive_pending_age_and_failures_are_separate(tmp_path, monkeypatch):
    record(tmp_path, 0, state="discovered")
    old, _ = record(tmp_path, 60, state="failed")
    RecordingStore(tmp_path).update(old, "failed", error="sdk_read")
    backup = manager(tmp_path, monkeypatch)
    summary = backup.archive_summary()
    assert summary["recent_pending"] == 1
    assert summary["overdue_pending"] == 1
    assert summary["retry_pending"] == 1
    assert summary["awaiting_playback"] == 2


def test_catalog_outage_still_prepares_local_originals(tmp_path, monkeypatch):
    local, _ = record(tmp_path, 10, state="verified")
    record(tmp_path, 0, state="discovered")
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    prepared = []

    def offline(*args):
        raise CameraError("sdk_connect", "offline", -3)

    monkeypatch.setattr(service, "discover", offline)
    monkeypatch.setattr(service, "download", lambda *args: pytest.fail("No extra camera attempt"))
    monkeypatch.setattr("v360_web.backup.playback.prepare", lambda cfg, key: prepared.append(key))
    backup.cycle()
    assert prepared == [local]
    assert backup.snapshot()["catalog_error"] == "sdk_connect"
    assert backup.snapshot()["last_prepared"] == 1


def test_legacy_retry_migration_survives_catalog_refresh(tmp_path, monkeypatch):
    key, _ = record(tmp_path, state="failed")
    store = RecordingStore(tmp_path)
    store.update(key, "failed", error="sdk_connect")
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    with backup.db.connection() as db:
        db.execute("INSERT INTO backup_retries VALUES (?,?,?)", (key, 5, time.time() + 86400))
    calls = []
    monkeypatch.setattr(service, "download", lambda cfg, ident: calls.append(ident))
    backup.cycle()
    assert calls == [key]
    assert backup.db.read("transport_retry_migration_v2") is True


def test_fixed_recovery_scope_and_fair_order(tmp_path, monkeypatch):
    keys = [record(tmp_path, n, state="discovered")[0] for n in (0, 10, 20, 30)]
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    backup.db.write("recovery_floor:test-device", "2026-01-01T00:00:00")
    backup.db.write("fresh_floor:test-device", (datetime.now() - timedelta(days=1)).isoformat())
    with backup.db.connection() as db:
        db.execute(
            "UPDATE recordings SET start_local=? WHERE id=?",
            ((datetime.now() - timedelta(days=4)).isoformat(), keys[-1]),
        )
    calls = []
    monkeypatch.setattr(service, "download", lambda cfg, key: calls.append(key))
    backup.cycle()
    assert calls == [keys[2], keys[1], keys[0], keys[3]]


def test_minute_recovery_does_not_repeat_successful_catalog(tmp_path, monkeypatch):
    _key, _ = record(tmp_path, state="discovered")
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    queries = []
    monkeypatch.setattr(
        service,
        "discover",
        lambda *args: queries.append(1) or {"metadata_complete": True, "records": 1},
    )

    def fail(*args):
        raise CameraError("sdk_connect", "offline", -3)

    monkeypatch.setattr(service, "download", fail)
    backup.cycle()
    assert 50 < backup.state["next_run"] - time.time() <= 60
    backup.cycle()
    assert queries == [1]


def test_local_preparation_does_not_acquire_camera_lock(tmp_path, monkeypatch):
    key, _ = record(tmp_path)
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    backup.db.write("recovery_floor:test-device", "2020-01-01T00:00:00")
    calls = []
    monkeypatch.setattr(
        "v360_web.backup.playback.prepare", lambda config, ident: calls.append(ident)
    )
    backup.camera_lock.acquire()
    try:
        assert backup.prepare_one()
    finally:
        backup.camera_lock.release()
    assert calls == [key]


def test_download_prefetch_bounds_survive_restart(tmp_path, monkeypatch):
    backup = manager(tmp_path, monkeypatch)
    policy(tmp_path, mode="server")
    keys = [record(tmp_path, i, state="discovered")[0] for i in range(5)]
    backup.prepare_thread = object()  # Exercise production pipeline selection without a racer.
    calls = []

    def save(config, key):
        calls.append(key)
        RecordingStore(tmp_path).update(key, "verified")

    monkeypatch.setattr(service, "download", save)
    backup.cycle()
    assert len(calls) == 3
    assert len(backup.db.read("download_ahead")) == 3
    restarted = manager(tmp_path, monkeypatch)
    restarted.prepare_thread = object()
    restarted.cycle()
    assert len(calls) == 3
    assert len(keys) == 5


def test_all_playback_callers_share_one_encoder():
    from v360_web.playback import serial_encoder

    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()

    @serial_encoder
    def first():
        entered.set()
        assert release.wait(2)

    @serial_encoder
    def other():
        second.set()

    a = threading.Thread(target=first)
    b = threading.Thread(target=other)
    a.start()
    assert entered.wait(1)
    b.start()
    try:
        assert not second.wait(0.1)
    finally:
        release.set()
        a.join(2)
        b.join(2)
    assert second.is_set()
