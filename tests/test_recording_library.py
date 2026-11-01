import hashlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from v360_web import playback, progress, service, storage
from v360_web.api import create_app
from v360_web.config import write_private_json
from v360_web.errors import CameraError
from v360_web.jobs import JobStore
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore

TOKEN = "test-library-token-1234567890123456789012345678"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def prepared(tmp_path, media_bytes, monkeypatch):
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    root = tmp_path / "data"
    store = RecordingStore(root)
    key = store.discover("MATE-test", 0, [RecordingEvent(datetime(2026, 10, 29, 12), 0, 2, 1, 1)])[
        0
    ]
    source = root / "media" / (key + ".media")
    source.parent.mkdir()
    source.write_bytes(media_bytes)
    store.update(
        key,
        "verified",
        path="media/" + source.name,
        bytes=len(media_bytes),
        sha256=hashlib.sha256(media_bytes).hexdigest(),
    )
    config = tmp_path / ".private/device.json"
    write_private_json(config.parent / "sdk-profiles.json", {"initialize": "unused-test-profile"})
    write_private_json(
        config,
        {
            "device": {"did": "MATE-test", "password": "example"},
            "bridge": {"adapter": "native-arm64"},
        },
    )
    return root, store, key, config


def test_library_date_pagination_and_catalog_boundaries(prepared):
    root, store, key, config = prepared
    store.discover("MATE-other", 0, [RecordingEvent(datetime(2026, 10, 29, 13), 0, 2, 1, 1)])
    store.discover("MATE-test", 0, [RecordingEvent(datetime(2026, 10, 30, 12), 0, 2, 1, 1)])
    store.save_catalog(
        "MATE-test",
        datetime(2026, 10, 29),
        datetime(2026, 10, 29, 23, 59, 59),
        {"metadata_complete": False, "invalid_records": 1},
    )
    with TestClient(create_app(root, TOKEN, config)) as client:
        assert client.get("/api/library").status_code == 401
        response = client.get("/api/library?day=2026-10-29&limit=1", headers=HEADERS).json()
        assert response["total"] == 1 and response["items"][0]["id"] == key
        assert "device_id" not in response["items"][0] and "path" not in response["items"][0]
        assert response["catalog"]["metadata_complete"] is False
        assert client.get("/api/library?day=bad", headers=HEADERS).status_code == 422
        assert client.get("/api/library?offset=-1", headers=HEADERS).status_code == 422
    assert store.catalog("MATE-test", date(2026, 10, 30)) is None


def test_browser_preview_media_session_range_and_logout(prepared):
    root, store, key, config = prepared
    assert playback.prepare(config, key)["state"] == "ready"
    assert playback.prepare(config, key)["already_present"]
    path = playback.ready(root, key)
    content = path.read_bytes()
    with TestClient(create_app(root, TOKEN, config)) as client:
        route = f"/api/recordings/{key}/preview"
        assert client.get(route).status_code == 401
        response = client.post("/api/playback/session", headers=HEADERS)
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert client.get(route).content == content
        assert client.get(route, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        response = client.get(route, headers={"Range": "bytes=0-31"})
        assert response.status_code == 206 and response.content == content[:32]
        # The media cookie cannot authorize camera operations or metadata.
        assert client.get("/api/library").status_code == 401
        assert client.post("/api/jobs/list", json={}).status_code == 401
        assert client.delete("/api/playback/session", headers=HEADERS).status_code == 200
        assert client.get(route).status_code == 401
    store.update(key, "verified", sha256="different-source")
    assert playback.ready(root, key) is None


def test_preview_rejects_unverified_and_outside_target(prepared, tmp_path):
    root, store, key, config = prepared
    store.update(key, "downloaded_unverified")
    with pytest.raises(CameraError):
        playback.prepare(config, key)
    store.update(key, "verified")
    directory = root / "previews"
    directory.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"preserve")
    (directory / (key + ".partial.mp4")).symlink_to(outside)
    with pytest.raises(CameraError):
        playback.prepare(config, key)
    assert outside.read_bytes() == b"preserve"


