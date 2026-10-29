from __future__ import annotations

import json
import math
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CameraError

PREFIX_PROFILES = {
    **dict.fromkeys(("MATE", "KEEP", "ZSYAS", "MNET"), None),
    **dict.fromkeys(("BFEP", "CFET", "CFMT", "CFCD", "NFET"), "NEW_INIT_KEY"),
    **dict.fromkeys(("BFEL", "CFEO", "CFMS", "CFCS", "NFES"), "NEW_INIT_2_KEY"),
    **dict.fromkeys(("BLPS", "BLPM", "MSPC", "MMPC"), "NEW_INIT_3_KEY"),
    **dict.fromkeys(("LPMC", "LPMU", "AOMC", "AOML"), "NEW_INIT_4_KEY"),
}


def read_private_json(path: Path):
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise CameraError("config_permissions", "Private configuration must have mode 600.")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CameraError(
            "config_unavailable", "Private configuration is missing or invalid."
        ) from exc


def write_private_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


@dataclass(repr=False)
class Settings:
    did: str
    password: str = field(repr=False)
    bridge_token: str = field(repr=False)
    sdk_init: str = field(repr=False)
    server_key: str | None = field(default=None, repr=False)
    user_id: int = 0
    shared: bool = False
    connect_mode: int = 0
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8766
    bridge_kind: str = "loopback"
    windows_python: str = ""
    data_root: Path = Path("data")
    command_channel: int = 0
    download_channel: int = 4
    request_timeout: float = 15
    transfer_timeout: float = 120

    def __post_init__(self):
        numeric_fields = [self.connect_mode, self.command_channel, self.download_channel]
        if (
            any(not 0 <= value <= 255 for value in numeric_fields)
            or not 0 <= self.user_id <= 0xFFFFFFFF
        ):
            raise CameraError("config_range", "Device mode, channel or user ID is out of range.")
        if not 1 <= self.bridge_port <= 65535:
            raise CameraError("config_range", "Bridge port is out of range.")
        if any(
            not math.isfinite(value) or value <= 0
            for value in [self.request_timeout, self.transfer_timeout]
        ):
            raise CameraError("config_range", "Timeouts must be finite positive numbers.")
        if "\0" in self.did or not 4 <= len(self.did) <= 128:
            raise CameraError("device_id", "Device identifier is invalid.")
        if "\0" in self.password or not 1 <= len(self.password.encode("utf-8")) <= 63:
            raise CameraError("password_format", "Device password must fit its 64-byte field.")

    @classmethod
    def load(cls, path: Path):
        cfg = read_private_json(path)
        device, bridge = cfg.get("device", {}), cfg.get("bridge", {})
        if not device.get("did") or not device.get("password") or not bridge.get("token"):
            raise CameraError(
                "device_not_configured", "Fill device and bridge credentials locally."
            )
        profile_path = (path.parent / cfg.get("sdk_profiles", "sdk-profiles.json")).resolve()
        profiles = read_private_json(profile_path)
        did = device["did"].strip()
        matching = [k for k in PREFIX_PROFILES if did.startswith(k)]
        override = device.get("server_profile")
        if not matching and override is None:
            raise CameraError(
                "unsupported_device_prefix", "No verified SDK profile for this DID prefix."
            )
        key_name = override if override is not None else PREFIX_PROFILES[matching[0]]
        server_key = profiles.get("server_profiles", {}).get(key_name) if key_name else None
        if key_name and not server_key:
            raise CameraError("sdk_profile_missing", "Selected SDK profile has not been imported.")
        channels = cfg.get("channels", {})
        return cls(
            did=did,
            password=device["password"],
            bridge_token=bridge["token"],
            sdk_init=profiles["initialize"],
            server_key=server_key,
            user_id=int(device.get("user_id", 0)),
            shared=bool(device.get("shared", False)),
            connect_mode=int(device.get("connect_mode", 0)),
            bridge_host=bridge.get("host", "127.0.0.1"),
            bridge_port=int(bridge.get("port", 8766)),
            bridge_kind=bridge.get("adapter", "loopback"),
            windows_python=bridge.get("windows_python", ""),
            data_root=(path.parent / cfg.get("data_root", "../data")).resolve(),
            command_channel=int(channels.get("command", 0)),
            download_channel=int(channels.get("download", 4)),
            request_timeout=float(cfg.get("request_timeout", 15)),
            transfer_timeout=float(cfg.get("transfer_timeout", 120)),
        )


def initialize_config(directory: Path):
    write_private_json(
        directory / "device.json",
        {
            "bridge": {"host": "127.0.0.1", "port": 8766, "token": ""},
            "device": {"did": "", "password": "", "user_id": 0, "shared": False, "connect_mode": 0},
            "sdk_profiles": "sdk-profiles.json",
            "data_root": "../data",
            "channels": {"command": 0, "download": 4},
            "request_timeout": 15,
            "transfer_timeout": 120,
        },
    )
    write_private_json(directory / "web.json", {"access_token": secrets.token_urlsafe(32)})
