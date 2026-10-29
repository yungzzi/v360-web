"""Local stdio-to-loopback relay; run by WSL through Windows Python, not a listener."""

import socket
import sys

MAX_LINE = 2 * 1024 * 1024


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    if not 1 <= port <= 65535:
        return 2
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=40) as connection:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            reader = connection.makefile("rb")
            try:
                while True:
                    request = sys.stdin.buffer.readline(MAX_LINE + 1)
                    if not request:
                        return 0
                    if len(request) > MAX_LINE or not request.endswith(b"\n"):
                        return 2
                    connection.sendall(request)
                    response = reader.readline(MAX_LINE + 1)
                    if not response or len(response) > MAX_LINE or not response.endswith(b"\n"):
                        return 2
                    sys.stdout.buffer.write(response)
                    sys.stdout.buffer.flush()
            finally:
                reader.close()
    except (OSError, ValueError):
        print("USB forwarding unavailable or timed out.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
