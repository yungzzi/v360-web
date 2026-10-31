import struct
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from v360_web import protocol as wire
from v360_web.errors import CameraError
from v360_web.streaming import camera_frames


def video(seq=1):
    body = b"\0\0\0\1test-video"
    return struct.pack(
        "<7I4B",
        wire.LIVE_FIRST if seq == 1 else wire.LIVE_VIDEO,
        len(body),
        1,
        1 if seq == 1 else 2,
        seq,
        1,
        5,
        0,
        0,
        0,
        0,
    ), body


def audio(seq=1, codec=82):
    body = bytes([0xD5]) * 320
    return struct.pack("<7I4B", wire.LIVE_AUDIO, len(body), 1, 0, seq, 1, codec, 0, 0, 0, 0), body


def source(monkeypatch, packets):
    reads = deque(part for packet in packets for part in packet)
    sent = []
    camera = SimpleNamespace(session=1, bridge=SimpleNamespace(timeout=15))
    camera.send = lambda command, body: sent.append((command, body))

    def read(session, channel, size, deadline):
        value = reads.popleft()
        assert len(value) == size
        return value

    camera.bridge.read_exact = read

    class Client:
        bridge = camera.bridge

        def __enter__(self):
            return camera

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(
        "v360_web.streaming.Settings.load",
        lambda _: SimpleNamespace(
            shared=False,
            user_id=7,
            live_channel=2,
        ),
    )
    monkeypatch.setattr("v360_web.streaming.CameraClient", lambda _: Client())
    return camera_frames(None, threading.Event()), sent


def test_audio_request_demux_and_stop_commands(monkeypatch):
    frames, sent = source(monkeypatch, [video(), audio(), video(2)])
    assert next(frames).data == b"alaw"
    assert next(frames).kind == "video"
    assert next(frames).kind == "audio"
    assert next(frames).kind == "video"
    frames.close()
    assert sent[1] == (wire.AUDIO_START, bytes(4))
    assert [x[0] for x in sent[-2:]] == [wire.AUDIO_STOP, wire.LIVE_STOP]


def test_unsupported_audio_retains_video(monkeypatch):
    frames, sent = source(monkeypatch, [video(), audio(codec=81), video(2)])
    assert next(frames).data == b""
    assert next(frames).kind == "video"
    assert next(frames).kind == "video"
    frames.close()
    assert sent[-1][0] == wire.LIVE_STOP


def test_missing_audio_falls_back_after_bounded_wait(monkeypatch):
    frames, _ = source(monkeypatch, [video()])
    ticks = iter(range(100))
    monkeypatch.setattr("v360_web.streaming.time.monotonic", lambda: next(ticks))
    assert next(frames).data == b""
    assert next(frames).kind == "video"
    frames.close()


def test_small_audio_gap_inserts_silence_large_gap_stops_cleanly(monkeypatch):
    frames, sent = source(monkeypatch, [video(), audio(), audio(3), audio(100)])
    assert next(frames).kind == "config"
    assert next(frames).kind == "video"
    assert next(frames).kind == "audio"
    silence = next(frames)
    assert silence.kind == "audio" and silence.data == bytes([0xD5]) * 320
    assert next(frames).kind == "audio"
    with pytest.raises(CameraError) as error:
        next(frames)
    assert error.value.code == "live_audio_sequence"
    assert sent[-1][0] == wire.LIVE_STOP


def test_audio_start_failure_closes_pipes_and_releases_camera(tmp_path, monkeypatch):
    import os

    from v360_web.streaming import LivePacket, StreamManager

    pipes = []
    original = os.pipe
    closed = threading.Event()

    def pipe():
        pair = original()
        pipes.extend(pair)
        return pair

    def packets(config, stop):
        try:
            yield LivePacket("config", b"alaw")
            yield LivePacket("video", b"test")
        finally:
            closed.set()

    def fail(fd):
        raise CameraError("ffmpeg_missing", "test")

    monkeypatch.setattr("v360_web.streaming.os.pipe", pipe)
    monkeypatch.setattr("v360_web.streaming.encoder_command", fail)
    lock = threading.Lock()
    manager = StreamManager(tmp_path / "unused", lock, packets)
    manager.subscribe()
    manager.thread.join(3)
    assert closed.is_set() and not lock.locked() and not manager.thread.is_alive()
    for fd in pipes:
        with pytest.raises(OSError):
            os.fstat(fd)
