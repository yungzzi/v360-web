import struct
from types import SimpleNamespace

import pytest

from v360_web import camera_probe
from v360_web.errors import CameraError


class Peer:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, command, data):
        self.sent.append((command, bytes(data)))

    def response(self, command, deadline):
        return self.reply


def setup(monkeypatch, peer):
    monkeypatch.setattr(
        camera_probe.Settings,
        "load",
        lambda _: SimpleNamespace(
            request_timeout=1, did="test", data_root=None, shared=False, user_id=0x12345678
        ),
    )
    monkeypatch.setattr(camera_probe, "CameraClient", lambda _: peer)


def test_storage_response_handles_card_fault_without_formatting(monkeypatch):
    payload = bytearray(56)
    struct.pack_into("<ii", payload, 40, -1, 0)
    peer = Peer(payload)
    setup(monkeypatch, peer)
    result = camera_probe.storage_status(None)
    assert result["anomaly"] and not result["has_card"]
    assert peer.sent == [(816, bytes(4))]


def test_rejected_sd_probe_always_stops_and_keeps_auth_fields(monkeypatch):
    peer = Peer(struct.pack("<iihBB", 16, -1, 0, 0, 0))
    setup(monkeypatch, peer)
    item = {
        "device_id": "test",
        "channel": 0,
        "event_type": 0,
        "start_local": "2026-11-02T12:00:00",
        "weekday": 1,
    }
    monkeypatch.setattr(
        camera_probe, "RecordingStore", lambda _: SimpleNamespace(get=lambda key: item)
    )
    result = camera_probe.playback_probe(None, "key")
    assert result["result"] == -1 and result["stop_requested"]
    assert [struct.unpack_from("<I", b, 4)[0] for _, b in peer.sent] == [16, 1]
    assert peer.sent[0][1][20:] == bytes.fromhex("00007856")


def test_storage_truncation_is_not_reported_healthy(monkeypatch):
    setup(monkeypatch, Peer(bytes(48)))
    with pytest.raises(CameraError, match="truncated"):
        camera_probe.storage_status(None)
