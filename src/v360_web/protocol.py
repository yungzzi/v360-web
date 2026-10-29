"""Wire layouts derived from the exact APK; remote dates retain device-local time."""

import struct
from dataclasses import dataclass
from datetime import datetime

from .errors import CameraError

MAGIC = 0x76768888
LOGIN = 0x10
LIST_REQUEST, LIST_RESPONSE = 0x318, 0x319
DOWNLOAD_REQUEST, DOWNLOAD_RESPONSE = 33068, 33069
MAX_COMMAND = 1024 * 1024


def command_frame(command: int, payload: bytes, selector: int = 0):
    if len(payload) > MAX_COMMAND or not 0 <= selector <= 255:
        raise CameraError("packet_size", "Command payload or selector is out of range.")
    return struct.pack("<IIIIB7x", MAGIC, len(payload), command, 0, selector) + payload


def parse_command_header(data: bytes):
    if len(data) != 24:
        raise CameraError("command_header", "Command header must contain 24 bytes.")
    magic, size, command, _, selector = struct.unpack("<IIIIB7x", data)
    if magic != MAGIC or size > MAX_COMMAND:
        raise CameraError("command_header", "Command header has invalid magic or length.")
    return command, size, selector


def encode_time(value: datetime, weekday: int = 0):
    if value.tzinfo is not None:
        raise CameraError("device_time", "Pass device-local time without a UTC offset.")
    return struct.pack(
        "<H6B", value.year, value.month, value.day, weekday, value.hour, value.minute, value.second
    )


def decode_time(data: bytes):
    try:
        year, month, day, weekday, hour, minute, second = struct.unpack("<H6B", data)
        return datetime(year, month, day, hour, minute, second), weekday
    except (ValueError, struct.error) as exc:
        raise CameraError("device_time", "Device returned an invalid calendar value.") from exc


def login_payload(password: str, user_id: int, shared: bool):
    raw = password.encode("utf-8")
    if not 1 <= len(raw) <= 63 or "\0" in password:
        raise CameraError("password_format", "Device password must fit its 64-byte field.")
    # Connect.loginDev always passes false in this APK. Shared ownership changes user ID.
    return raw.ljust(64, b"\0") + struct.pack("<B3xI", 0, 0 if shared else user_id)


def list_payload(channel: int, start: datetime, end: datetime):
    if start > end:
        raise CameraError("time_range", "Start time must not be after end time.")
    return struct.pack("<I", channel) + encode_time(start) + encode_time(end) + bytes(4)


@dataclass(frozen=True)
class RecordingEvent:
    start: datetime
    weekday: int
    event_type: int
    status: int
    duration_raw: int


def parse_list_page(payload: bytes, invalid_records: list | None = None):
    if len(payload) < 16:
        raise CameraError("list_packet", "Recording list header is truncated.")
    channel, total, count, index, endflag = struct.unpack_from("<iiiBB", payload)
    required = 16 + 12 * count
    if (
        count < 0
        or count > 80000
        or len(payload) < required
        or len(payload) > MAX_COMMAND
        or endflag not in (0, 1)
    ):
        raise CameraError("list_packet", "Recording list length or end flag is invalid.")
    events = []
    # The shipped constructor reads count records, not all remaining bytes.
    for at in range(16, required, 12):
        try:
            start, weekday = decode_time(payload[at : at + 8])
        except CameraError:
            if invalid_records is None:
                raise
            invalid_records.append((at - 16) // 12)
            continue
        event_type, status, duration = struct.unpack_from("<BBh", payload, at + 8)
        events.append(RecordingEvent(start, weekday, event_type, status, duration))
    return channel, total, index, bool(endflag), events


def download_payload(channel: int, start: datetime, user_id: int, weekday: int = 0):
    payload = bytearray(
        struct.pack("<I", 0) + encode_time(start, weekday) + struct.pack("<III", channel, 0, 0)
    )
    struct.pack_into("<H", payload, 20, user_id & 0xFFFF)
    return bytes(payload)


def parse_download_response(payload: bytes):
    if len(payload) < 20:
        raise CameraError("download_response", "Download response is truncated.")
    size = struct.unpack_from("<i", payload)[0]
    if size <= 0:
        raise CameraError("download_rejected", "Device did not provide a downloadable file.", size)
    start, weekday = decode_time(payload[4:12])
    packets, packet_length = struct.unpack_from("<ii", payload, 12)
    return size, start, weekday, packets, packet_length


def parse_download_header(header: bytes):
    if len(header) != 12:
        raise CameraError("download_packet", "Download packet header is truncated.")
    pkg_flag, flags, size, sequence, start_time = struct.unpack("<BBhII", header)
    if size < 0 or flags & ~3:
        raise CameraError("download_packet", "Download packet flags or length are unsupported.")
    return pkg_flag, flags, size, sequence, start_time
