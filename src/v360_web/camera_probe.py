"""Authenticated, bounded read-only storage and SD-playback diagnostics."""

import struct
import time
from datetime import datetime

from .client import CameraClient
from .config import Settings
from .errors import CameraError
from .protocol import encode_time
from .store import RecordingStore


def storage_status(config):
    settings = Settings.load(config)
    with CameraClient(settings) as camera:
        camera.send(816, bytes(4))
        payload = camera.response(817, time.monotonic() + settings.request_timeout)
    if len(payload) < 56:
        raise CameraError("storage_response", "Storage response is truncated.")
    total, free = struct.unpack_from("<ii", payload, 40)
    flags = payload[55]
    return {
        "total_raw": total,
        "free_raw": free,
        "has_card": total > 0,
        "anomaly": total < 0,
        "file_download_supported": bool(flags & 8),
    }


def playback_probe(config, key):
    settings = Settings.load(config)
    item = RecordingStore(settings.data_root).get(key)
    if not item or item["device_id"] != settings.did:
        raise CameraError("recording_missing", "Recording is not indexed for this device.")
    payload = bytearray(
        struct.pack("<III", item["channel"], 16, 0)
        + encode_time(datetime.fromisoformat(item["start_local"]), item["weekday"])
        + bytes([2, 0, 0, 0])
    )
    struct.pack_into("<H", payload, 22, (0 if settings.shared else settings.user_id) & 0xFFFF)
    result = {}
    phase = "control_response"
    with CameraClient(settings) as camera:
        try:
            camera.send(794, payload)
            reply = camera.response(795, time.monotonic() + settings.request_timeout)
            if len(reply) < 12:
                raise CameraError("sd_playback_response", "SD playback reply is truncated.")
            command, code = struct.unpack_from("<ii", reply)
            result.update(
                command=command, result=code, duration_raw=struct.unpack_from("<h", reply, 8)[0]
            )
            if command == 16 and code == 0:
                phase = "first_frame"
                head = camera.bridge.read_exact(camera.session, 3, 16, time.monotonic() + 5)
                flag, size, pts, kind = struct.unpack("<4I", head)
                result.update(frame_flag=flag, frame_size=size, frame_pts=pts, frame_type=kind)
        except CameraError as error:
            result.update(probe_error=error.code, phase=phase, sdk_code=error.sdk_code)
        finally:
            stop = bytearray(payload)
            struct.pack_into("<I", stop, 4, 1)
            try:
                camera.send(794, stop)
                result["stop_requested"] = True
            except CameraError:
                result["stop_requested"] = False
    return result
