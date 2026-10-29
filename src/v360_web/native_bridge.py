"""Owned stdio connection to the ARM64/Bionic worker; no Android device or TCP bridge."""

import base64
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

from .bridge import MAX_READ, AndroidBridge
from .errors import CameraError

MAGIC = 0x56575031


class NativePPCSBridge(AndroidBridge):
    def __init__(self, command=None, timeout=40):
        super().__init__("127.0.0.1", 8766, "private-stdio-not-a-network-auth-token", timeout)
        root = Path(__file__).resolve().parents[2]
        self.command = command or [sys.executable, str(root / "tools/run_native_worker.py")]
        self.process = None
        self.session = None
        self.last_cleanup = None

    def open(self):
        if os.name != "posix":
            raise CameraError("native_platform", "Run the native transport from Linux/WSL.")
        if self.process is not None:
            raise CameraError("native_busy", "Native worker is already open.")
        self.last_cleanup = None
        self.session = None
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=True,
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except OSError as exc:
            self._abort()
            raise CameraError("native_worker", "Could not start native SDK worker.") from exc

    def _io(self, data, size, deadline):
        p = self.process
        if p is None:
            raise CameraError("native_closed", "Native SDK worker is closed.")
        written = 0
        while written < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [p.stdin], [], max(0, remaining))[1]:
                raise TimeoutError
            count = os.write(p.stdin.fileno(), data[written:])
            if not count:
                raise OSError
            written += count
        value = bytearray()
        while len(value) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([p.stdout], [], [], max(0, remaining))[0]:
                raise TimeoutError
            piece = os.read(p.stdout.fileno(), size - len(value))
            if not piece:
                raise OSError
            value.extend(piece)
        return bytes(value)

    def _exchange(self, op, session=0, channel=0, timeout=0, size=0, body=b""):
        deadline = time.monotonic() + self.timeout
        header = struct.pack("<IiIIII", op, session, channel, timeout, size, len(body))
        raw = self._io(header + body, 12, deadline)
        magic, ret, length = struct.unpack("<IiI", raw)
        if (
            magic != MAGIC
            or length > MAX_READ
            or (op == 5 and length > size)
            or (op not in (5, 6) and length)
        ):
            raise ValueError("Invalid private worker frame")
        data = self._io(b"", length, deadline)
        return ret, data

    def rpc(self, operation, **args):
        with self.lock:
            try:
                if operation == "status":
                    ret, _ = self._exchange(0)
                    return {
                        "ok": True,
                        "version": 1,
                        "sdk_api_version": ret,
                        "active_sessions": int(self.session is not None),
                        "adapter": "native-arm64",
                    }
                if operation == "initialize":
                    body = args["init"].encode("utf-8")
                    if not 4 <= len(body) <= 65535 or b"\0" in body:
                        raise ValueError
                    ret, _ = self._exchange(1, body=body)
                    code = "sdk_initialize"
                elif operation == "connect":
                    did = args["did"].encode("ascii")
                    server = args.get("server_key", "").encode("ascii")
                    mode = args["mode"]
                    if (
                        len(did) > 127
                        or len(server) > 1023
                        or not 0 <= mode <= 255
                        or b"\0" in did + server
                    ):
                        raise ValueError
                    ret, _ = self._exchange(
                        2, channel=mode, body=did.ljust(128, b"\0") + server.ljust(1024, b"\0")
                    )
                    if ret >= 0:
                        self.session = ret
                        return {"ok": True, "session": ret}
                    code = "sdk_connect"
                elif operation == "close":
                    ret, _ = self._exchange(3, session=args["session"])
                    if ret == 0:
                        self.session = None
                    code = "sdk_close"
                elif operation == "write":
                    body = base64.b64decode(args["data"], validate=True)
                    if len(body) > MAX_READ or not 0 <= args["channel"] <= 255:
                        raise ValueError
                    ret, _ = self._exchange(
                        4, session=args["session"], channel=args["channel"], body=body
                    )
                    code = "sdk_write"
                elif operation == "read":
                    if (
                        not 1 <= args["size"] <= MAX_READ
                        or not 1 <= args["timeout_ms"] <= 1000
                        or not 0 <= args["channel"] <= 255
                    ):
                        raise ValueError
                    ret, body = self._exchange(
                        5,
                        session=args["session"],
                        channel=args["channel"],
                        timeout=args["timeout_ms"],
                        size=args["size"],
                    )
                    return {"ok": True, "ret": ret, "data": base64.b64encode(body).decode("ascii")}
                else:
                    raise ValueError
            except (OSError, ValueError, struct.error) as exc:
                self._abort()
                raise CameraError(
                    "native_io", "Native worker failed or timed out; process stopped."
                ) from exc
            if ret < 0:
                raise CameraError(code, "Native PPCS SDK rejected the operation.", ret)
            return {"ok": True, "ret": ret}

    def _abort(self):
        p, self.process = self.process, None
        if p is None:
            return
        if p.poll() is None:
            self.last_cleanup = {"forced": True}
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=2)
            except ProcessLookupError:
                p.wait(timeout=2)
        p.stdin.close()
        p.stdout.close()
        self.session = None

    def close(self):
        if self.process is None:
            return
        if self.lock.acquire(timeout=0.1):
            try:
                old_timeout = self.timeout
                self.timeout = min(self.timeout, 3)
                ret, body = self._exchange(6)
                if ret == 0 and len(body) == 8:
                    close_ret, deinit_ret = struct.unpack("<ii", body)
                    self.last_cleanup = {
                        "force_close_ret": close_ret,
                        "deinitialize_ret": deinit_ret,
                    }
                self.process.wait(timeout=2)
            except (OSError, ValueError, struct.error, CameraError, subprocess.TimeoutExpired):
                self.last_cleanup = {"forced": True}
            finally:
                self.timeout = old_timeout
                self.lock.release()
        self._abort()
