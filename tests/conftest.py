"""Synthetic TCP peer. This checks software only, never real PPCS/camera behavior."""

import base64
import json
import socket
import socketserver
import struct
import subprocess
import threading
from contextlib import contextmanager

import pytest

from v360_web.media import find_tool

TOKEN = "test-only-token-not-a-real-secret-123456789"
TIME = bytes.fromhex("ea070a1d000b1c00")


def frame(command, payload):
    return struct.pack("<6I", 0x76768888, len(payload), command, 0, 0, 0) + payload


@pytest.fixture(scope="session")
def media_bytes(tmp_path_factory):
    target = tmp_path_factory.mktemp("synthetic-media") / "one-second.mp4"
    executable = find_tool("ffmpeg")
    assert executable, "Media integration checks require the project-local FFmpeg installation"
    subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=5:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
        timeout=30,
    )
    return target.read_bytes()


@pytest.fixture
def bridge_server(media_bytes):
    @contextmanager
    def start(mode="normal"):
        state = {"closed": 0, "writes": [], "mode": mode}

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                buffers = {0: bytearray(), 4: bytearray()}
                try:
                    while line := self.rfile.readline():
                        request = json.loads(line)
                        if request.get("token") != TOKEN:
                            answer = {"ok": False, "error": "authentication", "ret": -1}
                        else:
                            op = request["op"]
                            answer = {"ok": True, "ret": 0}
                            if op == "connect":
                                answer["session"] = 7
                            elif op == "status":
                                answer["version"] = 1
                            elif op == "close":
                                state["closed"] += 1
                            elif op == "write":
                                data = base64.b64decode(request["data"])
                                state["writes"].append(data)
                                command = struct.unpack_from("<I", data, 8)[0]
                                if command == 16:
                                    buffers[0].extend(frame(16, struct.pack("<i", 0)))
                                elif command == 792:
                                    record = TIME + bytes([2, 1]) + struct.pack("<h", 60)
                                    buffers[0].extend(
                                        frame(793, struct.pack("<iiiBB2x", 2, 1, 1, 0, 1) + record)
                                    )
                                elif command == 33068:
                                    start_time = data[28:36]
                                    response = struct.pack("<i", len(media_bytes)) + start_time
                                    response += struct.pack("<ii", 2, len(media_bytes)) + bytes(8)
                                    buffers[0].extend(frame(33069, response))
                                    pieces = [media_bytes[:500], media_bytes[500:]]
                                    for seq, piece in enumerate(pieces):
                                        flags = 1 if seq == 0 else 2
                                        if mode == "early-end":
                                            flags = 3
                                        wire_seq = seq + (2 if mode == "gap" and seq == 1 else 0)
                                        buffers[4].extend(
                                            struct.pack(
                                                "<BBhII", 0, flags, len(piece), wire_seq, 100
                                            )
                                        )
                                        buffers[4].extend(piece)
                                        if mode == "early-end":
                                            break
                            elif op == "read":
                                data = buffers[request["channel"]]
                                count = min(request["size"], len(data), 13)
                                answer["data"] = base64.b64encode(data[:count]).decode()
                                del data[:count]
                                answer["ret"] = 0 if count == request["size"] else -3
                        wire = json.dumps(answer).encode() + b"\n"
                        for at in range(0, len(wire), 17):
                            self.wfile.write(wire[at : at + 17])
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return start
