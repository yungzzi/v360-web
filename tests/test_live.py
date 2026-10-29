"""Synthetic transport and media checks; real-camera evidence is separate."""

import struct
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from v360_web import live
from v360_web import protocol as wire
from v360_web.api import create_app
from v360_web.client import CameraClient
from v360_web.config import Settings, write_private_json
from v360_web.errors import CameraError
from v360_web.media import find_tool, make_live_preview, probe, validate_decode


class LivePeer:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.frames = 0
        self.commands = []
        self.closed = False
        self.now = 0
        self.data = b""

    def open(self):
        pass

    def initialize(self, value):
        pass

    def connect(self, *args):
        return 7

    def write(self, session, channel, data):
        self.commands.append(wire.parse_command_header(data[:24])[0])

    def read_exact(self, session, channel, size, deadline):
        if channel == 0:
            return wire.command_frame(wire.LOGIN, bytes(4))[:24] if size == 24 else bytes(4)
        self.now += 0.2
        if size == 32:
            self.frames += 1
            flag = wire.LIVE_FIRST if self.frames == 1 else wire.LIVE_VIDEO
            seq = self.frames + (1 if self.mode == "gap" and self.frames == 2 else 0)
            codec = 5 if self.mode != "unknown-codec" else 99
            if self.mode == "bad-header":
                return bytes(32)
            if self.mode == "no-key":
                flag = wire.LIVE_VIDEO
            self.data = b"\0\0\0\1\x40\x01test"
            return struct.pack(
                "<7I4B",
                flag,
                len(self.data),
                123456,
                1 if self.frames == 1 else 2,
                seq,
                1,
                codec,
                0,
                0,
                0,
                0,
            )
        return self.data

    def close_session(self, session):
        pass

    def close(self):
        self.closed = True


def camera(peer):
    return CameraClient(
        Settings(did="MATE-TEST", password="example", bridge_token="", sdk_init=""),
        transport=peer,
    )


def test_live_wire_layout_and_device_hevc_type_difference():
    assert wire.live_payload(2, 0x12345678) == bytes.fromhex(
        "0200000000000000000000000000000000007856"
    )
    # Physical header sample: codec=5 means HEVC despite frame_type=1.
    header = struct.pack("<7I4B", 428213528, 40085, 123456, 1, 1, 1, 5, 0, 0, 0, 0)
    parsed = wire.parse_live_header(header)
    assert parsed.codec == 5 and parsed.frame_type == 1 and parsed.size == 40085


def test_live_capture_keeps_alive_stops_and_releases(monkeypatch, tmp_path):
    peer = LivePeer()
    monkeypatch.setattr(live.time, "monotonic", lambda: peer.now)
    target = tmp_path / "video.hevc"
    with camera(peer) as client:
        summary = live.capture_video(client, target, 6)
    assert summary["stop_requested"] and summary["frames"] >= 14
    assert peer.commands[:2] == [wire.LOGIN, wire.LIVE_START]
    assert wire.KEEP_ALIVE in peer.commands and peer.commands[-1] == wire.LIVE_STOP
    assert peer.closed and summary["bytes"] == target.stat().st_size
    assert target.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "mode,error",
    [
        ("gap", "live_sequence"),
        ("unknown-codec", "live_format"),
        ("bad-header", "live_header"),
        ("no-key", "live_start"),
    ],
)
def test_bad_live_stream_stops_and_releases(mode, error, monkeypatch, tmp_path):
    peer = LivePeer(mode)
    monkeypatch.setattr(live.time, "monotonic", lambda: peer.now)
    with pytest.raises(CameraError) as exc, camera(peer) as client:
        live.capture_video(client, tmp_path / "bad.hevc", 1)
    assert exc.value.code == error
    assert peer.closed and peer.commands[-1] == wire.LIVE_STOP


@pytest.mark.parametrize("seconds", [0, 61, float("nan"), float("inf")])
def test_bad_duration_does_not_connect_or_read_config(seconds, tmp_path):
    with pytest.raises(CameraError) as exc:
        live.capture(tmp_path / "nonexistent.json", seconds)
    assert exc.value.code == "live_duration"


