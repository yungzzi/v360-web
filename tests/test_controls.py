import base64
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from v360_web import battery, battery_history, protocol
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


def test_watching_reading_is_kept_without_extra_camera_queries(tmp_path):
    sent = []
    settings = SimpleNamespace(did="watch-battery", data_root=tmp_path, command_channel=0)
    frames = bytearray()

    def send(command, payload):
        sent.append(command)
        frames.extend(protocol.command_frame(913, struct.pack("<iii", 71, 40000, 1)))

    def rpc(op, **args):
        data = bytes(frames[:13])
        del frames[:13]
        return {"ret": -3, "data": base64.b64encode(data).decode()}

    control = CameraControls()
    control.camera = SimpleNamespace(
        settings=settings, session=9, send=send, bridge=SimpleNamespace(rpc=rpc)
    )
    for _ in range(3):
        control.step(0)
    for _ in range(3):
        control.step(300)
    # Same five-minute cadence as before: one query per reading, nothing extra.
    assert sent == [912, 912]
    assert battery_history.writer_for(tmp_path).drain(10)
    rows = battery_history.history_for(tmp_path).day(
        battery_history.now_local().date(), "watch-battery"
    )
    # The second read returns an identical packet, so it is de-duplicated.
    assert len(rows) == 1
    assert rows[0]["percent"] == 71 and rows[0]["charging"] is True


def _lock_history_table(tmp_path):
    """Hold a real write transaction on the history table like a busy writer."""
    history = battery_history.history_for(tmp_path)
    assert history.day(battery_history.now_local().date(), "watch-battery") == []
    lock = sqlite3.connect(history.database, timeout=0.1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute(
        "INSERT INTO battery_history VALUES ('other','2026-01-01T00:00:00',1,0)"
    )
    return lock


def test_locked_database_never_delays_ptz_stop(tmp_path, monkeypatch):
    # Replace only the controls module's clock reference; the test keeps the real one.
    monkeypatch.setattr(
        "v360_web.controls.time", SimpleNamespace(monotonic=lambda: 100.0)
    )
    sent = []
    settings = SimpleNamespace(did="watch-battery", data_root=tmp_path, command_channel=0)
    frames = bytearray()

    def send(command, payload):
        sent.append((command, payload))
        if command == 912:
            frames.extend(protocol.command_frame(913, struct.pack("<iii", 64, 40100, 0)))

    def rpc(op, **args):
        data = bytes(frames[:13])
        del frames[:13]
        return {"ret": -3, "data": base64.b64encode(data).decode()}

    control = CameraControls()
    control.thread = SimpleNamespace(is_alive=lambda: True)
    control.camera = SimpleNamespace(
        settings=settings, session=4, send=send, bridge=SimpleNamespace(rpc=rpc)
    )
    lock = _lock_history_table(tmp_path)
    try:
        # A battery packet needs a few partial reads before it parses.
        started = time.monotonic()
        for _ in range(4):
            control.step(100)
        assert time.monotonic() - started < 0.3
        assert battery.cached(settings)["percent"] == 64

        control.request_move("viewer", 3)
        control.move_tick(100)
        assert sent[-1] == (4097, bytes([3, 8, 0, 0, 0, 0, 3, 0]))
        # The tick that releases the PTZ lease (0.4 s) also reads the battery.
        started = time.monotonic()
        for _ in range(4):
            control.step(100.5)
        assert time.monotonic() - started < 0.3
        assert (4097, bytes([0, 8, 0, 0, 0, 0, 3, 0])) in sent
        assert control.error is None
        assert battery_history.writer_for(tmp_path).drain(10)
        # The locked write is dropped instead of holding the control loop.
        assert battery_history.history_for(tmp_path).day(
            battery_history.now_local().date(), "watch-battery"
        ) == []
    finally:
        lock.rollback()
        lock.close()

    assert battery_history.writer_for(tmp_path).offer("watch-battery", 63, False)
    assert battery_history.writer_for(tmp_path).drain(10)
    rows = battery_history.history_for(tmp_path).day(
        battery_history.now_local().date(), "watch-battery"
    )
    assert [row["percent"] for row in rows] == [63]


def test_history_writer_drops_instead_of_blocking_when_full(tmp_path, monkeypatch):
    writer = battery_history.HistoryWriter(tmp_path, capacity=2)
    hold = threading.Event()
    entered = threading.Event()

    class SlowStore:
        def record(self, *args):
            entered.set()
            hold.wait(5)
            return True

    monkeypatch.setattr(writer, "_store", lambda: SlowStore())
    try:
        assert writer.offer("cam", 50, False)
        assert entered.wait(2)
        while not writer.queue.full():
            assert writer.offer("cam", 50, False)
        started = time.monotonic()
        assert writer.offer("cam", 51, False) is False
        assert time.monotonic() - started < 0.1
        assert writer.dropped == 1
    finally:
        hold.set()
        writer.drain(5)


def test_first_history_read_does_not_delay_ptz_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "v360_web.controls.time", SimpleNamespace(monotonic=lambda: 200.0)
    )
    sent = []
    settings = SimpleNamespace(did="watch-battery", data_root=tmp_path, command_channel=0)
    frames = bytearray()

    def send(command, payload):
        sent.append((command, payload))
        if command == 912:
            frames.extend(protocol.command_frame(913, struct.pack("<iii", 58, 40200, 0)))

    def rpc(op, **args):
        data = bytes(frames[:13])
        del frames[:13]
        return {"ret": -3, "data": base64.b64encode(data).decode()}

    control = CameraControls()
    control.thread = SimpleNamespace(is_alive=lambda: True)
    control.camera = SimpleNamespace(
        settings=settings, session=5, send=send, bridge=SimpleNamespace(rpc=rpc)
    )

    root = tmp_path / "data"
    root.mkdir()
    database = root / "recordings.sqlite3"
    setup = sqlite3.connect(database)
    setup.execute("CREATE TABLE lock_holder (id INTEGER PRIMARY KEY)")
    setup.execute("INSERT INTO lock_holder VALUES (1)")
    setup.commit()
    setup.close()
    lock = sqlite3.connect(database, timeout=0.1)
    lock.execute("BEGIN IMMEDIATE")
    lock.execute("INSERT INTO lock_holder VALUES (2)")

    errors = []

    def first_read():
        try:
            battery_history.history_for(root)
        except Exception as error:  # noqa: BLE001 -- the locked table may refuse to be created
            errors.append(error)

    reader = threading.Thread(target=first_read, name="first-history-read", daemon=True)
    try:
        reader.start()
        time.sleep(0.2)
        # The tick that receives a battery packet must not wait for the reader.
        started = time.monotonic()
        for _ in range(4):
            control.step(200)
        assert time.monotonic() - started < 0.3
        assert battery.cached(settings)["percent"] == 58

        control.request_move("viewer", 3)
        control.move_tick(200)
        assert sent[-1] == (4097, bytes([3, 8, 0, 0, 0, 0, 3, 0]))
        started = time.monotonic()
        control.step(200.5)
        assert time.monotonic() - started < 0.3
        assert sent[-1] == (4097, bytes([0, 8, 0, 0, 0, 0, 3, 0]))
        assert control.error is None
        # The reader is still waiting for the table it cannot create.
        assert reader.is_alive() or errors
    finally:
        lock.rollback()
        lock.close()
        reader.join(5)
    assert not reader.is_alive()
