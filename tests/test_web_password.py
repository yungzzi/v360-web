from fastapi.testclient import TestClient

from v360_web.api import create_app
from v360_web.sessions import COOKIE

TOKEN = "test-chinese-password-token-12345678901234567890"
PASSWORD = "这是一段用于测试的中文密码"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_unicode_password_change_login_restart_and_api_compatibility(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        assert client.post('/api/auth/login', json={"password": TOKEN}).status_code == 200
        old = client.cookies.get(COOKIE)
        assert client.post('/api/auth/password', json={"current_password": TOKEN, "new_password": PASSWORD}).status_code == 200
        assert client.get('/api/status').status_code == 401
        assert client.post('/api/auth/login', json={"password": TOKEN}).status_code == 401
        assert client.post('/api/auth/login', json={"password": PASSWORD, "remember": True}).status_code == 200
        assert client.get('/api/status').status_code == 200
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        assert client.post('/api/auth/login', json={"password": PASSWORD}).status_code == 200
        assert client.get('/api/status', headers=HEADERS).status_code == 200
        client.cookies.set(COOKIE, old, path='/api/')
        assert client.get('/api/status').status_code == 401
    assert PASSWORD.encode() not in (tmp_path/'recordings.sqlite3').read_bytes()


def test_password_limits_origin_and_throttle(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        assert client.post('/api/auth/password', json={"current_password": TOKEN, "new_password": PASSWORD}).status_code == 401
        assert client.post('/api/auth/password', headers={**HEADERS, "Origin":"https://elsewhere.example"}, json={"current_password":TOKEN,"new_password":PASSWORD}).status_code == 403
        assert client.post('/api/auth/password', headers=HEADERS, json={"current_password":TOKEN,"new_password":"短密码"}).status_code == 422
        for _ in range(5):
            assert client.post('/api/auth/login', json={"password":"不正确的密码"}).status_code == 401
        assert client.post('/api/auth/login', json={"password":TOKEN}).status_code == 429
        assert client.get('/api/status', headers=HEADERS).status_code == 200
