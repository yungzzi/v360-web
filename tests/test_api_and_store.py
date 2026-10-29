from datetime import datetime

from fastapi.testclient import TestClient

from v360_web.api import create_app
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore

TOKEN = "web-test-token-not-a-real-secret-123456789"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def event():
    return RecordingEvent(datetime(2026, 10, 29, 11, 28), 0, 2, 1, 60)


def test_rediscovery_is_idempotent_and_preserves_verified_state(tmp_path):
    store = RecordingStore(tmp_path)
    key = store.discover("MATE-test", 2, [event()])[0]
    store.update(key, "verified", path="media/test.mp4", bytes=42, sha256="test-digest")
    assert store.discover("MATE-test", 2, [event()]) == [key]
    assert len(store.list()) == 1
    assert store.get(key)["state"] == "verified"


def test_web_authentication_covers_metadata_and_media(tmp_path):
    store = RecordingStore(tmp_path)
    key = store.discover("MATE-test", 2, [event()])[0]
    media = tmp_path / "one.media"
    media.write_bytes(b"test fixture")
    store.update(key, "verified", path=media.name)
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/recordings").status_code == 401
        assert client.get(f"/api/recordings/{key}/file").status_code == 401
        listing = client.get("/api/recordings", headers=HEADERS).json()
        assert "path" not in listing[0] and "device_id" not in listing[0]
        assert client.get(f"/api/recordings/{key}/file", headers=HEADERS).content == b"test fixture"


def test_unverified_and_outside_paths_are_not_served(tmp_path):
    store = RecordingStore(tmp_path)
    key = store.discover("MATE-test", 2, [event()])[0]
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        store.update(key, "downloaded_unverified", path="anything")
        assert client.get(f"/api/recordings/{key}/file", headers=HEADERS).status_code == 404
        store.update(key, "verified", path="../outside")
        assert client.get(f"/api/recordings/{key}/file", headers=HEADERS).status_code == 404


def test_api_rejects_ambiguous_device_time_and_unconfigured_jobs(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        request = {"channel": 2, "start": "2026-10-29T00:00:00Z", "end": "2026-10-30T00:00:00Z"}
        assert client.post("/api/jobs/list", headers=HEADERS, json=request).status_code == 422
        request.update(start="2026-10-29T00:00:00", end="2026-10-30T00:00:00")
        assert client.post("/api/jobs/list", headers=HEADERS, json=request).status_code == 503
