"""Manual, cached battery query; never wakes a camera during page polling."""

import hashlib
import struct
import time
from pathlib import Path

from .client import CameraClient
from .config import Settings, read_private_json, write_private_json
from .errors import CameraError

TTL = 15 * 60


def parse(payload):
    if len(payload) < 12:
        raise CameraError("battery_packet", "Battery response is truncated.")
    # App constructors receive command ID + payload; CameraClient strips the ID.
    level, voltage, charging = struct.unpack_from("<iii", payload)
    return {
        "percent": level if 0 <= level <= 100 else None,
        "charging": bool(charging) if charging in (0, 1) else None,
        "voltage_raw": voltage,
        "charging_raw": charging,
    }


def cache_path(settings):
    key = hashlib.sha256(settings.did.encode()).hexdigest()[:32]
    return settings.data_root / "device-status" / (key + ".json")


def cached(settings):
    try:
        value = read_private_json(cache_path(settings))
        return {**value, "stale": time.time() - value["updated_at"] >= TTL}
    except (CameraError, KeyError, TypeError):
        return {"percent": None, "charging": None, "updated_at": None, "stale": True}


def refresh(config: Path):
    settings = Settings.load(config)
    previous = cached(settings)
    if not previous["stale"]:
        return {**previous, "from_cache": True}
    with CameraClient(settings) as camera:
        camera.send(912, bytes(4))
        value = parse(camera.response(913, time.monotonic() + settings.request_timeout))
    value["updated_at"] = time.time()
    path = cache_path(settings)
    temporary = path.with_suffix(".new")
    write_private_json(temporary, value)
    temporary.replace(path)
    return {**value, "stale": False, "from_cache": False}