def test_live_size_bound_and_exclusive_output(monkeypatch, tmp_path):
    peer = LivePeer()
    monkeypatch.setattr(live.time, "monotonic", lambda: peer.now)
    monkeypatch.setattr(live, "MAX_CAPTURE_BYTES", 1)
    with pytest.raises(CameraError, match="storage limit"), camera(peer) as client:
        live.capture_video(client, tmp_path / "small.hevc", 1)
    assert peer.commands[-1] == wire.LIVE_STOP and peer.closed
    peer = LivePeer()
    target = tmp_path / "existing.hevc"
    target.write_bytes(b"preserve")
    with pytest.raises(FileExistsError), camera(peer) as client:
        live.capture_video(client, target, 1)
    assert peer.commands == [wire.LOGIN] and target.read_bytes() == b"preserve"


def test_raw_hevc_preview_preserves_bitstream_rate_and_decodes(tmp_path):
    raw = tmp_path / "synthetic.hevc"
    subprocess.run(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x96:r=5:d=2",
            "-c:v",
            "libx265",
            "-threads",
            "1",
            "-x265-params",
            "pools=1:frame-threads=1:log-level=error",
            "-an",
            str(raw),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    target = tmp_path / "preview.mp4"
    summary = make_live_preview(raw, target, probe(raw))
    info = probe(target)
    validate_decode(target)
    assert summary["preview_frame_rate"] == "5"
    assert info["streams"][0]["codec_name"] == "h264"
    assert float(info["format"]["duration"]) == pytest.approx(2, abs=0.05)


def test_live_api_auth_verified_media_range_and_path_boundary(tmp_path, media_bytes):
    token = "test-only-web-token-123456789012345678901234"
    headers = {"Authorization": "Bearer " + token}
    key = "a" * 32
    directory = tmp_path / "live" / key
    directory.mkdir(parents=True)
    target = directory / "preview.mp4"
    target.write_bytes(media_bytes)
    route = f"/api/live/captures/{key}/file"
    with TestClient(create_app(tmp_path, token)) as client:
        assert client.get(route).status_code == 401
        assert client.get(route, headers=headers).status_code == 404
        write_private_json(directory / "capture.json", {"state": "verified"})
        response = client.get(route, headers=headers)
        assert response.status_code == 200 and response.content == media_bytes
        assert response.headers["content-type"] == "video/mp4"
        response = client.get(route, headers={**headers, "Range": "bytes=0-19"})
        assert response.status_code == 206 and response.content == media_bytes[:20]
        outside = tmp_path.parent / "outside-preview.mp4"
        outside.write_bytes(b"outside")
        target.unlink()
        target.symlink_to(outside)
        assert client.get(route, headers=headers).status_code == 404
        assert client.get("/api/live/captures/invalid/file", headers=headers).status_code == 404


def test_live_job_uses_serial_camera_queue_and_validates_duration(tmp_path, monkeypatch):
    token = "test-only-web-token-123456789012345678901234"
    headers = {"Authorization": "Bearer " + token}
    calls = []

    def captured(config, seconds, resolution):
        calls.append((config, seconds, resolution))
        return {"state": "verified", "id": "b" * 32}

    monkeypatch.setattr(live, "capture", captured)
    config = tmp_path / "device.json"
    with TestClient(create_app(tmp_path, token, config)) as client:
        assert client.post("/api/jobs/live", json={}).status_code == 401
        assert (
            client.post("/api/jobs/live", json={"seconds": 61}, headers=headers).status_code == 422
        )
        response = client.post(
            "/api/jobs/live", json={"seconds": 3, "resolution": 2}, headers=headers
        )
        assert response.status_code == 202
        key = response.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{key}", headers=headers).json()
            if job["state"] == "complete":
                break
            time.sleep(0.01)
        assert job["state"] == "complete" and calls == [(config, 3, 2)]
