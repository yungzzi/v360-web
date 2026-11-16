import io
import queue
import subprocess
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from v360_web import maintenance, playback, server_capture, storage
from v360_web.api import create_app
from v360_web.backup_policy import BackupPolicy
from v360_web.config import write_private_json
from v360_web.errors import CameraError
from v360_web.media import find_tool, probe, validate_decode
from v360_web.protocol import RecordingEvent
from v360_web.store import SCHEDULED_SOURCE, WATCH_SOURCE, RecordingStore
from v360_web.streaming import FragmentReader, StreamManager

TOKEN = "server-capture-test-token-12345678901234567890"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(scope="module")
def fragments(tmp_path_factory):
    target = tmp_path_factory.mktemp("server-capture") / "fragmented.mp4"
    command = [
        find_tool("ffmpeg"),
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=160x96:r=15:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=8000:duration=3",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-preset",
        "ultrafast",
        "-g",
        "15",
        "-c:a",
        "aac",
        "-b:a",
        "24k",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        str(target),
    ]
    subprocess.run(command, capture_output=True, check=True, timeout=60)
    reader = FragmentReader(io.BytesIO(target.read_bytes()).read)
    init = None
    pieces = []
    try:
        for kind, value in reader.events():
            if kind == "init" and init is None:
                init = value[1]
            elif kind == "fragment":
                pieces.append(value)
    except CameraError:
        pass
    assert init and len(pieces) >= 2
    return init, pieces


@pytest.fixture(scope="module")
def hevc_bytes(tmp_path_factory):
    target = tmp_path_factory.mktemp("server-capture-hevc") / "video.hevc"
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
            str(target),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return target.read_bytes()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3)
    )
    root = tmp_path / "data"
    config = tmp_path / ".private/device.json"
    write_private_json(config.parent / "sdk-profiles.json", {"initialize": "unused-test-profile"})
    write_private_json(
        config,
        {
            "device": {"did": "MATE-capture", "password": "example"},
            "bridge": {"adapter": "native-arm64"},
        },
    )
    return server_capture.CaptureManager(root, config, enabled=True), config


def feed(sink, init, pieces, *, span=2):
    mime = 'video/mp4; codecs="avc1.42e01e,mp4a.40.2"'
    sink.publish("init", (mime, init), datetime(2026, 11, 16, 6), 1000)
    for index, piece in enumerate(pieces):
        wall = datetime(2026, 11, 16, 6, 0, index * span)
        sink.note_video(wall, 1000 + index * span)
        sink.publish("fragment", piece, wall, 1000 + index * span)


def test_next_slot_covers_day_night_and_midnight():
    assert server_capture.next_slot(datetime(2026, 11, 16, 5, 59, 59)) == (
        datetime(2026, 11, 16, 6, 0),
        30,
    )
    assert server_capture.next_slot(datetime(2026, 11, 16, 6, 0, 1)) == (
        datetime(2026, 11, 16, 6, 10),
        30,
    )
    assert server_capture.next_slot(datetime(2026, 11, 16, 17, 59, 59)) == (
        datetime(2026, 11, 16, 18, 0),
        15,
    )
    assert server_capture.next_slot(datetime(2026, 11, 16, 18, 0, 1)) == (
        datetime(2026, 11, 16, 18, 30),
        15,
    )
    assert server_capture.next_slot(datetime(2026, 11, 16, 23, 59, 59)) == (
        datetime(2026, 11, 17, 0, 0),
        15,
    )
    assert server_capture.next_slot(datetime(2026, 11, 17, 5, 59, 59)) == (
        datetime(2026, 11, 17, 6, 0),
        30,
    )


def test_shared_output_creates_independent_playable_segments(manager, fragments, monkeypatch):
    capture, _ = manager
    capture.did = "MATE-capture"
    monkeypatch.setattr(server_capture, "SEGMENT_SECONDS", 1)
    sink = capture.new_watch()
    init, pieces = fragments
    feed(sink, init, pieces)
    sink.flush()
    sink.close()
    rows = [row for row in RecordingStore(capture.root).list() if row["source"] == WATCH_SOURCE]
    assert len(rows) >= 2
    for row in rows:
        path = capture.root / row["path"]
        assert path.is_file() and path.stat().st_size == row["bytes"]
        info = playback.ready(capture.root, row["id"])
        assert info == path
        validate_decode(path)
        assert any(stream["codec_name"] == "h264" for stream in probe(path)["streams"])
        assert any(
            stream["codec_type"] == "audio"
            for stream in probe(path)["streams"]
        )
    assert capture.snapshot()["segments"][WATCH_SOURCE] == len(rows)