def test_storage_budget_and_free_reserve_preserve_existing_files(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    file = media / "existing.media"
    file.write_bytes(b"1234567")
    monkeypatch.setattr(storage, "ARCHIVE_LIMIT", 10)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    with pytest.raises(CameraError) as error:
        storage.admit(tmp_path, 4, "archive")
    assert error.value.code == "storage_limit" and file.read_bytes() == b"1234567"
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=storage.FREE_RESERVE - 1)
    )
    with pytest.raises(CameraError) as error:
        storage.admit(tmp_path, 0, "cache")
    assert error.value.code == "disk_reserve"


def test_persisted_jobs_recover_interrupted_without_restarting_work(tmp_path):
    jobs = JobStore(tmp_path)
    jobs.update_job("done", state="complete", result={"saved": True})
    jobs.update_job("interrupted", state="running", progress={"stage": "downloading", "bytes": 42})
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        value = client.get("/api/jobs/interrupted", headers=HEADERS).json()
        assert value["state"] == "failed" and value["error"] == "interrupted"
        assert value["progress"]["bytes"] == 42
        assert client.get("/api/jobs/done", headers=HEADERS).json()["result"]["saved"]


def test_download_job_persists_progress(tmp_path, monkeypatch):
    def download(config, key):
        progress.report("downloading", bytes=30, total=100)
        return {"id": key, "state": "verified"}

    monkeypatch.setattr(service, "download", download)
    app = create_app(tmp_path, TOKEN, tmp_path / "not-read.json")
    with TestClient(app) as client:
        job = client.post("/api/jobs/download/example", headers=HEADERS).json()
    value = JobStore(tmp_path).get_job(job["id"])
    assert value["state"] == "complete" and value["progress"] == {
        "stage": "downloading",
        "bytes": 30,
        "total": 100,
    }


def test_downloaded_file_validation_resumes_without_camera(prepared, monkeypatch):
    _root, store, key, config = prepared
    store.update(key, "downloaded_unverified")

    def forbidden(*args):
        raise AssertionError("Existing complete file must not be downloaded again")

    monkeypatch.setattr(service, "CameraClient", forbidden)
    assert service.download(config, key)["rechecked_local_file"]
    assert store.get(key)["state"] == "verified"


def test_timeline_uses_whole_day_not_list_page_and_hides_private_fields(prepared, monkeypatch):
    root, store, key, config = prepared
    for minute in range(20):
        store.discover(
            "MATE-test", 0, [RecordingEvent(datetime(2026, 10, 29, 13, minute), 0, 2, 1, 15)]
        )
    store.discover("MATE-test", 0, [RecordingEvent(datetime(2026, 10, 30, 0), 0, 2, 1, -1)])
    store.discover("MATE-other", 0, [RecordingEvent(datetime(2026, 10, 29, 14), 0, 2, 1, 30)])

    def forbidden(*args, **kwargs):
        raise AssertionError("Timeline must not connect to the camera or process media")

    monkeypatch.setattr(service, "CameraClient", forbidden)
    with TestClient(create_app(root, TOKEN, config)) as client:
        assert client.get("/api/library/timeline?day=2026-10-29").status_code == 401
        assert client.get("/api/library/timeline?day=bad", headers=HEADERS).status_code == 422
        value = client.get("/api/library/timeline?day=2026-10-29", headers=HEADERS).json()
        assert value["total"] == 21 and len(value["items"]) == 21
        assert not value["truncated"]
        assert value["days"] == [
            {"day": "2026-10-29", "count": 21},
            {"day": "2026-10-30", "count": 1},
        ]
        assert value["items"][0]["id"] == key and value["items"][0]["original_ready"]
        assert value["items"][0]["second"] == 43200
        assert value["items"][-1]["duration_hint"] == 15
        for item in value["items"]:
            assert not {"device_id", "path", "sha256"} & item.keys()
        other_day = client.get("/api/library/timeline?day=2026-10-30", headers=HEADERS).json()
        assert other_day["items"][0]["duration_hint"] is None
        empty = client.get("/api/library/timeline?day=2026-11-01", headers=HEADERS).json()
        assert empty["total"] == 0 and empty["days"] == []


