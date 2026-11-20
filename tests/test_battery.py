import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from v360_web import battery, battery_history
from v360_web.config import write_private_json
from v360_web.errors import CameraError


def test_real_wire_payload_has_no_command_prefix():
    assert battery.parse(struct.pack("<iii", 47, 42300, 0)) == {
        "percent": 47,
        "voltage_raw": 42300,
        "charging": False,
        "charging_raw": 0,
    }
    assert battery.parse(struct.pack("<iii", -1, 0, 255))["percent"] is None
    assert battery.parse(struct.pack("<iii", 101, 0, 255))["charging"] is None
    with pytest.raises(CameraError):
        battery.parse(bytes(11))


def test_cache_does_not_connect_and_becomes_stale(tmp_path, monkeypatch):
    settings = SimpleNamespace(did="test-camera", data_root=tmp_path)
    assert battery.cached(settings)["updated_at"] is None
    write_private_json(
        battery.cache_path(settings), {"percent": 39, "charging": False, "updated_at": 1000}
    )
    monkeypatch.setattr(battery.time, "time", lambda: 1100)
    monkeypatch.setattr(battery.Settings, "load", lambda _: settings)

    def forbidden(*args):
        raise AssertionError("Fresh cache must not connect to camera")

    monkeypatch.setattr(battery, "CameraClient", forbidden)
    assert battery.refresh(tmp_path)["from_cache"]
    monkeypatch.setattr(battery.time, "time", lambda: 2000)
    assert battery.cached(settings)["stale"]


def test_history_skips_duplicates_but_keeps_five_minute_readings(tmp_path):
    history = battery_history.history_for(tmp_path)
    base = datetime(2026, 11, 20, 6, 5)
    assert history.record("cam", 88, True, base)
    # A repeated packet inside the same reading window is not a new sample.
    assert not history.record("cam", 88, True, base + timedelta(seconds=90))
    # The next five-minute reading is kept even when the value has not moved.
    assert history.record("cam", 88, True, base + timedelta(minutes=5))
    assert history.record("cam", 61, False, base + timedelta(minutes=5, seconds=30))
    assert not history.record("cam", None, None, base + timedelta(minutes=6))
    rows = history.day(base.date(), "cam")
    assert [row["percent"] for row in rows] == [88, 88, 61]
    assert [row["charging"] for row in rows] == [True, True, False]
    assert rows[0]["at"] == base.isoformat()


def test_history_caps_rows_and_drops_expired_days(tmp_path, monkeypatch):
    monkeypatch.setattr(battery_history, "MAX_ROWS_PER_DEVICE", 3)
    history = battery_history.history_for(tmp_path)
    start = datetime(2026, 11, 20, 6, 5)
    for index in range(5):
        assert history.record("cam", 70 + index, False, start + timedelta(minutes=10 * index))
    rows = history.day(start.date(), "cam")
    assert [row["percent"] for row in rows] == [72, 73, 74]

    old = datetime(2026, 9, 1, 6, 5)
    assert history.record("cam", 50, None, old)
    assert history.record("cam", 49, None, datetime(2026, 10, 20, 6, 5))
    kept = history.day(old.date(), "cam")
    assert kept == []


def test_history_separates_devices_and_ignores_unknown_percent(tmp_path):
    history = battery_history.history_for(tmp_path)
    base = datetime(2026, 11, 20, 23, 55)
    assert history.record("first", 40, False, base)
    assert history.record("second", 90, True, base)
    assert not history.record("first", None, True, base + timedelta(minutes=5))
    assert [row["percent"] for row in history.day(base.date(), "first")] == [40]
    assert [row["percent"] for row in history.day(base.date(), "second")] == [90]
    assert len(history.day(base.date())) == 2
    assert history.latest("second")["percent"] == 90
    assert history.latest("missing") is None