def test_handoff_splits_source_without_duplicating_fragments(manager, fragments, monkeypatch):
    capture, _ = manager
    capture.did = "MATE-capture"
    monkeypatch.setattr(server_capture, "SEGMENT_SECONDS", 1)
    sink = capture.new_scheduled(30)
    init, pieces = fragments
    feed(sink, init, pieces[:1])
    sink.flush()
    sink.handoff(WATCH_SOURCE, datetime(2026, 11, 16, 6, 0, 10), 1100)
    for index, piece in enumerate(pieces[1:], 2):
        wall = datetime(2026, 11, 16, 6, 0, index)
        sink.note_video(wall, 1000 + index)
        sink.publish("fragment", piece, wall, 1000 + index)
    sink.flush()
    sink.close()
    rows = RecordingStore(capture.root).list()
    assert {row["source"] for row in rows} == {SCHEDULED_SOURCE, WATCH_SOURCE}
    bodies = [(capture.root / row["path"]).read_bytes() for row in rows]
    assert sum(body.count(b"moof") for body in bodies) == sum(
        piece.count(b"moof") for piece in pieces
    )


def test_unfinished_capture_is_not_indexed(manager, fragments):
    capture, _ = manager
    init, _pieces = fragments
    sink = capture.new_watch()
    sink.publish("init", ("video/mp4", init), datetime(2026, 11, 16, 6), 1000)
    sink.flush()
    sink.close()
    assert RecordingStore(capture.root).list() == []
    assert not list((capture.root / "captures").glob("*.part"))
    assert not list((capture.root / "captures").glob("*.mp4"))


def test_capacity_cleanup_only_evicts_old_unused_captures(manager, monkeypatch):
    capture, _ = manager
    store = RecordingStore(capture.root)
    directory = capture.root / "captures"
    directory.mkdir(parents=True)
    for source, key, saved, size, pinned in (
        (WATCH_SOURCE, "a" * 32, "2026-01-01T00:00:00+00:00", 30, False),
        (SCHEDULED_SOURCE, "b" * 32, "2026-02-01T00:00:00+00:00", 30, True),
        ("sd", "c" * 32, "2026-03-01T00:00:00+00:00", 30, False),
    ):
        folder = "media" if source == "sd" else "captures"
        target = capture.root / folder / (key + (".media" if source == "sd" else ".mp4"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        if source == "sd":
            store.discover(
                "MATE-test", 0, [RecordingEvent(datetime(2026, 1, 1), 0, 2, 1, 1)]
            )
            with store.connection() as db:
                db.execute(
                    "UPDATE recordings SET id=?,path=?,bytes=?,state='verified',saved_at=? "
                    "WHERE device_id='MATE-test'",
                    (key, folder + "/" + target.name, size, saved),
                )
        else:
            store.add_capture(
                "MATE-capture",
                key,
                source,
                "2026-01-01T00:00:00",
                1,
                folder + "/" + target.name,
                size,
                "0" * 64,
            )
        with store.connection() as db:
            db.execute(
                "UPDATE recordings SET saved_at=?, pinned=? WHERE id=?", (saved, int(pinned), key)
            )
    monkeypatch.setattr(storage, "CAPTURE_LIMIT", 40)
    removed = maintenance.cleanup_captures(capture.root, 5)
    assert removed["ids"] == ["a" * 32]
    assert store.get("b" * 32)["state"] == "verified"
    assert store.get("c" * 32)["state"] == "verified"
    assert (capture.root / "media" / ("c" * 32 + ".media")).is_file()


def test_storage_failure_pauses_capture_without_raising(manager, fragments, monkeypatch):
    capture, _ = manager
    init, pieces = fragments
    sink = capture.new_watch()

    def refused(*args, **kwargs):
        raise CameraError("storage_limit", "full")

    monkeypatch.setattr(capture, "prepare", refused)
    sink.publish("init", ("video/mp4", init), datetime(2026, 11, 16, 6), 1000)
    wall = datetime(2026, 11, 16, 6)
    sink.note_video(wall, 1000)
    sink.publish("fragment", pieces[0], wall, 1000)
    sink.flush()
    assert capture.snapshot()["paused"] is True
    assert capture.snapshot()["error"] == "storage_limit"
    assert RecordingStore(capture.root).list() == []


def test_capture_rows_play_immediately_and_bypass_batch_release(manager, monkeypatch):
    capture, config = manager
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3)
    )
    store = RecordingStore(capture.root)
    target = capture.root / "captures" / ("d" * 32 + ".mp4")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fake-media")
    store.add_capture(
        "MATE-capture",
        "d" * 32,
        WATCH_SOURCE,
        datetime.now().isoformat(),
        2,
        "captures/" + target.name,
        target.stat().st_size,
        "0" * 64,
    )
    with TestClient(create_app(capture.root, TOKEN, config)) as client:
        assert client.post("/api/publication?enabled=true", headers=HEADERS).status_code == 405
        assert client.put("/api/publication?enabled=true", headers=HEADERS).status_code == 200
        library = client.get("/api/library", headers=HEADERS).json()
        item = next(row for row in library["items"] if row["id"] == "d" * 32)
        assert item["source"] == WATCH_SOURCE and item["playback_ready"] is True
        timeline = client.get(
            "/api/library/timeline?day=" + item["start_local"][:10], headers=HEADERS
        ).json()
        assert any(row["id"] == "d" * 32 and row["source"] == WATCH_SOURCE for row in timeline["items"])
        assert client.get("/api/recordings/" + "d" * 32 + "/preview").status_code == 401
        client.post("/api/playback/session", headers=HEADERS)
        response = client.get("/api/recordings/" + "d" * 32 + "/preview")
        assert response.status_code == 200 and response.content == target.read_bytes()


