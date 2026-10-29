import io
import json
import sys
import urllib.parse
import urllib.request
import urllib.response
from email.message import Message

import pytest

from v360_web import cloud
from v360_web.apk import KNOWN_APK
from v360_web.cli import main
from v360_web.config import read_private_json, write_private_json
from v360_web.errors import CameraError

PROFILE = {"source_apk_sha256": KNOWN_APK, "web_key": "0123456789abcdef"}
DEVICE = "BFEP-test-camera"
ACCOUNT_PASSWORD = "fake-account-password-never-store"
DEVICE_PASSWORD = "fake-device-password"
TOKEN = "fake-account-token-1234567890"


def configured(tmp_path):
    path = tmp_path / "device.json"
    write_private_json(
        path,
        {
            "device": {"did": DEVICE, "connect_mode": 94},
            "bridge": {"token": "fake-existing-bridge-token-1234567890"},
            "cloud_headers": {"os": "8", "sysver": "36", "fonType": "fake-test-phone"},
        },
    )
    write_private_json(tmp_path / "cloud-profile.json", PROFILE)
    write_private_json(
        tmp_path / "sdk-profiles.json",
        {"initialize": "fake-sdk-init", "server_profiles": {"NEW_INIT_KEY": "fake-server-key"}},
    )
    return path


def device_row(did=DEVICE):
    return {
        "did": cloud.encrypt_text(did, PROFILE),
        "pwd": cloud.encrypt_text(DEVICE_PASSWORD, PROFILE),
        "isSharedDev": "0",
    }


@pytest.mark.parametrize(
    ("text", "encoded"),
    [
        ("password", "R4lDIBO/32oRLZSjtsPrGQ=="),
        ("0123456789abcdef", "cnJ+iB7c/QEApxhoeQm1ZTdyIuBhqSTFkc2cJ+oWPtQ="),
        ("中文ABC", "ttGA497dnqJAq5IDjf6hoQ=="),
        (
            "long-test-password-0123456789012345678901234567890123456789",
            "gIdOa8NX/vdZCBE0jm7LiS/oAnJWUhy7XayEo7bqNLdKyd3Xd2F2mfuEWaMnfN9Ukf8K+WDzwarmYvH+1Foo8A==",
        ),
    ],
)
def test_cipher_matches_independent_jdk_vectors(text, encoded):
    # Generated separately with JDK 17 Cipher(AES/ECB/PKCS5Padding), UTF-8 input.
    assert cloud.encrypt_text(text, PROFILE) == encoded
    assert cloud.decrypt_text(encoded[:20] + "\n" + encoded[20:], PROFILE) == text


class FakeOpener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(json.dumps(next(self.replies)).encode())


def test_normal_login_imports_only_saved_device_without_printing_or_storing_account_password(
    tmp_path, monkeypatch, capsys
):
    path = configured(tmp_path)
    bridge_before = read_private_json(path)["bridge"]
    opener = FakeOpener(
        [
            {"code": 200, "data": {"token": TOKEN, "id": 12345}},
            {"code": 200, "data": [device_row("BFEP-unrelated-camera"), device_row()]},
        ]
    )
    monkeypatch.setattr(cloud.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr("builtins.input", lambda _: "fake-account@example.invalid")
    monkeypatch.setattr("getpass.getpass", lambda _: ACCOUNT_PASSWORD)
    monkeypatch.setattr(sys, "argv", ["v360", "--config", str(path), "cloud-login"])
    assert main() == 0
    cfg = read_private_json(path)
    assert cfg["device"]["password"] == DEVICE_PASSWORD
    assert cfg["device"]["user_id"] == 12345
    assert cfg["device"]["shared"] is False
    assert cfg["device"]["connect_mode"] == 94
    assert cfg["bridge"] == bridge_before
    session = read_private_json(tmp_path / "cloud-session.json")
    assert session["token"] == TOKEN
    forms = [
        urllib.parse.parse_qs(r.data.decode(), keep_blank_values=True) for r in opener.requests
    ]
    assert forms[0]["pwd"] == [cloud.encrypt_text(ACCOUNT_PASSWORD, PROFILE)]
    assert forms[0]["secret"] == ["IOTCARE"]
    assert forms[1]["token"] == [TOKEN]
    assert opener.requests[1].get_header("Tk") == TOKEN
    assert opener.requests[0].get_header("Os") == "8"
    assert opener.requests[0].get_header("Env") == "pro"
    assert opener.requests[0].get_header("Sysver") == "36"
    assert all(r.full_url.startswith(cloud.ORIGIN + "/") for r in opener.requests)
    printed = capsys.readouterr().out
    assert all(
        secret not in printed for secret in (TOKEN, DEVICE_PASSWORD, ACCOUNT_PASSWORD, DEVICE)
    )
    for p in tmp_path.glob("*.json"):
        assert ACCOUNT_PASSWORD not in p.read_text()
        assert "fake-account@example.invalid" not in p.read_text()
        assert p.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "rows", [[], [device_row("BFEP-unrelated-camera")], [device_row(), device_row()]]
)
def test_missing_or_ambiguous_target_preserves_existing_configuration(tmp_path, rows):
    path = configured(tmp_path)
    before = path.read_bytes()
    with pytest.raises(CameraError, match="exactly one"):
        cloud.import_device(path, rows, {"user_id": 12345}, PROFILE)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("device.json.cloud-*"))


def test_invalid_target_password_preserves_existing_configuration(tmp_path):
    path = configured(tmp_path)
    before = path.read_bytes()
    row = device_row()
    row["pwd"] = "invalid encrypted payload"
    with pytest.raises(CameraError):
        cloud.import_device(path, [row], {"user_id": 12345}, PROFILE)
    assert path.read_bytes() == before


def test_redirect_cannot_forward_credentials_to_another_host():
    client = cloud.CloudClient(PROFILE)
    requests = []

    class RedirectServer(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, request):
            requests.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://example.invalid/steal"
            response = urllib.response.addinfourl(io.BytesIO(), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    client.opener.add_handler(RedirectServer())
    with pytest.raises(CameraError, match="redirect"):
        client.login("fake-account", ACCOUNT_PASSWORD, "fake-uuid")
    assert requests == [cloud.ORIGIN + "/user/loginUp"]


def test_failed_login_does_not_echo_server_secrets(tmp_path, monkeypatch, capsys):
    path = configured(tmp_path)
    before = path.read_bytes()
    opener = FakeOpener(
        [{"success": False, "message": ACCOUNT_PASSWORD + TOKEN, "data": {"token": TOKEN}}]
    )
    monkeypatch.setattr(cloud.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr("builtins.input", lambda _: "fake-account")
    monkeypatch.setattr("getpass.getpass", lambda _: ACCOUNT_PASSWORD)
    monkeypatch.setattr(sys, "argv", ["v360", "--config", str(path), "cloud-login"])
    assert main() == 2
    output = capsys.readouterr().out
    assert ACCOUNT_PASSWORD not in output and TOKEN not in output
    assert len(opener.requests) == 1  # No password guessing or automatic retry.
    assert path.read_bytes() == before
    assert not (tmp_path / "cloud-session.json").exists()