def test_playback_reports_real_conversion_progress_and_timings(prepared):
    root, _store, key, config = prepared
    values = []
    handle = progress.callback.set(values.append)
    try:
        result = playback.prepare(config, key)
    finally:
        progress.callback.reset(handle)
    assert playback.ready(root, key) is not None
    assert result["conversion_seconds"] > 0 and result["validation_seconds"] > 0
    updates = [v for v in values if "total_seconds" in v]
    assert updates and all(0 <= v["seconds"] <= v["total_seconds"] for v in updates)
    assert any(v["stage"] == "validating_playback" for v in values)


@pytest.mark.parametrize("value", ["N/A", "bad", "00:00:nan", "00:00:-1", ""])
def test_playback_progress_tolerates_invalid_output(value):
    assert playback.parse_progress_time(value, 2.0) == 2.0


def test_playback_progress_time_is_monotonic():
    assert playback.parse_progress_time("00:01:03.250000") == 63.25
    assert playback.parse_progress_time("00:00:01", 2.0) == 2.0


def test_window_preview_is_separate_and_requires_media_auth(prepared):
    root, _store, key, config = prepared
    result = playback.prepare(config, key, 0)
    assert not result["full"] and result["start"] == 0
    assert playback.ready(root, key) is None
    assert playback.ready(root, key, 0) is not None
    with TestClient(create_app(root, TOKEN, config)) as client:
        route = f"/api/recordings/{key}/window?start=0"
        assert client.get(route).status_code == 401
        assert client.get(route, headers=HEADERS).status_code == 200
        assert client.get(route.replace("start=0", "start=1"), headers=HEADERS).status_code == 422
        assert client.get(route.replace("start=0", "start=30"), headers=HEADERS).status_code == 404
    assert playback.prepare(config, key, 0)["seconds"] == result["seconds"]
    with pytest.raises(CameraError):
        playback.prepare(config, key, 30)


def test_long_recording_window_seek_tail_and_cache_cleanup(prepared, tmp_path, monkeypatch):
    import subprocess

    from v360_web import maintenance
    from v360_web.media import find_tool, probe

    root, store, key, config = prepared
    source = store.media_path(key)
    long_file = tmp_path / "long.mp4"
    subprocess.run(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-stream_loop",
            "64",
            "-i",
            str(source),
            "-c",
            "copy",
            str(long_file),
        ],
        check=True,
        timeout=30,
    )
    source.write_bytes(long_file.read_bytes())
    store.update(
        key,
        "verified",
        bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    middle = playback.prepare(config, key, 30)
    tail = playback.prepare(config, key, 60)
    assert middle["start"] == 30 and middle["seconds"] == 30
    assert tail["start"] == 60 and abs(tail["seconds"] - 5) < 0.25
    assert abs(float(probe(playback.ready(root, key, 30))["format"]["duration"]) - 30) < 0.25
    assert playback.ready(root, key) is None
    store.lease(key, -1)
    monkeypatch.setattr(storage, "CACHE_LIMIT", 1)
    maintenance.cleanup(root, kind="cache")
    assert playback.ready(root, key, 30) is None
    assert playback.ready(root, key, 60) is None
    assert store.media_path(key) == source


def test_prefetch_yields_to_foreground_and_duplicate_cannot_displace_it(prepared, monkeypatch):
    import threading
    import time

    root, _store, key, config = prepared
    entered = threading.Event()
    cancelled = threading.Event()

    def prepare(config, key, start=None):
        if start == 30:
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    progress.check_cancelled()
                except CameraError:
                    cancelled.set()
                    raise
                time.sleep(0.01)
            raise AssertionError("Background work was not cancelled")
        return {"start": start}

    monkeypatch.setattr(playback, "prepare", prepare)
    with TestClient(create_app(root, TOKEN, config, start_backup=False)) as client:
        assert client.post(f"/api/jobs/prefetch/{key}?start=30").status_code == 401
        bg = client.post(f"/api/jobs/prefetch/{key}?start=30", headers=HEADERS).json()
        assert entered.wait(2)
        assert client.post(f"/api/jobs/prefetch/{key}?start=60", headers=HEADERS).status_code == 409
        fg = client.post(f"/api/jobs/playback/{key}?start=60", headers=HEADERS)
        assert fg.status_code == 202 and cancelled.is_set()
    assert JobStore(root).get_job(bg["id"])["state"] == "failed"
    assert JobStore(root).get_job(fg.json()["id"])["result"]["start"] == 60