def live_source(hevc):
    def source(config, stop):
        while not stop.is_set():
            yield hevc
            stop.wait(0.05)

    return source


def get_event(viewer, event):
    kind, value = viewer.messages.get(timeout=10)
    assert kind == event
    return value


def wait_for_rows(root, minimum=1, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = RecordingStore(root).list()
        if len(rows) >= minimum:
            return rows
        time.sleep(0.05)
    return RecordingStore(root).list()


def test_multiple_viewers_share_one_recorder(manager, hevc_bytes):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    try:
        first = streams.subscribe()
        sink = streams.recorder
        assert sink is not None
        get_event(first, "init")
        second = streams.subscribe()
        assert streams.recorder is sink
        get_event(second, "init")
        get_event(first, "fragment")
        get_event(second, "fragment")
        streams.unsubscribe(first)
        assert streams.snapshot()["viewers"] == 1 and streams.thread.is_alive()
        streams.unsubscribe(second)
        streams.thread.join(timeout=8)
        assert not streams.thread.is_alive()
        rows = wait_for_rows(capture.root)
        assert len(rows) == 1 and rows[0]["source"] == WATCH_SOURCE
        validate_decode(capture.root / rows[0]["path"])
    finally:
        streams.close()


def test_real_viewer_handoff_continues_same_session(manager, hevc_bytes):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    try:
        streams.start_capture(30, SCHEDULED_SOURCE)
        sink = streams.recorder
        assert sink is not None and sink.source == SCHEDULED_SOURCE
        deadline = time.monotonic() + 8
        while sink.fragments < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        observer = streams.subscribe()
        get_event(observer, "init")
        get_event(observer, "fragment")
        assert sink.handoff_source == WATCH_SOURCE
        deadline = time.monotonic() + 8
        while sink.source != WATCH_SOURCE and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sink.source == WATCH_SOURCE and streams.recorder is sink
        streams.unsubscribe(observer)
        streams.thread.join(timeout=8)
        rows = wait_for_rows(capture.root)
        sources = {row["source"] for row in rows}
        assert SCHEDULED_SOURCE in sources
    finally:
        streams.close()


def test_secondary_view_releases_capture_session(manager, hevc_bytes):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    try:
        streams.start_capture(30, SCHEDULED_SOURCE)
        sink = streams.recorder
        deadline = time.monotonic() + 8
        while sink.fragments < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(CameraError) as error:
            streams.subscribe(1)
        assert error.value.code == "camera_busy"
        deadline = time.monotonic() + 8
        while sink.fragments < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        streams.thread.join(timeout=8)
        assert not streams.thread.is_alive() and not streams.camera_lock.locked()
        assert wait_for_rows(capture.root)
    finally:
        streams.close()


def test_capture_failure_does_not_stop_live(manager, hevc_bytes, monkeypatch):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)

    def refused(*args, **kwargs):
        raise CameraError("storage_limit", "full")

    monkeypatch.setattr(capture, "prepare", refused)
    try:
        viewer = streams.subscribe()
        get_event(viewer, "init")
        assert get_event(viewer, "fragment")
        assert streams.thread.is_alive() and not streams.error
        deadline = time.monotonic() + 5
        while not capture.snapshot()["paused"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert capture.snapshot()["paused"] is True
        streams.unsubscribe(viewer)
        streams.thread.join(timeout=8)
    finally:
        streams.close()


def test_existing_discovery_rows_remain_sd_source(manager):
    capture, _ = manager
    store = RecordingStore(capture.root)
    ids = store.discover(
        "MATE-capture", 0, [RecordingEvent(datetime(2026, 11, 16, 6), 0, 2, 1, 1)]
    )
    assert store.get(ids[0])["source"] == "sd"


def test_status_with_pending_sd_rows_uses_sqlite_rows(manager):
    capture, config = manager
    store = RecordingStore(capture.root)
    next_hour = server_capture.now_local().replace(minute=0, second=0, microsecond=0)
    store.discover(
        "MATE-capture", 0, [RecordingEvent(next_hour, 0, 2, 1, 1)]
    )
    with TestClient(create_app(capture.root, TOKEN, config)) as client:
        response = client.get("/api/status", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["backup"]["archive"]["pending"] == 1


def test_capture_cleanup_counts_combined_budget(manager, monkeypatch):
    capture, _ = manager
    store = RecordingStore(capture.root)
    directory = capture.root / "captures"
    directory.mkdir(parents=True)
    capture_file = directory / ("e" * 32 + ".mp4")
    capture_file.write_bytes(b"x" * 30)
    store.add_capture(
        "MATE-capture", "e" * 32, WATCH_SOURCE, "2026-01-01T00:00:00", 1,
        "captures/" + capture_file.name, 30, "0" * 64,
    )
    with store.connection() as db:
        db.execute(
            "UPDATE recordings SET saved_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            ("e" * 32,),
        )
    media = capture.root / "media" / ("f" * 32 + ".media")
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x" * 60)
    store.discover("MATE-capture", 0, [RecordingEvent(datetime(2026, 1, 2), 0, 2, 1, 1)])
    with store.connection() as db:
        db.execute(
            "UPDATE recordings SET path=?,bytes=?,state='verified',saved_at=? "
            "WHERE device_id='MATE-capture' AND id<>?",
            ("media/" + media.name, 60, "2026-01-02T00:00:00+00:00", "e" * 32),
        )
    BackupPolicy(capture.root).write("policy", {"mode": "server", "enabled": True})
    monkeypatch.setattr(storage, "SERVER_TOTAL_LIMIT", 100)
    monkeypatch.setattr(storage, "CAPTURE_LIMIT", 1000)

    removed = maintenance.cleanup_captures(capture.root, 20)
    assert removed["ids"] == ["e" * 32]
    assert not capture_file.exists()
    assert media.is_file()
    assert store.get("e" * 32)["state"] == "evicted"


def test_sd_cleanup_reclaims_captures_before_archive(manager, monkeypatch):
    capture, _ = manager
    store = RecordingStore(capture.root)
    directory = capture.root / "captures"
    directory.mkdir(parents=True)
    capture_file = directory / ("0" * 32 + ".mp4")
    capture_file.write_bytes(b"x" * 30)
    store.add_capture(
        "MATE-capture", "0" * 32, SCHEDULED_SOURCE, "2026-01-01T00:00:00", 1,
        "captures/" + capture_file.name, 30, "0" * 64,
    )
    with store.connection() as db:
        db.execute(
            "UPDATE recordings SET saved_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            ("0" * 32,),
        )
    media = capture.root / "media" / ("1" * 32 + ".media")
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x" * 60)
    sd_id = store.discover(
        "MATE-capture", 0, [RecordingEvent(datetime(2026, 1, 2), 0, 2, 1, 1)]
    )[0]
    with store.connection() as db:
        db.execute(
            "UPDATE recordings SET id=?,path=?,bytes=?,state='verified',saved_at=? WHERE id=?",
            ("1" * 32, "media/" + media.name, 60, server_capture.now_local().isoformat(), sd_id),
        )
    BackupPolicy(capture.root).write("policy", {"mode": "server", "enabled": True})
    monkeypatch.setattr(storage, "SERVER_TOTAL_LIMIT", 100)
    monkeypatch.setattr(storage, "CAPTURE_LIMIT", 1000)

    result = maintenance.cleanup(capture.root, 20)
    assert result["removed_recordings"] == 0
    assert store.get("0" * 32)["state"] == "evicted"
    assert store.get("1" * 32)["state"] == "verified"
    assert media.is_file()


def test_slow_capture_validation_does_not_delay_viewers(manager, hevc_bytes, monkeypatch):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    validation_started = threading.Event()
    release = threading.Event()

    def slow_decode(path):
        validation_started.set()
        assert release.wait(10)
        validate_decode(path)

    monkeypatch.setattr(server_capture, "validate_decode", slow_decode)
    monkeypatch.setattr(server_capture, "SEGMENT_SECONDS", 0)
    monkeypatch.setattr(server_capture, "CAPTURE_QUEUE_SIZE", 16)
    try:
        viewer = streams.subscribe()
        get_event(viewer, "init")
        assert validation_started.wait(10)
        started = time.monotonic()
        received = [get_event(viewer, "fragment") for _ in range(4)]
        assert len(received) == 4 and time.monotonic() - started < 5
        assert streams.thread.is_alive() and streams.snapshot()["recording"] is True
    finally:
        release.set()
        streams.unsubscribe(viewer)
        streams.thread.join(timeout=10)
        streams.close()


def test_capture_queue_overflow_pauses_without_stalling_live(manager, hevc_bytes, monkeypatch):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    validation_started = threading.Event()
    release = threading.Event()

    def slow_decode(path):
        validation_started.set()
        assert release.wait(10)
        validate_decode(path)

    monkeypatch.setattr(server_capture, "validate_decode", slow_decode)
    monkeypatch.setattr(server_capture, "SEGMENT_SECONDS", 0)
    monkeypatch.setattr(server_capture, "CAPTURE_QUEUE_SIZE", 16)
    received = []
    collecting = threading.Event()

    def collect():
        while not collecting.is_set():
            try:
                kind, _value = viewer.messages.get(timeout=0.2)
                received.append(kind)
            except queue.Empty:
                pass

    collector = None
    try:
        viewer = streams.subscribe()
        sink = streams.recorder
        collector = threading.Thread(target=collect, name="capture-viewer-drain", daemon=True)
        collector.start()
        assert validation_started.wait(10)
        deadline = time.monotonic() + 5
        while not sink.overflow and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sink.overflow is True
        assert len(received) >= 6
        assert streams.snapshot()["recording"] is True
    finally:
        collecting.set()
        if collector is not None:
            collector.join(timeout=2)
        release.set()
        streams.unsubscribe(viewer)
        streams.thread.join(timeout=10)
        streams.close()
    deadline = time.monotonic() + 5
    while capture.snapshot()["paused"] is False and time.monotonic() < deadline:
        time.sleep(0.05)
    assert capture.snapshot()["error"] == "capture_backlog"


def test_slow_finalization_does_not_hold_live_thread(manager, hevc_bytes, monkeypatch):
    capture, config = manager
    streams = StreamManager(config, threading.Lock(), live_source(hevc_bytes), capture=capture)
    capture.attach(streams)
    validation_started = threading.Event()
    release = threading.Event()

    def slow_decode(path):
        validation_started.set()
        assert release.wait(10)
        validate_decode(path)

    monkeypatch.setattr(server_capture, "validate_decode", slow_decode)
    monkeypatch.setattr(server_capture, "SEGMENT_SECONDS", 0)
    monkeypatch.setattr(server_capture, "CAPTURE_QUEUE_SIZE", 16)
    viewer = None
    try:
        viewer = streams.subscribe()
        get_event(viewer, "init")
        assert validation_started.wait(10)
        streams.unsubscribe(viewer)
        streams.thread.join(timeout=5)
        assert not streams.thread.is_alive()
    finally:
        release.set()
        if viewer is not None:
            streams.unsubscribe(viewer)
        streams.close()
