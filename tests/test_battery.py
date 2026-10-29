import struct
from types import SimpleNamespace

import pytest

from v360_web import battery
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
