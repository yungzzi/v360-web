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
