import io
import struct
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from v360_web.api import create_app
from v360_web.errors import CameraError
from v360_web.media import find_tool, probe, validate_decode
from v360_web.streaming import FragmentReader, LivePacket, StreamManager, Subscriber

TOKEN = "test-only-browser-token-12345678901234567890"


@pytest.fixture(scope="module")
def hevc(tmp_path_factory):
    path = tmp_path_factory.mktemp("stream-source") / "video.hevc"
    subprocess.run(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x96:r=15:d=1",
            "-c:v",
            "libx265",
            "-threads",
            "1",
            "-x265-params",
            "pools=1:frame-threads=1:keyint=15:log-level=error",
            "-an",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return path.read_bytes()


def get(viewer, event):
    kind, value = viewer.messages.get(timeout=10)
    assert kind == event
    return value


def test_one_encoder_shared_and_last_viewer_releases_everything(hevc, tmp_path):
    started, closed = [], threading.Event()

    def source(config, stop):
        started.append(True)
        try:
            while not stop.is_set():
                yield hevc
                stop.wait(0.1)
        finally:
            closed.set()

    lock = threading.Lock()
    manager = StreamManager(tmp_path / "not-read.json", lock, source)
    try:
        first = manager.subscribe()
        mime, initialization = get(first, "init")
        assert "avc1." in mime
        assert manager.encoder_pid is not None
        second = manager.subscribe()
        assert get(second, "init") == (mime, initialization)
        fragment = get(second, "fragment")
        manager.unsubscribe(first)
        assert manager.snapshot()["viewers"] == 1 and not closed.is_set()
        target = tmp_path / "stream.mp4"
        target.write_bytes(initialization + fragment + get(second, "fragment"))
        assert probe(target)["streams"][0]["codec_name"] == "h264"
        validate_decode(target)
        manager.unsubscribe(second)
        manager.thread.join(timeout=6)
        assert closed.is_set() and not manager.thread.is_alive()
        assert not lock.locked() and manager.encoder_pid is None and len(started) == 1
    finally:
        manager.close()


@pytest.mark.parametrize("size", [0, 1, 7, 2**31])
def test_fragment_parser_rejects_unbounded_boxes(size):
    reader = FragmentReader(io.BytesIO(struct.pack(">I4s", size, b"moov")).read)
    with pytest.raises(CameraError) as error:
        next(reader.events())
    assert error.value.code == "encoder_frame"


def test_slow_subscriber_does_not_grow_memory_or_block_other_viewer(tmp_path):
    manager = StreamManager(tmp_path / "unused", threading.Lock())
    slow, fast = Subscriber(), Subscriber()
    manager.viewers.update((slow, fast))
    for _ in range(5):
        manager.publish("fragment", b"bounded")
        assert fast.messages.get_nowait() == ("fragment", b"bounded")
    assert slow.closed.is_set() and slow.messages.qsize() == 4
    assert manager.snapshot()["viewers"] == 1


def test_busy_camera_and_closed_manager_reject_before_source_start(tmp_path):
    lock = threading.Lock()
    lock.acquire()
    manager = StreamManager(tmp_path / "unused", lock)
    with pytest.raises(CameraError) as error:
        manager.subscribe()
    assert error.value.code == "camera_busy"
    lock.release()
    manager.close()
    with pytest.raises(CameraError) as error:
        manager.subscribe()
    assert error.value.code == "stream_unavailable"


def test_encoder_start_failure_still_closes_source_and_unlocks(tmp_path, monkeypatch):
    closed = threading.Event()

    def source(config, stop):
        try:
            yield b"data"
        finally:
            closed.set()

    def missing_encoder():
        raise CameraError("ffmpeg_missing", "Test-only missing encoder")

    monkeypatch.setattr("v360_web.streaming.encoder_command", missing_encoder)
    lock = threading.Lock()
    manager = StreamManager(tmp_path / "unused", lock, source)
    viewer = manager.subscribe()
    manager.thread.join(timeout=3)
    assert viewer.closed.is_set() and closed.is_set() and not lock.locked()
    assert manager.snapshot()["error"] == "ffmpeg_missing"


def test_browser_assets_and_websocket_auth_do_not_start_camera(tmp_path):
    app = create_app(tmp_path, TOKEN, tmp_path / "unused.json")
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "家庭监控" in page.text
        assert "no-store" in page.headers["cache-control"]
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/device.json").status_code == 404
        with client.websocket_connect("/api/live/ws") as socket:
            socket.send_json({"token": "incorrect"})
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/live/ws", headers={"origin": "https://unrelated.example"}
            ),
        ):
            pass
        assert app.state.streams.thread is None


def test_authenticated_socket_disconnect_stops_source(hevc, tmp_path):
    app = create_app(tmp_path, TOKEN, tmp_path / "unused.json")
    cleaned = threading.Event()

    def source(config, stop):
        try:
            while not stop.is_set():
                yield hevc
                stop.wait(0.1)
        finally:
            cleaned.set()

    app.state.streams.source_factory = source
    with TestClient(app) as client:
        with client.websocket_connect("/api/live/ws") as socket:
            socket.send_json({"token": TOKEN})
            assert socket.receive_json()["state"] == "starting"
            assert socket.receive_json()["type"] == "init"
            assert b"moov" in socket.receive_bytes()
            assert b"moof" in socket.receive_bytes()
        assert cleaned.wait(6)
    assert app.state.streams.encoder_pid is None


def test_already_disconnected_socket_can_be_closed_during_final_cleanup(tmp_path, monkeypatch):
    original = WebSocket.close
    calls = []

    async def close(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise WebSocketDisconnect(1006)
        await original(self, *args, **kwargs)

    monkeypatch.setattr(WebSocket, "close", close)
    with (
        TestClient(create_app(tmp_path, TOKEN)) as client,
        client.websocket_connect("/api/live/ws") as socket,
    ):
        socket.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    assert len(calls) == 2


def test_audio_video_mux_shared_and_cleanup(hevc, tmp_path):
    audio = subprocess.check_output(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=1",
            "-f",
            "alaw",
            "pipe:1",
        ],
        timeout=10,
    )

    def source(config, stop):
        yield LivePacket("config", b"alaw")
        while not stop.is_set():
            yield LivePacket("video", hevc)
            yield LivePacket("audio", audio)
            stop.wait(0.1)

    lock = threading.Lock()
    manager = StreamManager(tmp_path / "unused", lock, source)
    try:
        first = manager.subscribe()
        mime, init = get(first, "init")
        assert "mp4a.40.2" in mime
        assert manager.snapshot()["audio"]
        target = tmp_path / "audio-video.mp4"
        target.write_bytes(init + get(first, "fragment") + get(first, "fragment"))
        second = manager.subscribe()
        assert get(second, "init")[0] == mime
        manager.unsubscribe(first)
        manager.unsubscribe(second)
        manager.thread.join(6)
        streams = probe(target)["streams"]
        assert {s["codec_name"] for s in streams} == {"h264", "aac"}
        assert next(s for s in streams if s["codec_type"] == "audio")["sample_rate"] == "16000"
        assert abs(float(streams[0]["start_time"]) - float(streams[1]["start_time"])) < 0.2
        validate_decode(target)
        assert not manager.thread.is_alive() and not lock.locked()
        assert manager.encoder_pid is None
    finally:
        manager.close()
