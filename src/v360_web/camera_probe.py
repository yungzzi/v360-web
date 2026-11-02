"""Authenticated, bounded read-only storage and SD-playback diagnostics."""

import base64
import hashlib
import struct
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import CameraClient
from .config import Settings
from .errors import CameraError
from .media import find_tool
from .protocol import encode_time
from .store import RecordingStore


def monthly_index(camera, year, month, channel=0):
    # LiveViewModel.getMonthFlag(year, month, channel) selects physical SD disk 1.
    camera.send(1287, struct.pack("<4I", 1, year, month, channel))
    payload = camera.response(1288, time.monotonic() + camera.settings.request_timeout)
    if len(payload) < 36:
        raise CameraError("sd_calendar_response", "SD monthly directory response is truncated.")
    return {
        "disk_flag": struct.unpack_from("<I", payload)[0],
        "calendar_flags": list(payload[4:36]),
    }


def storage_status(config):
    settings = Settings.load(config)
    with CameraClient(settings) as camera:
        camera.send(816, bytes(4))
        payload = camera.response(817, time.monotonic() + settings.request_timeout)
        now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        try:
            calendar = monthly_index(camera, now.year, now.month)
        except CameraError as error:
            calendar = {"calendar_error": error.code}
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
        **calendar,
    }


def playback_probe(config, key, stream_type=None, capture=False):
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
                if capture and 0 < size <= 2 * 1024**2:
                    if kind == 16384:
                        ext = camera.bridge.read_exact(
                            camera.session, frame_channel[0], 16, time.monotonic() + 10
                        )
                        kind = struct.unpack_from("<I", ext)[0]
                    content = camera.bridge.read_exact(
                        camera.session, frame_channel[0], size, time.monotonic() + 15
                    )
                    if kind not in (1, 17) or not content.startswith((b"\0\0\1", b"\0\0\0\1")):
                        raise CameraError(
                            "sd_probe_format", "First SD frame is not a supported key frame."
                        )
                    folder = settings.data_root / "protocol-diagnostics" / "sd-frame"
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    name = hashlib.sha256(key.encode()).hexdigest()[:32]
                    raw = folder / (name + (".hevc" if kind == 17 else ".h264"))
                    raw.write_bytes(content)
                    raw.chmod(0o600)
                    picture = folder / (name + ".jpg")
                    converted = subprocess.run(
                        [
                            find_tool("ffmpeg"),
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(raw),
                            "-frames:v",
                            "1",
                            str(picture),
                        ],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                    result["frame_decoded"] = converted.returncode == 0 and picture.is_file()
                    if result["frame_decoded"]:
                        picture.chmod(0o600)
                        result["diagnostic_picture"] = str(picture.relative_to(settings.data_root))
        except (CameraError, subprocess.TimeoutExpired) as error:
            result.update(
                probe_error=getattr(error, "code", "sd_probe_decode_timeout"),
                phase=phase,
                sdk_code=getattr(error, "sdk_code", None),
            )
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
