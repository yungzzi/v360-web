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
