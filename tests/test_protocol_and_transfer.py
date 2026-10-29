import struct
import time
from datetime import datetime

import pytest
from conftest import TIME, TOKEN

from v360_web import protocol
from v360_web.bridge import AndroidBridge
from v360_web.client import CameraClient
from v360_web.config import Settings
from v360_web.errors import CameraError
from v360_web.media import probe, validate_decode


def settings(port):
    return Settings(
        did="MATE-TEST",
        password="test-device-password",
        bridge_token=TOKEN,
        sdk_init="synthetic-only-not-an-sdk-profile",
        bridge_port=port,
        request_timeout=2,
        transfer_timeout=5,
    )


def test_apk_wire_list_layout():
    # Literal bytes follow the APK's 16-byte page header + 12-byte record layout.
    value = bytes.fromhex("02000000010000000100000000010000ea070a1d000b1c0002013c00")
    channel, total, index, end, events = protocol.parse_list_page(value)
    assert (channel, total, index, end) == (2, 1, 0, True)
    assert events[0].start == datetime(2026, 10, 29, 11, 28)
    assert (events[0].event_type, events[0].status, events[0].duration_raw) == (2, 1, 60)


def test_login_and_low16_user_id_are_distinct_wire_fields():
    value = protocol.login_payload("example", 0x12345678, False)
    assert len(value) == 72 and value[64:68] == bytes(4)
    assert value[68:] == bytes.fromhex("78563412")
    request = protocol.download_payload(2, datetime(2026, 10, 29, 11, 28), 0x12345678)
    assert request[4:12] == TIME
    assert request[20:24] == bytes.fromhex("78560000")


@pytest.mark.parametrize(
    "data", [bytes(24), bytes(23), struct.pack("<6I", 0x76768888, 2**30, 1, 0, 0, 0)]
)
def test_bad_command_header_rejected(data):
    with pytest.raises(CameraError):
        protocol.parse_command_header(data)


def test_truncated_list_is_not_accepted_as_complete():
    data = struct.pack("<iiiBB2x", 2, 1, 1, 0, 1)
    with pytest.raises(CameraError, match="length"):
        protocol.parse_list_page(data)


@pytest.mark.parametrize("count", [0, 1])
def test_device_trailing_slot_is_not_read_as_an_event(count):
    data = struct.pack("<iiiBB2x", 0, count, count, 0, 1)
    if count:
        data += TIME + struct.pack("<BBh", 2, 1, 60)
    # Physical response carried one extra slot; invalid date bytes make accidental
    # parsing observable rather than silently creating a phantom recording.
    data += b"\xff" * 12
    _, _, _, finished, events = protocol.parse_list_page(data)
    assert finished and len(events) == count


def test_oversized_list_padding_is_rejected():
    data = struct.pack("<iiiBB2x", 0, 0, 0, 0, 1) + bytes(protocol.MAX_COMMAND)
    with pytest.raises(CameraError, match="length"):
        protocol.parse_list_page(data)


def test_invalid_record_dates_require_opt_in_and_preserve_raw_page(bridge_server, tmp_path):
    with bridge_server("invalid-record-date") as (port, _):
        strict = settings(port)
        strict.data_root = tmp_path
        with pytest.raises(CameraError, match="calendar"), CameraClient(strict) as camera:
            camera.list_recordings(2, datetime(2026, 10, 29), datetime(2026, 10, 30))
        assert not (tmp_path / "protocol-diagnostics").exists()
        tolerant = settings(port)
        tolerant.data_root = tmp_path
        tolerant.allow_invalid_record_dates = True
        with CameraClient(tolerant) as camera:
            events = camera.list_recordings(2, datetime(2026, 10, 29), datetime(2026, 10, 30))
            diagnostic = camera.last_list_diagnostics
        assert len(events) == 1
        assert diagnostic == {
            "invalid_records": 1,
            "metadata_complete": False,
            "raw_pages_saved": 1,
        }
        raw_files = list((tmp_path / "protocol-diagnostics").glob("*.bin"))
        assert len(raw_files) == 1
        assert raw_files[0].stat().st_mode & 0o077 == 0
        assert raw_files[0].read_bytes() == (
            struct.pack("<iiiBB2x", 2, 2, 2, 0, 1)
            + b"\xff" * 12
            + TIME
            + struct.pack("<BBh", 2, 1, 60)
            + bytes(12)
        )


def test_transport_timeout_retries_once_before_sending_login(bridge_server, monkeypatch):
    monkeypatch.setattr("v360_web.client.time.sleep", lambda _: None)
    with bridge_server("connect-timeout-once") as (port, state):
        with CameraClient(settings(port)):
            pass
        assert state["connect_calls"] == 2 and state["closed"] == 1
        assert len(state["writes"]) == 1
        assert struct.unpack_from("<I", state["writes"][0], 8)[0] == protocol.LOGIN


def test_device_authentication_rejection_is_not_retried(bridge_server):
    with bridge_server("login-rejected") as (port, state):
        with pytest.raises(CameraError) as exc, CameraClient(settings(port)):
            pass
        assert exc.value.code == "device_authentication"
        assert state["connect_calls"] == 1 and state["closed"] == 1


def test_fragmented_rpc_and_partial_sdk_reads_produce_exact_file(
    bridge_server, media_bytes, tmp_path
):
    target = tmp_path / "recording.part"
    with bridge_server() as (port, state):
        with CameraClient(settings(port)) as camera:
            events = camera.list_recordings(2, datetime(2026, 10, 29), datetime(2026, 10, 30))
            result = camera.download(2, events[0].start, target)
        assert state["closed"] == 1
        assert result["bytes"] == len(media_bytes)
        assert target.read_bytes() == media_bytes
        assert struct.unpack_from("<I", state["writes"][2], 8)[0] == 33068
    metadata = probe(target)
    assert metadata["streams"][0]["codec_name"] == "h264"
    validate_decode(target)


@pytest.mark.parametrize(
    "mode,code", [("gap", "download_sequence"), ("early-end", "download_truncated")]
)
def test_bad_transfer_stops_and_releases_session(bridge_server, tmp_path, mode, code):
    with bridge_server(mode) as (port, state):
        with pytest.raises(CameraError) as error, CameraClient(settings(port)) as camera:
            camera.download(2, datetime(2026, 10, 29, 11, 28), tmp_path / "partial")
        assert error.value.code == code
        assert state["closed"] == 1


def test_read_deadline_does_not_wait_forever(bridge_server):
    with bridge_server() as (port, _):
        bridge = AndroidBridge("127.0.0.1", port, TOKEN)
        bridge.open()
        before = time.monotonic()
        try:
            with pytest.raises(CameraError) as error:
                bridge.read_exact(7, 4, 12, time.monotonic() + 0.03)
            assert error.value.code == "read_timeout"
            assert time.monotonic() - before < 1
        finally:
            bridge.close()


def test_wrong_bridge_token_is_rejected(bridge_server):
    with bridge_server() as (port, state):
        bridge = AndroidBridge("127.0.0.1", port, "wrong-test-token-not-real-1234567890")
        bridge.open()
        try:
            with pytest.raises(CameraError) as error:
                bridge.rpc("status")
            assert error.value.code == "authentication"
            assert not state["writes"]
        finally:
            bridge.close()


def test_bridge_will_not_expose_credentials_to_non_loopback_address():
    with pytest.raises(CameraError) as error:
        AndroidBridge("192.168.1.2", 8766, TOKEN)
    assert error.value.code == "bridge_address"
