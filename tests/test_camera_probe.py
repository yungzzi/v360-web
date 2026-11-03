import struct
from types import SimpleNamespace

import pytest

from v360_web import camera_probe
from v360_web.errors import CameraError


class Peer:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []
        self.session = 1
        self.settings = SimpleNamespace(request_timeout=1)
        self.bridge = SimpleNamespace(rpc=lambda *args, **kwargs: {"ret": -3, "data": ""})

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
    assert peer.sent[0] == (816, bytes(4))
    assert peer.sent[1][0] == 1287


def test_rejected_sd_probe_always_stops_and_keeps_auth_fields(monkeypatch):
    peer = Peer(struct.pack("<iihBB", 16, -1, 0, 0, 0))
    setup(monkeypatch, peer)
    item = {
        "device_id": "test",
        "channel": 0,
        "event_type": 0,
        "duration_raw": 15,
        "start_local": "2026-11-02T12:00:00",
        "weekday": 1,
    }
    monkeypatch.setattr(
        camera_probe, "RecordingStore", lambda _: SimpleNamespace(get=lambda key: item)
    )
    result = camera_probe.playback_probe(None, "key")
    assert result["result"] == -1 and result["stop_requested"]
    assert [struct.unpack_from("<I", b, 4)[0] for _, b in peer.sent] == [1, 16, 1]
    assert peer.sent[1][1][20:] == bytes.fromhex("00007856")


def test_storage_truncation_is_not_reported_healthy(monkeypatch):
    setup(monkeypatch, Peer(bytes(48)))
    with pytest.raises(CameraError, match="truncated"):
        camera_probe.storage_status(None)


def test_probe_receives_frame_when_reader_precedes_start(monkeypatch):
    import base64
    import threading

    started = threading.Event()
    peer = Peer(struct.pack("<iihBB", 16, 0, 15, 0, 0))
    original_send = peer.send

    def send(cmd, data):
        original_send(cmd, data)
        if struct.unpack_from("<I", data, 4)[0] == 16:
            started.set()

    peer.send = send
    header = struct.pack("<4I", 428213528, 1234, 1793620000, 16384)

    def read(*args, **kwargs):
        if not started.is_set():
            return {"ret": -3, "data": ""}
        return {"ret": 0, "data": base64.b64encode(header).decode()}

    peer.bridge.rpc = read
    setup(monkeypatch, peer)
    item = {
        "device_id": "test",
        "channel": 0,
        "event_type": 0,
        "duration_raw": 15,
        "start_local": "2026-11-02T12:00:00",
        "weekday": 1,
    }
    monkeypatch.setattr(
        camera_probe, "RecordingStore", lambda _: SimpleNamespace(get=lambda key: item)
    )
    result = camera_probe.playback_probe(None, "key")
    assert result["frame_size"] == 1234 and result["stop_requested"]
    assert "probe_error" not in result
    assert result["frame_time_in_requested_range"] is False
    assert "stop_confirmed" in result