def test_history_query_defaults_to_today_and_requires_auth(tmp_path):
    from fastapi.testclient import TestClient

    from v360_web.api import create_app

    token = "battery-history-token-1234567890123456789012"
    history = battery_history.history_for(tmp_path)
    today = battery_history.now_local().date()
    history.record("cam", 66, True, datetime.combine(today, datetime.min.time()).replace(hour=7))
    app = create_app(tmp_path, token)
    with TestClient(app) as client:
        assert client.get("/api/battery/history").status_code == 401
        value = client.get(
            "/api/battery/history", headers={"Authorization": "Bearer " + token}
        ).json()
        assert value["day"] == today.isoformat()
        assert value["count"] == 1 and value["items"][0]["percent"] == 66
        assert value["retention_days"] == battery_history.RETENTION_DAYS
        empty = client.get(
            "/api/battery/history?day=2026-01-01",
            headers={"Authorization": "Bearer " + token},
        ).json()
        assert empty["count"] == 0
        assert client.get(
            "/api/battery/history?day=not-a-day",
            headers={"Authorization": "Bearer " + token},
        ).status_code == 422


def test_save_returns_immediately_when_history_is_locked(tmp_path):
    settings = SimpleNamespace(did="lock-camera", data_root=tmp_path)
    history = battery_history.history_for(tmp_path)
    lock = sqlite3.connect(history.database, timeout=0.1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT INTO battery_history VALUES ('other','2026-01-01T00:00:00',1,0)")
    day = battery_history.now_local().date()
    writer = battery_history.writer_for(tmp_path)
    try:
        started = time.monotonic()
        value = battery.save(settings, {"percent": 55, "charging": True})
        elapsed = time.monotonic() - started
        # The cache write is local; history goes to a bounded queue and never waits.
        assert elapsed < 0.2
        assert value["percent"] == 55 and value["stale"] is False
        assert battery.cached(settings)["percent"] == 55
        # The writer gives up while the lock is still held instead of blocking.
        assert writer.drain(10)
        assert history.day(day, "lock-camera") == []
    finally:
        lock.rollback()
        lock.close()

    # Once the writer is free again the next reading is stored normally.
    assert writer.offer("lock-camera", 54, True)
    assert writer.drain(10)
    assert [row["percent"] for row in history.day(day, "lock-camera")] == [54]


def _seed_locked_database(root):
    """Hold a write transaction on a database that has no history table yet.

    Creating the table therefore needs the write lock, which is what a first
    history query does while the camera control loop is running.
    """
    root.mkdir(parents=True, exist_ok=True)
    database = root / "recordings.sqlite3"
    setup = sqlite3.connect(database)
    setup.execute("CREATE TABLE lock_holder (id INTEGER PRIMARY KEY)")
    setup.execute("INSERT INTO lock_holder VALUES (1)")
    setup.commit()
    setup.close()
    lock = sqlite3.connect(database, timeout=0.1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT INTO lock_holder VALUES (2)")
    return lock


def test_first_history_read_does_not_block_saving(tmp_path):
    root = tmp_path / "data"
    settings = SimpleNamespace(did="fresh-camera", data_root=root)
    lock = _seed_locked_database(root)
    errors = []

    def first_read():
        try:
            battery_history.history_for(root)
        except Exception as error:  # noqa: BLE001 -- the locked table may refuse to be created
            errors.append(error)

    reader = threading.Thread(target=first_read, name="first-history-read", daemon=True)
    try:
        reader.start()
        # Let the reader reach the locked CREATE TABLE before saving.
        time.sleep(0.2)
        started = time.monotonic()
        value = battery.save(settings, {"percent": 52, "charging": False})
        elapsed = time.monotonic() - started
        assert elapsed < 0.3
        assert value["percent"] == 52 and battery.cached(settings)["percent"] == 52
        assert reader.is_alive() or errors
        writer = battery_history.writer_for(root)
        assert writer.drain(10)
        assert writer.thread is not None
    finally:
        lock.rollback()
        lock.close()
        reader.join(5)
    assert not reader.is_alive()
