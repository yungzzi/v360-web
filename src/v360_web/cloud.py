"""Normal account login and import of one already identified, account-owned device.

Routes, form fields and AES behavior come from the analyzed V360 Pro 7.2.0 APK.
The account password is used only in memory; never accept it on the command line.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .apk import KNOWN_APK
from .config import Settings, read_private_json, write_private_json
from .errors import CameraError

ORIGIN = "https://birds-user.hapseemate.cn"
MAX_RESPONSE = 2 * 1024 * 1024


def _key(profile: dict) -> bytes:
    try:
        key = profile["web_key"].encode("ascii")
        if profile["source_apk_sha256"] != KNOWN_APK or len(key) != 16:
            raise ValueError
        return key
    except (KeyError, AttributeError, UnicodeError, ValueError) as exc:
        raise CameraError("cloud_profile", "Import the known APK's cloud profile first.") from exc


def encrypt_text(value: str, profile: dict) -> str:
    encoded = value.encode("utf-8")
    if not 1 <= len(encoded) <= 4096 or "\0" in value:
        raise CameraError("cloud_input", "Account input is empty or exceeds its supported size.")
    padder = padding.PKCS7(128).padder()
    data = padder.update(encoded) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_key(profile)), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")


def decrypt_text(value: str, profile: dict) -> str:
    try:
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError
        data = base64.b64decode("".join(value.split()).encode("ascii"), validate=True)
        if not data or len(data) % 16:
            raise ValueError
        decryptor = Cipher(algorithms.AES(_key(profile)), modes.ECB()).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CameraError("cloud_ciphertext", "Device parameters could not be decoded.") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward account information or tk headers to another endpoint.
        raise CameraError("cloud_redirect", "Unexpected account-server redirect; stopped.")


class CloudClient:
    def __init__(self, profile: dict, header_overrides: dict | None = None):
        _key(profile)
        self.profile = profile
        self.header_overrides = header_overrides or {}
        allowed = {
            "os",
            "sysver",
            "fonType",
            "operator",
            "timeZone",
            "sysCountryCode",
            "lang",
            "netType",
        }
        if not isinstance(self.header_overrides, dict) or any(
            name not in allowed
            or not isinstance(value, str)
            or len(value) > 128
            or not value.isascii()
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            for name, value in self.header_overrides.items()
        ):
            raise CameraError("cloud_headers", "Account client metadata is invalid.")
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, route: str, form: dict, token: str = "") -> dict:
        if route not in ("/user/loginUp", "/device/suit/queryDevs.html"):
            raise CameraError("cloud_route", "Unsupported account operation.")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "appkey": "com.dayunlinks.cloudbirds",
            "platform": "ANDROID",
            "version": "7.2.0",
            "tk": token,
            "ts": str(int(time.time() * 1000)),
            "lang": "zh",
            "os": "1",
            "channel": "oppo",
            "env": "pro",
            "appId": "1",
            "sysver": "34",
            "netType": "4g",
            "fonType": "v360-web",
            "operator": "",
            "timeZone": "Asia/Shanghai",
            "sysCountryCode": "CN",
        }
        headers.update(self.header_overrides)
        request = urllib.request.Request(
            ORIGIN + route,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=25) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise ValueError
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise TypeError
                return result
        except urllib.error.HTTPError as exc:
            raise CameraError(
                "cloud_http", "Account server returned an HTTP error.", exc.code
            ) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise CameraError(
                "cloud_io", "Account server is unavailable or returned invalid data."
            ) from exc

    def login(self, account: str, password: str, phone_uuid: str) -> dict:
        if not account or len(account) > 256 or "\0" in account:
            raise CameraError("cloud_input", "Enter a valid account locally.")
        response = self.request(
            "/user/loginUp",
            {
                "name": account,
                "pwd": encrypt_text(password, self.profile),
                "secret": "IOTCARE",
                "phoneUuid": phone_uuid,
            },
        )
        data = response.get("data")
        if (
            response.get("success") is False
            or not isinstance(data, dict)
            or not isinstance(data.get("token"), str)
            or not 8 <= len(data["token"]) <= 4096
            or not data["token"].isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in data["token"])
        ):
            # Do not echo arbitrary response bodies, which may contain credentials.
            raise CameraError("cloud_login", "Account login was not accepted; no automatic retry.")
        try:
            raw_id = data["id"]
            if isinstance(raw_id, bool) or not (
                isinstance(raw_id, int)
                or isinstance(raw_id, str)
                and raw_id.isascii()
                and raw_id.isdecimal()
            ):
                raise ValueError
            user_id = int(raw_id)
            if not 0 <= user_id <= 0xFFFFFFFF:
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise CameraError("cloud_identity", "Account user identifier is invalid.") from exc
        return {"token": data["token"], "user_id": user_id, "phone_uuid": phone_uuid}

    def devices(self, token: str) -> list:
        response = self.request(
            "/device/suit/queryDevs.html",
            {"did": "", "type": "", "token": token, "platform": "ANDROID"},
            token,
        )
        if response.get("success") is False or not isinstance(response.get("data"), list):
            raise CameraError("cloud_devices", "Account device list was not accepted.")
        return response["data"]


def _shared(value) -> bool:
    if value in (False, None, "0", "false", "False", ""):
        return False
    if value in (True, "1", "true", "True"):
        return True
    raise CameraError("cloud_device", "Device sharing flag is invalid.")


def import_device(config_path: Path, rows: list, identity: dict, profile: dict) -> None:
    _key(profile)
    cfg = read_private_json(config_path)
    expected = cfg["device"].get("did", "").strip()
    if not expected:
        raise CameraError("cloud_target", "Identify the camera DID locally before account import.")
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            did = decrypt_text(row.get("did"), profile)
        except CameraError:
            continue
        if did == expected:
            matched.append(row)
    if len(matched) != 1:
        raise CameraError(
            "cloud_target", "Expected exactly one account device matching the saved DID."
        )
    device = matched[0]
    cfg["device"].update(
        password=decrypt_text(device.get("pwd"), profile),
        user_id=identity["user_id"],
        shared=_shared(device.get("isSharedDev")),
    )
    temporary = config_path.with_name(config_path.name + ".cloud-" + secrets.token_hex(8))
    try:
        write_private_json(temporary, cfg)
        Settings.load(temporary)  # Validate before replacing the working configuration.
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)


def onboard(config_path: Path, account: str, password: str, progress=None) -> None:
    cfg = read_private_json(config_path)
    if not cfg.get("device", {}).get("did"):
        raise CameraError("cloud_target", "Identify the camera DID locally before account import.")
    profile = read_private_json(config_path.parent / "cloud-profile.json")
    client = CloudClient(profile, cfg.get("cloud_headers"))
    # Reuse this client's UUID on later logins; it is independent of the phone hardware ID.
    session_path = config_path.parent / "cloud-session.json"
    previous = read_private_json(session_path) if session_path.exists() else {}
    phone_uuid = previous.get("phone_uuid") or str(uuid.uuid4())
    if progress:
        progress("Logging in to the V360 Pro account service...")
    identity = client.login(account, password, phone_uuid)
    if progress:
        progress("Fetching account devices and matching the saved camera DID...")
    rows = client.devices(identity["token"])
    import_device(config_path, rows, identity, profile)
    temporary = session_path.with_name(session_path.name + ".new-" + secrets.token_hex(8))
    try:
        write_private_json(temporary, identity)
        temporary.replace(session_path)
    finally:
        temporary.unlink(missing_ok=True)
