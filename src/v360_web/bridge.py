"""Authenticated, loopback-only JSON RPC adapter for our Android PPCS helper."""

from __future__ import annotations

import base64
import ipaddress
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .errors import CameraError
from .progress import check_cancelled

MAX_READ = 1024 * 1024
MAX_LINE = 2 * MAX_READ


class AndroidBridge:
    def __init__(self, host: str, port: int, token: str, timeout: float = 40):
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError as exc:
            raise CameraError(
                "bridge_address", "Use a loopback address with USB/SSH forwarding."
            ) from exc
        if len(token) < 32:
            raise CameraError("bridge_token", "Use the full token shown by the Android helper.")
        self.host, self.port, self._token, self.timeout = host, port, token, timeout
        self.socket = None
        self.reader = None
        self.lock = threading.Lock()

    def open(self):
        try:
            self.socket = socket.create_connection((self.host, self.port), self.timeout)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.reader = self.socket.makefile("rb")
        except OSError as exc:
            self.close()
            raise CameraError(
                "bridge_unreachable", "Android bridge is unavailable; check USB forwarding."
            ) from exc

    def rpc(self, operation: str, **args):
        with self.lock:
            if self.socket is None:
                raise CameraError("bridge_closed", "Bridge connection is closed.")
            try:
                request = {"version": 1, "token": self._token, "op": operation, **args}
                self.socket.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                line = self.reader.readline(MAX_LINE + 1)
                if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise ValueError("Invalid RPC frame")
                result = json.loads(line)
            except (OSError, ValueError) as exc:
                self.close()
                raise CameraError("bridge_io", "Bridge response failed or timed out.") from exc
            if not result.get("ok"):
                raise CameraError(
                    result.get("error", "bridge_error"),
                    "Android bridge rejected the operation.",
                    result.get("ret"),
                )
            return result

    def initialize(self, value: str):
        return self.rpc("initialize", init=value)

    def connect(self, did: str, mode: int, server_key: str | None):
        return self.rpc("connect", did=did, mode=mode, udp_port=0, server_key=server_key or "")[
            "session"
        ]

    def write(self, session: int, channel: int, data: bytes):
        result = self.rpc(
            "write", session=session, channel=channel, data=base64.b64encode(data).decode("ascii")
        )
        if result["ret"] < 0:
            raise CameraError("sdk_write", "PPCS write failed.", result["ret"])

    def read_exact(self, session: int, channel: int, size: int, deadline: float):
        if size < 0 or size > 16 * MAX_READ:
            raise CameraError("packet_size", "Requested packet exceeds the protocol limit.")
        data = bytearray()
        while len(data) < size:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CameraError("read_timeout", "Timed out waiting for a complete PPCS packet.")
            result = self.rpc(
                "read",
                session=session,
                channel=channel,
                size=min(size - len(data), MAX_READ),
                timeout_ms=max(1, min(500, int(remaining * 1000))),
            )
            try:
                chunk = base64.b64decode(result.get("data", ""), validate=True)
            except ValueError as exc:
                raise CameraError("bridge_data", "Invalid bridge data encoding.") from exc
            if len(chunk) > size - len(data):
                raise CameraError("packet_size", "Bridge returned more bytes than requested.")
            if result["ret"] not in (0, -3):
                raise CameraError("sdk_read", "PPCS read failed.", result["ret"])
            data.extend(chunk)
            if not chunk:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return bytes(data)

    def close_session(self, session: int):
        return self.rpc("close", session=session)

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.socket is not None:
            self.socket.close()
            self.socket = None


class _PipeWriter:
    def __init__(self, process):
        self.process = process

    def sendall(self, value):
        self.process.stdin.write(value)
        self.process.stdin.flush()

    def close(self):
        self.process.stdin.close()


class StdioBridge(AndroidBridge):
    """Same authenticated RPC through an owned, local subprocess and private pipes."""

    def __init__(self, command: list[str], token: str):
        super().__init__("127.0.0.1", 8766, token)
        self.command = command
        self.process = None

    def open(self):
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.reader = self.process.stdout
            self.socket = _PipeWriter(self.process)
        except OSError as exc:
            raise CameraError("usb_relay", "Could not start the local USB relay.") from exc

    def close(self):
        try:
            super().close()
        finally:
            if self.process is not None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                self.process = None


def create_bridge(host: str, port: int, token: str, kind="loopback", windows_python=""):
    if kind == "native-arm64":
        from .native_bridge import NativePPCSBridge

        return NativePPCSBridge()
    if kind == "loopback":
        return AndroidBridge(host, port, token)
    if kind != "windows-usb":
        raise CameraError("bridge_adapter", "Unknown bridge adapter.")
    interpreter = windows_python or shutil.which("python.exe")
    if not interpreter or not Path(interpreter).is_file():
        raise CameraError(
            "windows_python",
            "Configure bridge.windows_python with a Windows Python 3.10+ executable.",
        )
    script = Path(__file__).resolve().parents[2] / "tools/windows_usb_stdio.py"
    try:
        windows_script = subprocess.check_output(["wslpath", "-w", str(script)], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CameraError(
            "usb_relay_path", "Windows USB adapter must run from the WSL checkout."
        ) from exc
    return StdioBridge([str(interpreter), "-u", windows_script, str(port)], token)
