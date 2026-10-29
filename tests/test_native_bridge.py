"""Synthetic subprocess checks; these do not load the proprietary SDK."""

import os
import sys
import time

import pytest

from v360_web.client import CameraClient
from v360_web.config import Settings, write_private_json
from v360_web.errors import CameraError
from v360_web.native_bridge import NativePPCSBridge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX pipe transport")

FAKE = r"""
import os,struct,sys,time
mode=sys.argv[1]
def exact(n):
 data=b''
 while len(data)<n:
  p=os.read(0,n-len(data))
  if not p:raise EOFError
  data+=p
 return data
pending=bytearray()
while True:
 try:op,session,channel,timeout,size,length=struct.unpack('<IiIIII',exact(24))
 except EOFError:break
 body=exact(length)
 if mode=='stall':time.sleep(30)
 result=b'';ret=0;magic=0x56575031
 if op==0:ret=84017664
 elif op==2:ret=7
 elif op==4:
  assert session==7
  command=struct.unpack_from('<I',body,8)[0]
  if command==16:pending.extend(struct.pack('<6I',0x76768888,4,16,0,0,0)+bytes(4))
 elif op==5:
  result=bytes(pending[:min(size,5)]);del pending[:len(result)]
  ret=0 if len(result)==size else -3
 elif op==6:result=bytes(8)
 if mode=='bad-magic':magic=0
 header=struct.pack('<IiI',magic,ret,1048577 if mode=='oversize' else len(result))
 for byte in header+result:os.write(1,bytes([byte]))
 if op==6:break
"""


def worker(tmp_path, mode="normal", timeout=2):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE)
    return NativePPCSBridge([sys.executable, str(script), mode], timeout=timeout)


def test_native_framing_handles_fragmentation_and_closes_owned_process(tmp_path):
    transport = worker(tmp_path)
    settings = Settings(
        did="MATE-test",
        password="fake-device-password",
        bridge_token="",
        sdk_init="fake-sdk-initialization",
        bridge_kind="native-arm64",
    )
    with CameraClient(settings, transport=transport):
        assert transport.session == 7
        process = transport.process
    assert transport.last_cleanup == {"force_close_ret": 0, "deinitialize_ret": 0}
    assert transport.process is None and process.poll() == 0


@pytest.mark.parametrize("mode", ["bad-magic", "oversize", "stall"])
def test_untrusted_or_stalled_worker_is_stopped(tmp_path, mode):
    transport = worker(tmp_path, mode=mode, timeout=0.2)
    transport.open()
    process = transport.process
    started = time.monotonic()
    with pytest.raises(CameraError) as exc:
        transport.rpc("status")
    assert exc.value.code == "native_io"
    assert time.monotonic() - started < 3
    assert process.poll() is not None and transport.process is None
    assert transport.last_cleanup == {"forced": True}
    transport.close()


def test_native_configuration_needs_no_phone_token_or_windows_python(tmp_path):
    path = tmp_path / "device.json"
    write_private_json(
        path,
        {
            "device": {"did": "MATE-test", "password": "fake-device-password"},
            "bridge": {"adapter": "native-arm64"},
        },
    )
    write_private_json(tmp_path / "sdk-profiles.json", {"initialize": "fake-sdk-profile"})
    cfg = Settings.load(path)
    assert cfg.bridge_kind == "native-arm64" and cfg.bridge_token == ""
    assert cfg.windows_python == ""
