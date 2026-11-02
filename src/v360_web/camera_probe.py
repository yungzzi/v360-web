"""Authenticated, bounded read-only storage and SD-playback diagnostics."""

import base64
import struct
import threading
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


def playback_probe(config, key, stream_type=None):
    settings = Settings.load(config)
    item = RecordingStore(settings.data_root).get(key)
    if not item or item["device_id"] != settings.did:
        raise CameraError("recording_missing", "Recording is not indexed for this device.")
    # Normal single-camera overload uses the directory event byte as optSeq;
    # the older NVR overload's stream-type byte is a different contract.
    selector = item["event_type"] if stream_type is None else stream_type
    payload = bytearray(
        struct.pack("<III", item["channel"], 16, 0)
        + encode_time(datetime.fromisoformat(item["start_local"]), item["weekday"])
        + bytes([selector, 0, 0, 0])
    )
    struct.pack_into("<H", payload, 22, (0 if settings.shared else settings.user_id) & 0xFFFF)
    result = {}
    phase = "control_response"
    with CameraClient(settings) as camera:
        frame = bytearray()
        frame_channel = []
        done = threading.Event()
        halt = threading.Event()
        read_errors = []

        def first_header():
            deadline = time.monotonic() + 20
            try:
                buffers = {channel: bytearray() for channel in (3, 2, 5)}
                while not halt.is_set() and time.monotonic() < deadline and len(frame) < 16:
                    for channel, buffer in buffers.items():
                        reply = camera.bridge.rpc(
                            "read",
                            session=camera.session,
                            channel=channel,
                            size=16 - len(buffer),
                            timeout_ms=50,
                        )
                        if reply["ret"] not in (0, -3):
                            raise CameraError(
                                "sd_frame_read", "SD channel read failed.", reply["ret"]
                            )
                        chunk = base64.b64decode(reply.get("data", ""), validate=True)
                        if len(chunk) > 16 - len(buffer):
                            raise CameraError("sd_frame_read", "SD frame header overflow.")
                        buffer.extend(chunk)
                        if len(buffer) == 16:
                            frame.extend(buffer)
                            frame_channel.append(channel)
                            break
                    if len(frame) < 16:
                        halt.wait(0.05)
            except (CameraError, OSError, ValueError) as error:
                read_errors.append(getattr(error, "code", "sd_frame_read"))
            finally:
                done.set()

        reader = threading.Thread(target=first_header, name="sd-probe-reader", daemon=True)
        reader.start()
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
                done.wait(20)
                if len(frame) != 16:
                    raise CameraError(
                        read_errors[0] if read_errors else "read_timeout",
                        "No complete SD frame header arrived.",
                    )
                flag, size, pts, kind = struct.unpack("<4I", frame)
                result.update(
                    frame_channel=frame_channel[0],
                    frame_flag=flag,
                    frame_size=size,
                    frame_pts=pts,
                    frame_type=kind,
                )
        except CameraError as error:
            result.update(probe_error=error.code, phase=phase, sdk_code=error.sdk_code)
        finally:
            halt.set()
            reader.join(timeout=2)
            stop = bytearray(payload)
            struct.pack_into("<I", stop, 4, 1)
            try:
                camera.send(794, stop)
                result["stop_requested"] = True
            except CameraError:
                result["stop_requested"] = False
    return result
