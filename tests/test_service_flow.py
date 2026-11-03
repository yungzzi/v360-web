import hashlib
from datetime import datetime

import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient

from v360_web import service
from v360_web.api import create_app
from v360_web.config import write_private_json
from v360_web.errors import CameraError
from v360_web.store import RecordingStore


@pytest.fixture(autouse=True)
def simulated_storage_capacity(monkeypatch):
    # /tmp is a 4 GiB tmpfs on this host. These are transport integration tests;
    # exercise admission-limit failures separately in test_recording_library.py.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "v360_web.storage.shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3)
    )


def config(root, port):
    private = root / "private"
    write_private_json(private / "sdk-profiles.json", {"initialize": "test-only-sdk-profile"})
    path = private / "device.json"
    write_private_json(
        path,
        {
            "bridge": {"host": "127.0.0.1", "port": port, "token": TOKEN},
            "device": {"did": "MATE-test", "password": "test-password", "connect_mode": 0},
            "request_timeout": 2,
            "transfer_timeout": 5,
        },
    )
    return path


def test_discover_download_validate_and_serve_complete_flow(bridge_server, media_bytes, tmp_path):
    with bridge_server() as (port, state):
        path = config(tmp_path, port)
        discovered = service.discover(path, 2, datetime(2026, 10, 29), datetime(2026, 10, 30))
        key = discovered["ids"][0]
        result = service.download(path, key)
        assert result["state"] == "verified"
        assert result["sha256"] == hashlib.sha256(media_bytes).hexdigest()
        assert state["closed"] == 2
        assert service.download(path, key)["already_present"]
        assert state["closed"] == 2
    with TestClient(create_app(tmp_path / "data", TOKEN)) as client:
        response = client.get(
            f"/api/recordings/{key}/file", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200 and response.content == media_bytes


def test_failed_transfer_never_becomes_a_served_recording(bridge_server, tmp_path):
    with bridge_server("gap") as (port, state):
        path = config(tmp_path, port)
        key = service.discover(path, 2, datetime(2026, 10, 29), datetime(2026, 10, 30))["ids"][0]
        with pytest.raises(CameraError):
            service.download(path, key)
        store = RecordingStore(tmp_path / "data")
        assert store.get(key)["state"] == "failed"
        assert store.media_path(key) is None
        assert (tmp_path / "data/media" / f"{key}.part").is_file()
        assert state["closed"] == 2


def test_batch_reuses_session_and_releases_before_validation(bridge_server, media_bytes, tmp_path):
    with bridge_server() as (port, state):
        path = config(tmp_path, port)
        key = service.discover(path, 2, datetime(2026, 10, 29), datetime(2026, 10, 30))["ids"][0]
        store = RecordingStore(tmp_path / "data")
        before = state["connect_calls"]
        batch = service.DownloadBatch(path)
        try:
            for _ in range(3):
                store.update(key, "discovered")
                result = batch.download(key)
                assert result["state"] == "downloaded_unverified"
        finally:
            batch.close()
        assert state["connect_calls"] - before == 1
        assert state["closed"] == 2  # catalog and batch
        result = service.download(path, key, local_only=True)
        assert result["rechecked_local_file"]
        assert state["connect_calls"] - before == 1


def test_invalid_local_original_returns_to_network_queue(bridge_server, tmp_path):
    with bridge_server() as (port, state):
        path = config(tmp_path, port)
        key = service.discover(path, 2, datetime(2026, 10, 29), datetime(2026, 10, 30))["ids"][0]
        store = RecordingStore(tmp_path / "data")
        bad = store.root / "media" / (key + ".media")
        bad.parent.mkdir(exist_ok=True)
        content = b"invalid-camera-transfer"
        bad.write_bytes(content)
        store.update(
            key,
            "downloaded_unverified",
            path=str(bad.relative_to(store.root)),
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        before = state["connect_calls"]
        with pytest.raises(CameraError) as exc:
            service.download(path, key, local_only=True)
        assert exc.value.code == "local_media_invalid"
        assert state["connect_calls"] == before
        assert store.get(key)["state"] == "failed"
        assert service.download(path, key)["state"] == "verified"
        assert state["connect_calls"] == before + 1


@pytest.mark.parametrize("duration", ["0.000000", "nan", None, "invalid", "-1"])
def test_zero_duration_original_cannot_pass_recording_validation(duration):
    from v360_web.media import validate_recording

    with pytest.raises(CameraError) as error:
        validate_recording(
            {
                "format": {"duration": duration},
                "streams": [{"codec_type": "video", "nb_frames": "1"}],
            }
        )
    assert error.value.code == "media_incomplete"
