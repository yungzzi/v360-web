from fastapi.testclient import TestClient

from v360_web.api import create_app

TOKEN = "test-local-shutdown-token-12345678901234567890"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_local_shutdown_requires_auth_and_same_origin(tmp_path):
    calls = []
    app = create_app(tmp_path, TOKEN, shutdown_callback=lambda: calls.append(True))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/local/shutdown").status_code == 401
        assert client.get("/api/status", headers=HEADERS).json()["local_shutdown_available"]
        assert (
            client.post(
                "/api/local/shutdown", headers={**HEADERS, "Origin": "https://unrelated.example"}
            ).status_code
            == 403
        )
        assert calls == []
        response = client.post(
            "/api/local/shutdown", headers={**HEADERS, "Origin": "http://testserver"}
        )
        assert response.status_code == 200 and response.json()["state"] == "shutting_down"
        assert calls == [True]


def test_remote_client_and_managed_app_do_not_offer_shutdown(tmp_path):
    calls = []
    app = create_app(tmp_path, TOKEN, shutdown_callback=lambda: calls.append(True))
    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        assert not client.get("/api/status", headers=HEADERS).json()["local_shutdown_available"]
        assert (
            client.post(
                "/api/local/shutdown", headers={**HEADERS, "X-Forwarded-For": "127.0.0.1"}
            ).status_code
            == 403
        )
        assert calls == []
    with TestClient(create_app(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        assert not client.get("/api/status", headers=HEADERS).json()["local_shutdown_available"]
        assert client.post("/api/local/shutdown", headers=HEADERS).status_code == 403
