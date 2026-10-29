import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import TOKEN

from v360_web.bridge import StdioBridge, create_bridge


def test_owned_stdio_relay_roundtrip_and_cleanup(bridge_server):
    worker = Path(__file__).resolve().parents[1] / "tools/windows_usb_stdio.py"
    with bridge_server() as (port, _):
        bridge = StdioBridge([sys.executable, "-u", str(worker), str(port)], TOKEN)
        bridge.open()
        process = bridge.process
        assert bridge.rpc("status")["version"] == 1
        bridge.close()
        assert process.poll() == 0


@pytest.mark.skipif(
    shutil.which("python.exe") is None, reason="Windows interoperability unavailable"
)
def test_actual_wsl_windows_pipe_to_windows_loopback():
    # A Windows-local synthetic peer validates the exact USB relay placement, not a camera.
    code = """import socket,json
s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1)
print(s.getsockname()[1],flush=True)
c,_=s.accept();r=c.makefile('rb');q=json.loads(r.readline())
c.sendall(json.dumps({'ok':True,'version':1,'same_token':q.get('token')=='test-only-token-not-a-real-secret-123456789'}).encode()+b'\\n')
r.close();c.close();s.close()
"""
    peer = subprocess.Popen(
        [shutil.which("python.exe"), "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        port = int(peer.stdout.readline())
        bridge = create_bridge("127.0.0.1", port, TOKEN, "windows-usb")
        try:
            bridge.open()
            answer = bridge.rpc("status")
            assert answer["version"] == 1 and answer["same_token"]
        finally:
            bridge.close()
        assert peer.wait(timeout=5) == 0
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=5)
