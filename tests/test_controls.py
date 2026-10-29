import base64
import struct
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from v360_web import battery, protocol
from v360_web.api import create_app
from v360_web.controls import CameraControls
from v360_web.errors import CameraError
from v360_web.streaming import Subscriber


def test_watching_queries_immediately_then_every_five_minutes_on_same_session(tmp_path):
    sent = []
    settings = SimpleNamespace(did="test-camera", data_root=tmp_path, command_channel=0)
    frames = bytearray()

    def send(command, payload):
        sent.append(command)
        frames.extend(protocol.command_frame(913, struct.pack("<iii", 63, 40000, 0)))

    def rpc(op, **args):
        assert op == "read" and args["session"] == 7
        data = bytes(frames[:13])
        del frames[:13]
        return {"ret": -3, "data": base64.b64encode(data).decode()}

    control = CameraControls()
    control.camera = SimpleNamespace(
        settings=settings, session=7, send=send, bridge=SimpleNamespace(rpc=rpc)
    )
    for _ in range(4):
        control.step(0)
    assert sent == [912] and battery.cached(settings)["percent"] == 63
    control.step(299)
    assert sent == [912]
    for _ in range(4):
        control.step(300)
    assert sent == [912, 912]
    assert control.error is None


def test_ptz_lease_expires_and_disconnect_releases_owner(monkeypatch):
    sent = []
    clock = [100.0]
    monkeypatch.setattr("v360_web.controls.time.monotonic", lambda: clock[0])
    control = CameraControls()
    control.camera = SimpleNamespace(send=lambda command, payload: sent.append((command, payload)))
    control.thread = SimpleNamespace(is_alive=lambda: True)
    control.request_move("first", 3)
    control.move_tick(100)
    assert sent[-1] == (4097, bytes([3, 8, 0, 0, 0, 0, 3, 0]))
    with pytest.raises(CameraError):
        control.request_move("second", 6)
    control.move_tick(100.5)
    assert sent[-1][1][0] == 0
    clock[0] = 101
    control.request_move("second", 6)
    control.move_tick(101)
    control.release("second")
    control.move_tick(101.1)
    assert [payload[0] for _, payload in sent] == [3, 0, 6, 0]


def test_ptz_api_requires_auth_and_active_viewer(tmp_path, monkeypatch):
    token = "test-ptz-token-123456789012345678901234567890"
    headers = {"Authorization": "Bearer " + token}
    app = create_app(tmp_path, token)
    viewer = Subscriber()
    calls = []
    monkeypatch.setattr(
        app.state.streams.controls, "request_move", lambda owner, direction: calls.append(direction)
    )
    with TestClient(app) as client:
        body = {"viewer_id": viewer.identity, "direction": "left"}
        assert client.post("/api/live/ptz", json=body).status_code == 401
        assert client.post("/api/live/ptz", json=body, headers=headers).status_code == 409
        app.state.streams.viewers.add(viewer)
        assert client.post("/api/live/ptz", json=body, headers=headers).status_code == 200
        assert calls == [3]
        body["direction"] = "invalid"
        assert client.post("/api/live/ptz", json=body, headers=headers).status_code == 422
