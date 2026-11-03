import struct
from fractions import Fraction
from types import SimpleNamespace

import pytest

from v360_web import media_recovery as recovery
from v360_web.errors import CameraError


def box(kind, content):
    return struct.pack(">I4s", 8 + len(content), kind) + content


def nal(kind, first=True):
    content = bytes([kind << 1, 1, 128 if first else 0, 73])
    return struct.pack(">I", len(content)) + content


def fixture_mdat(tail=b""):
    return b"".join(nal(k) for k in (32, 33, 34, 19)) + b"\xd5" * 320 + nal(1) + tail


def original(data):
    return box(b"ftyp", b"isom") + box(b"moov", b"index") + box(b"mdat", data)


def test_extract_accounts_for_every_byte_without_searching_for_a_new_offset():
    video, audio, frames = recovery.extract_tracks(original(fixture_mdat()))
    assert frames == 2 and audio == b"\xd5" * 320
    assert video == b"".join(b"\0\0\0\1" + nal(k)[4:] for k in (32, 33, 34, 19, 1))
    with pytest.raises(CameraError):
        recovery.extract_tracks(original(fixture_mdat(b"\x01")))


def test_wrong_box_lengths_and_multislice_variant_are_not_silently_repaired():
    for value in (
        original(fixture_mdat())[:-1],
        original(b"".join(nal(k) for k in (32, 33, 34, 19)) + nal(1, False)),
    ):
        with pytest.raises(CameraError) as error:
            recovery.extract_tracks(value)
        assert error.value.code == "source_recovery_unavailable"


def test_single_still_frame_cannot_be_extended_into_a_recording():
    with pytest.raises(CameraError):
        recovery.extract_tracks(original(b"".join(nal(k) for k in (32, 33, 34, 19))))


def test_parameter_clock_is_evidence_not_a_default_frame_rate(monkeypatch, tmp_path):
    trace = b"""vui_num_units_in_tick 000101 = 6000
vui_time_scale 000111 = 90000
vui_poc_proportional_to_timing_flag 1 = 1
vui_num_ticks_poc_diff_one_minus1 010 = 1
"""
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=trace)
    )
    assert recovery.parameter_rate(tmp_path / "synthetic.hevc") == Fraction(15, 1)
    trace = b"no explicit clock values"
    with pytest.raises(CameraError):
        recovery.parameter_rate(tmp_path / "synthetic.hevc")


def test_inconsistent_parameter_sets_cannot_choose_a_convenient_rate(monkeypatch, tmp_path):
    trace = b"vui_num_units_in_tick 00 = 6000\nvui_num_units_in_tick 01 = 3000\nvui_time_scale 10 = 90000\nvui_poc_proportional_to_timing_flag 1 = 1\nvui_num_ticks_poc_diff_one_minus1 1 = 0\n"
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=trace)
    )
    with pytest.raises(CameraError):
        recovery.parameter_rate(tmp_path / "synthetic.hevc")


@pytest.mark.parametrize("fail_stage", [None, "clock", "duration", "decode", "consumer"])
def test_recovery_keeps_original_and_cleans_temporary_files(monkeypatch, tmp_path, fail_stage):
    import hashlib

    key = "a" * 32
    source = tmp_path / "media" / (key + ".media")
    source.parent.mkdir()
    content = original(fixture_mdat())
    source.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    seconds = 2 / 15
    item = {
        "id": key, "device_id": "BFEP-test", "sha256": sha,
        "duration_raw": seconds if fail_stage != "duration" else 30,
    }
    video = {"codec_type": "video", "codec_name": "hevc", "width": 1920,
             "height": 1080, "nb_frames": "1", "r_frame_rate": "15/1"}
    original_info = {"streams": [video], "format": {"duration": "0"}}
    repaired_info = {"streams": [{**video, "nb_frames": "2"}],
                     "format": {"duration": str(seconds)}}
    monkeypatch.setattr(recovery, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(recovery, "admit", lambda *a, **kw: None)
    monkeypatch.setattr(recovery, "record", lambda *a, **kw: None)
    monkeypatch.setattr(recovery, "probe", lambda path: repaired_info)

    def clock(path):
        if fail_stage == "clock":
            raise recovery.unavailable()
        return Fraction(15, 1)

    def remux(command, **kw):
        from pathlib import Path
        Path(command[-1]).write_bytes(b"temporary-reconstructed-file")
        return SimpleNamespace(returncode=0)

    decoded = []

    def decode(path):
        decoded.append(path)
        if fail_stage == "decode":
            raise CameraError("media_decode", "Synthetic damaged source.")

    monkeypatch.setattr(recovery, "parameter_rate", clock)
    monkeypatch.setattr(recovery, "run_media", remux)
    monkeypatch.setattr(recovery, "validate_decode", decode)

    def consume():
        with recovery.playable_source(source, item, tmp_path, original_info) as (path, info, evidence):
            assert path != source and path.is_file()
            assert info == repaired_info
            assert evidence["original_sha256"] == sha
            assert evidence["frames"] == 2
            assert decoded == [path]
            if fail_stage == "consumer":
                raise CameraError("preview_failed", "Synthetic conversion failure.")

    if fail_stage:
        with pytest.raises(CameraError):
            consume()
    else:
        consume()
    assert source.read_bytes() == content
    assert not list((tmp_path / "previews").glob("*.repair.*"))
    assert not recovery.RECOVERY_LOCK.locked()
