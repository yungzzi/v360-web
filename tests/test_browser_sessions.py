import queue
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from v360_web.api import create_app
from v360_web.sessions import COOKIE, SessionStore

TOKEN = "browser-session-test-token-12345678901234567890"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def login(client, remember=True, prefix=""):
    return client.post(prefix + "/api/auth/login", headers=HEADERS, json={"remember": remember})


def test_session_survives_restart_and_stores_no_raw_secrets(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), base_url="https://testserver") as client:
        response = login(client)
        cookie = client.cookies.get(COOKIE)
        assert response.status_code == 200
        header = response.headers["set-cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=strict", "Max-Age=604800", "Path=/api/"):
            assert part in header
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/playback/session").json() == {"ready": True}
        assert "set-cookie" not in client.post("/api/playback/session").headers
    database = (tmp_path / "recordings.sqlite3").read_bytes()
    assert cookie.encode() not in database and TOKEN.encode() not in database
    with TestClient(create_app(tmp_path, TOKEN), base_url="https://testserver") as client:
        client.cookies.set(COOKIE, cookie, path="/api/")
        assert client.get("/api/status").status_code == 200


def test_logout_revokes_copied_cookie_and_rotation_invalidates(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        login(client)
        cookie = client.cookies.get(COOKIE)
        assert client.delete("/api/auth/session").status_code == 200
        client.cookies.set(COOKIE, cookie, path="/api/")
        assert client.get("/api/status").status_code == 401
        client.cookies.clear()
        login(client)
        cookie = client.cookies.get(COOKIE)
    with TestClient(create_app(tmp_path, TOKEN + "changed")) as client:
        client.cookies.set(COOKIE, cookie, path="/api/")
        assert client.get("/api/status").status_code == 401


def test_expiration_and_nonremembered_cookie(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        response = login(client, False)
        assert "Max-Age" not in response.headers["set-cookie"]
        now = __import__("time").time()
        monkeypatch.setattr("v360_web.sessions.time.time", lambda: now + 12 * 3600 + 1)
        assert client.get("/api/status").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://untrusted.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cookie_cross_origin_rejected_including_login_logout(tmp_path, headers):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        login(client)
        assert client.get("/api/status", headers=headers).status_code == 403
        assert client.delete("/api/auth/session", headers=headers).status_code == 403
        assert (
            client.post("/api/auth/login", headers={**HEADERS, **headers}, json={}).status_code
            == 403
        )
        assert client.get("/api/status").status_code == 200


def test_session_cookie_scoped_to_subpath(tmp_path):
    outer = FastAPI()
    outer.mount("/v360", create_app(tmp_path, TOKEN))
    with TestClient(outer) as client:
        response = login(client, prefix="/v360")
        assert "Path=/v360/api/" in response.headers["set-cookie"]
        assert (
            client.get("/v360/api/status", headers={"Origin": "http://testserver"}).status_code
            == 200
        )
        assert client.delete("/v360/api/auth/session").status_code == 200
        assert client.get("/v360/api/status").status_code == 401


def test_session_capacity_does_not_evict_other_devices(tmp_path):
    store = SessionStore(tmp_path, TOKEN)
    keys = [store.issue(True) for _ in range(32)]
    assert all(keys) and store.issue(True) is None
    assert all(store.valid(key) for key in keys)
    replacement = store.issue(True, keys[0])
    assert replacement and not store.valid(keys[0])


def test_cookie_websocket_and_logout_releases_viewer(tmp_path, monkeypatch):
    app = create_app(tmp_path, TOKEN)
    viewer = SimpleNamespace(identity="test", closed=threading.Event(), messages=queue.Queue())
    unsubscribed = threading.Event()
    monkeypatch.setattr(app.state.streams, "subscribe", lambda lens=0: viewer)
    monkeypatch.setattr(app.state.streams, "unsubscribe", lambda v: unsubscribed.set())
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect(
            "/api/live/ws", headers={"Origin": "http://testserver"}
        ) as ws:
            ws.send_json({})
            assert ws.receive_json()["viewer_id"] == "test"
            client.delete("/api/auth/session")
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008
        assert unsubscribed.wait(2)
        with client.websocket_connect(
            "/api/live/ws", headers={"Origin": "http://testserver"}
        ) as ws:
            ws.send_json({})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_login_cannot_be_minted_from_cookie_alone(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        login(client)
        assert client.post("/api/auth/login", json={"remember": True}).status_code == 401
