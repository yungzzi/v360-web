"""Session-owned control worker. Battery queries share the existing camera session."""

import base64
import threading
import time

from . import battery, protocol
from .errors import CameraError


class CameraControls:
    def __init__(self):
        self.stop = threading.Event()
        self.thread = None
        self.camera = None
        self.buffer = bytearray()
        self.next_battery = 0
        self.pending_until = 0
        self.error = None
        self.guard = threading.Lock()
        self.owner = None
        self.direction = 0
        self.until = 0
        self.sent = 0

    def request_move(self, owner, direction):
        with self.guard:
            if not self.thread or not self.thread.is_alive() or self.stop.is_set():
                raise CameraError("control_unavailable", "Camera controls are not ready.")
            if direction and self.owner not in (None, owner) and time.monotonic() < self.until:
                raise CameraError("ptz_busy", "Another viewer is moving the camera.")
            self.owner = owner if direction else None
            self.direction = direction
            self.until = time.monotonic() + 0.4 if direction else 0

    def release(self, owner):
        with self.guard:
            if self.owner == owner:
                self.owner, self.direction, self.until = None, 0, 0

    def move_tick(self, now):
        with self.guard:
            direction = self.direction if now < self.until else 0
        if direction != self.sent:
            self.camera.send(4097, bytes([direction, 8, 0, 0, 0, 0, 3, 0]))
            self.sent = direction

    def attach(self, camera):
        self.camera = camera
        self.stop.clear()
        self.buffer.clear()
        self.next_battery = 0
        self.pending_until = 0
        self.error = None
        self.owner, self.direction, self.until, self.sent = None, 0, 0, 0
        self.thread = threading.Thread(target=self.run, name="v360-controls", daemon=True)
        self.thread.start()

    def step(self, now):
        self.move_tick(now)
        if now >= self.next_battery:
            self.camera.send(912, bytes(4))
            self.next_battery = now + battery.WATCH_INTERVAL
            self.pending_until = now + 5
        if not self.pending_until:
            return
        if now >= self.pending_until:
            self.pending_until = 0
            self.error = "battery_timeout"
            return
        result = self.camera.bridge.rpc(
            "read",
            session=self.camera.session,
            channel=self.camera.settings.command_channel,
            size=65536,
            timeout_ms=50,
        )
        if result["ret"] not in (0, -3):
            raise CameraError("battery_read", "Control channel read failed.", result["ret"])
        self.buffer.extend(base64.b64decode(result.get("data", ""), validate=True))
        while len(self.buffer) >= 24:
            command, size, _ = protocol.parse_command_header(bytes(self.buffer[:24]))
            if len(self.buffer) < 24 + size:
                break
            payload = bytes(self.buffer[24 : 24 + size])
            del self.buffer[: 24 + size]
            if command == 913:
                battery.save(self.camera.settings, battery.parse(payload))
                self.pending_until = 0
                self.error = None

    def run(self):
        try:
            while not self.stop.is_set():
                self.step(time.monotonic())
                self.stop.wait(0.05 if self.pending_until else 0.2)
        except (CameraError, OSError, ValueError):
            self.error = "control_unavailable"
        finally:
            if self.sent:
                try:
                    self.camera.send(4097, bytes([0, 8, 0, 0, 0, 0, 3, 0]))
                    self.sent = 0
                except (CameraError, OSError):
                    self.error = "ptz_stop_failed"

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)
