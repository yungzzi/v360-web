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


def test_playback_primed_download_uses_same_session_and_always_stops(monkeypatch):
    peer = Peer(struct.pack("<iihBB", 16, 0, 15, 0, 0))
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
        camera_probe, "RecordingStore", lambda _: SimpleNamespace(get=lambda _: item)
    )
    # Empty read ends immediately with a transport error; a missing frame must
    # not prevent testing the independent, identity-checked file-transfer command.
    peer.bridge.rpc = lambda *args, **kwargs: {"ret": -4, "data": ""}
    calls = []

    def download(config, key, **kwargs):
        calls.append((key, kwargs))
        raise CameraError("download_rejected", "unavailable", -1)

    monkeypatch.setattr("v360_web.service.download", download)
    result = camera_probe.playback_probe(None, "key", test_download=True)
    assert calls == [("key", {"camera": peer, "defer_validation": True})]
    assert result["primed_download_error"] == "download_rejected"
    assert result["stop_requested"]
    assert struct.unpack_from("<I", peer.sent[-1][1], 4)[0] == 1


def test_health_read_collects_log_pages_without_sending_a_setter(tmp_path):
    peer = Peer(b"")
    replies = iter(
        [
            struct.pack("<3i", 0, 2, 0),
            struct.pack("<3i", 2, 1, 6) + b"reboot",
            struct.pack("<3i", 2, 0, 3) + b"mmc",
        ]
    )
    peer.response = lambda *args: next(replies)
    result = camera_probe.read_device_health(peer, tmp_path)
    assert result["record_type_raw"] == 2
    assert result["device_log_complete"] and result["device_log_bytes"] == 9
    path = tmp_path / "protocol-diagnostics/device-health-latest.log"
    assert path.read_bytes() == b"mmcreboot"
    assert path.stat().st_mode & 0o077 == 0
    assert peer.sent == [(786, bytes(8)), (33224, bytes(8))]


def test_health_read_keeps_missing_pages_incomplete(tmp_path):
    peer = Peer(b"")
    replies = iter([struct.pack("<3i", 0, 2, 0), struct.pack("<3i", 2, 0, 3) + b"mmc"])

    def response(*args):
        try:
            return next(replies)
        except StopIteration:
            raise CameraError("read_timeout", "no second page") from None

    peer.response = response
    result = camera_probe.read_device_health(peer, tmp_path)
    assert not result["device_log_complete"] and result["device_log_error"] == "read_timeout"
    assert result["device_log_pages"] == 1


def test_health_read_rejects_truncated_log_content(tmp_path):
    peer = Peer(b"")
    replies = iter([struct.pack("<3i", 0, 2, 0), struct.pack("<3i", 1, 0, 9) + b"mmc"])
    peer.response = lambda *args: next(replies)
    result = camera_probe.read_device_health(peer, tmp_path)
    assert result["device_log_error"] == "device_log_response"
    assert result["device_log_bytes"] == 0 and not result["device_log_complete"]
