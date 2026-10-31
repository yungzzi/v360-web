from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from v360_web.api import create_app
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore

TOKEN = "subpath-test-token-12345678901234567890123"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_subpath_assets_cookie_and_file_are_scoped(tmp_path):
    store = RecordingStore(tmp_path)
    key = store.discover("MATE-test", 0, [RecordingEvent(datetime(2026, 10, 31, 12), 0, 2, 1, 1)])[
        0
    ]
    path = tmp_path / "media" / f"{key}.media"
    path.parent.mkdir()
    path.write_bytes(b"preserved-recording")
    store.update(key, "verified", path="media/" + path.name, bytes=path.stat().st_size)
    service = create_app(tmp_path, TOKEN)
    outer = FastAPI()
    outer.mount("/v360", service)
    with TestClient(outer) as client:
        assert client.get("/v360/").status_code == 200
        assert 'href="assets/style.css"' in client.get("/v360/").text
        assert client.get("/v360/assets/app.js").status_code == 200
        assert client.get("/v360/api/status", headers=HEADERS).status_code == 200
        response = client.post("/v360/api/playback/session", headers=HEADERS)
        assert "Path=/v360/api/" in response.headers["set-cookie"]
        assert client.get(f"/v360/api/recordings/{key}/file").content == b"preserved-recording"
        response = client.delete("/v360/api/playback/session", headers=HEADERS)
        assert "Path=/v360/api/" in response.headers["set-cookie"]
        assert client.get(f"/v360/api/recordings/{key}/file").status_code == 401
        assert client.get("/api/status").status_code == 404


def test_stripped_proxy_path_and_direct_health_still_route(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), root_path="/v360") as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/api/status", headers=HEADERS).status_code == 200
        assert client.get("/assets/style.css").status_code == 200
