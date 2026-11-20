import struct
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
