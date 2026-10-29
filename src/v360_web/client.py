from __future__ import annotations

import hashlib
import struct
import time
from datetime import datetime
from pathlib import Path

from . import protocol as wire
from .bridge import create_bridge
from .config import Settings
from .errors import CameraError


class CameraClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.bridge = transport or create_bridge(
            settings.bridge_host,
            settings.bridge_port,
            settings.bridge_token,
            settings.bridge_kind,
            settings.windows_python,
        )
        self.session = None
        self.last_list_diagnostics = {"invalid_records": 0, "metadata_complete": True}

    def __enter__(self):
        try:
            self.bridge.open()
            self.bridge.initialize(self.settings.sdk_init)
            for attempt in range(2):
                try:
                    self.session = self.bridge.connect(
                        self.settings.did, self.settings.connect_mode, self.settings.server_key
                    )
                    break
                except CameraError as error:
                    if error.code != "sdk_connect" or error.sdk_code != -3 or attempt:
                        raise
                    # Retry only transport setup, before sending any login command.
                    time.sleep(1)
            self.send(
                wire.LOGIN,
                wire.login_payload(
                    self.settings.password, self.settings.user_id, self.settings.shared
                ),
            )
            payload = self.response(wire.LOGIN, time.monotonic() + self.settings.request_timeout)
            if len(payload) < 4:
                raise CameraError("login_response", "Device login response is truncated.")
            result = struct.unpack_from("<i", payload)[0]
            if result:
                raise CameraError(
                    "device_authentication", "Device did not accept its credentials.", result
                )
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            if self.session is not None:
                self.bridge.close_session(self.session)
        except CameraError:
            # The helper also releases session ownership when the RPC connection closes.
            pass
        finally:
            self.session = None
            self.bridge.close()

    def send(self, command: int, payload: bytes):
        self.bridge.write(
            self.session, self.settings.command_channel, wire.command_frame(command, payload)
        )

    def response(self, expected: int, deadline: float):
        for _ in range(1024):
            header = self.bridge.read_exact(
                self.session, self.settings.command_channel, 24, deadline
            )
            command, length, _ = wire.parse_command_header(header)
            payload = self.bridge.read_exact(
                self.session, self.settings.command_channel, length, deadline
            )
            if command == expected:
                return payload
        raise CameraError("command_flood", "Too many unrelated command responses.")

    def list_recordings(self, channel: int, start: datetime, end: datetime):
        self.send(wire.LIST_REQUEST, wire.list_payload(channel, start, end))
        deadline = time.monotonic() + self.settings.request_timeout
        events, seen = [], set()
        self.last_list_diagnostics = {"invalid_records": 0, "metadata_complete": True}
        for _ in range(1024):
            payload = self.response(wire.LIST_RESPONSE, deadline)
            invalid = [] if self.settings.allow_invalid_record_dates else None
            response_channel, _total, _index, finished, page = wire.parse_list_page(
                payload, invalid
            )
            if response_channel != channel:
                raise CameraError(
                    "channel_mismatch", "Device replied for a different recording channel."
                )
            identity = hashlib.sha256(payload).digest()
            if identity not in seen:
                seen.add(identity)
                events.extend(page)
                if invalid:
                    self.last_list_diagnostics["invalid_records"] += len(invalid)
                    self.last_list_diagnostics["metadata_complete"] = False
                    # Preserve a bounded number of malformed pages privately. They
                    # are never served as recordings by the authenticated media API.
                    if self.last_list_diagnostics.get("raw_pages_saved", 0) < 4:
                        root = self.settings.data_root / "protocol-diagnostics"
                        root.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target = root / (identity.hex() + ".bin")
                        if not target.exists():
                            with target.open("xb") as output:
                                output.write(payload)
                        target.chmod(0o600)
                        self.last_list_diagnostics["raw_pages_saved"] = (
                            self.last_list_diagnostics.get("raw_pages_saved", 0) + 1
                        )
            if len(events) > 100000:
                raise CameraError("list_size", "Recording query exceeded its supported size.")
            if finished:
                return events
        raise CameraError("list_unfinished", "Recording list did not receive an end marker.")

    def download(self, channel: int, start: datetime, target: Path, weekday: int = 0, progress=None):
        user_id = 0 if self.settings.shared else self.settings.user_id
        self.send(wire.DOWNLOAD_REQUEST, wire.download_payload(channel, start, user_id, weekday))
        response = self.response(
            wire.DOWNLOAD_RESPONSE, time.monotonic() + self.settings.request_timeout
        )
        expected, actual_start, _, _, _ = wire.parse_download_response(response)
        if actual_start != start or expected > 2 * 1024**3:
            raise CameraError(
                "download_identity", "Device returned a different recording or excessive size."
            )
        if progress:
            progress(0, expected)
        deadline = time.monotonic() + self.settings.transfer_timeout
        previous = file_start = None
        written = 0
        digest = hashlib.sha256()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("wb") as output:
            while True:
                header = self.bridge.read_exact(
                    self.session, self.settings.download_channel, 12, deadline
                )
                _, flags, length, sequence, packet_start = wire.parse_download_header(header)
                if previous is None:
                    if not flags & 1:
                        raise CameraError(
                            "download_start", "Download did not begin with its start marker."
                        )
                    file_start = packet_start
                elif sequence != (previous + 1) & 0xFFFFFFFF or flags & 1:
                    raise CameraError(
                        "download_sequence", "Download packet sequence is discontinuous."
                    )
                if file_start != packet_start or written + length > expected:
                    raise CameraError(
                        "download_identity", "Download identity or total size changed."
                    )
                content = self.bridge.read_exact(
                    self.session, self.settings.download_channel, length, deadline
                )
                output.write(content)
                digest.update(content)
                written += length
                if progress:
                    progress(written, expected)
                previous = sequence
                if flags & 2:
                    if written != expected:
                        raise CameraError(
                            "download_truncated",
                            "End marker arrived before the declared file size.",
                        )
                    output.flush()
                    __import__("os").fsync(output.fileno())
                    break
        return {"bytes": written, "sha256": digest.hexdigest(), "start_local": start.isoformat()}
