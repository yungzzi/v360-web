"""Conservative recovery of the observed unfinished HEVC/ALaw MP4 variant.

Original bytes stay untouched. Every mdat byte must belong to a supported NAL
or one 320-byte audio block. Timing must come from HEVC VUI and agree with the
camera directory. A decode-only success or a guessed frame rate is insufficient.
"""

import math
import os
import re
import struct
import subprocess
import threading
from contextlib import contextmanager
from fractions import Fraction

from .diagnostics import record
from .errors import CameraError
from .maintenance import cleanup, owned
from .media import find_tool, probe, run_media, validate_decode, validate_recording
from .progress import check_cancelled
from .storage import admit

RECOVERY_LOCK = threading.Lock()
MAX_SOURCE = 16 * 1024**2
SUFFIXES = (".repair.hevc", ".repair.alaw", ".repair.mp4")


def unavailable():
    return CameraError(
        "source_recovery_unavailable", "Original index cannot be safely reconstructed."
    )


def extract_tracks(value):
    if len(value) > MAX_SOURCE:
        raise unavailable()
    position, tracks, kinds = 0, [], []
    while position + 8 <= len(value):
        size, kind = struct.unpack_from(">I4s", value, position)
        header = 8
        if size == 1:
            if position + 16 > len(value):
                raise unavailable()
            size, header = struct.unpack_from(">Q", value, position + 8)[0], 16
        elif size == 0:
            size = len(value) - position
        if size < header or position + size > len(value):
            raise unavailable()
        kinds.append(kind)
        if kind == b"mdat":
            tracks.append(value[position + header : position + size])
        position += size
    if position != len(value) or not kinds or kinds[0] != b"ftyp" or len(tracks) != 1:
        raise unavailable()
    data = tracks[0]
    video, audio, kinds = bytearray(), bytearray(), []
    position = frames = 0
    headers = {bytes([kind << 1, 1]): kind for kind in (1, 19, 20, 32, 33, 34)}
    while position < len(data):
        size = struct.unpack_from(">I", data, position)[0] if position + 6 <= len(data) else -1
        kind = headers.get(data[position + 4 : position + 6])
        if kind is not None and 3 <= size <= len(data) - position - 4:
            nal = data[position + 4 : position + 4 + size]
            if kind < 32:
                if not nal[2] & 0x80:  # Only the observed one-slice-per-frame variant.
                    raise unavailable()
                frames += 1
            video.extend(b"\0\0\0\1" + nal)
            kinds.append(kind)
            position += 4 + size
        elif kinds and position + 320 <= len(data):
            audio.extend(data[position : position + 320])
            position += 320
        else:
            raise unavailable()
    if (
        kinds[:4] not in ([32, 33, 34, 19], [32, 33, 34, 20])
        or frames < 2
        or any(kinds.count(kind) != 1 for kind in (32, 33, 34))
    ):
        raise unavailable()
    return bytes(video), bytes(audio), frames


def parameter_rate(path):
    result = subprocess.run(
        [
            find_tool("ffmpeg"),
            "-v",
            "verbose",
            "-i",
            str(path),
            "-c:v",
            "copy",
            "-frames:v",
            "1",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise unavailable()
    fields = {}
    for name, value in re.findall(
        r"\b(vui_num_units_in_tick|vui_time_scale|vui_poc_proportional_to_timing_flag|vui_num_ticks_poc_diff_one_minus1)\s+[01]+\s+=\s+(\d+)",
        result.stderr.decode("ascii", errors="ignore"),
    ):
        fields.setdefault(name, set()).add(int(value))
    if any(len(values) != 1 for values in fields.values()):
        raise unavailable()
    fields = {name: next(iter(values)) for name, values in fields.items()}
    if fields.get("vui_poc_proportional_to_timing_flag") != 1:
        raise unavailable()
    tick = fields.get("vui_num_units_in_tick", 0)
    scale = fields.get("vui_time_scale", 0)
    ticks = fields.get("vui_num_ticks_poc_diff_one_minus1", -1) + 1
    if not tick or not scale or not ticks:
        raise unavailable()
    rate = Fraction(scale, tick)
    if not 1 <= rate <= 60:
        raise unavailable()
    return rate


@contextmanager
def recovery_slot():
    while not RECOVERY_LOCK.acquire(timeout=0.2):
        check_cancelled()
    try:
        check_cancelled()
        yield
    finally:
        RECOVERY_LOCK.release()


@contextmanager
def playable_source(source, item, root, info=None):
    info = probe(source) if info is None else info
    try:
        validate_recording(info)
    except CameraError as error:
        if error.code != "media_incomplete":
            raise
    else:
        yield source, info, None
        return
    original_video = next(
        (v for v in info.get("streams", []) if v.get("codec_type") == "video"), {}
    )
    if (
        source.stat().st_size > MAX_SOURCE
        or not item.get("device_id", "").startswith("BFEP")
        or original_video.get("codec_name") != "hevc"
        or str(original_video.get("nb_frames")) != "1"
    ):
        raise unavailable()
    video, audio, frames = extract_tracks(source.read_bytes())
    # The examined device variant records mono 8 kHz A-law in these blocks.
    # Repair remains deliberately limited to exact directory-duration matches.
    with recovery_slot():
        key = item["id"]
        paths = [owned(root, "previews", key, suffix) for suffix in SUFFIXES]
        if not all(paths):
            raise unavailable()
        paths[0].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in paths:
            path.unlink(missing_ok=True)
        reservation = 2 * source.stat().st_size + 1024**2
        cleanup(root, reservation, kind="cache", protected=key)
        admit(root, reservation, "cache")
        try:
            for path, content in zip(paths[:2], (video, audio), strict=True):
                with path.open("wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(content)
            rate = parameter_rate(paths[0])
            duration = float(Fraction(frames, 1) / rate)
            expected = item["duration_raw"]
            if not 0 < expected <= 3600 or abs(duration - expected) > 1 / float(rate):
                raise unavailable()
            raw_info = probe(paths[0])
            stream = next(s for s in raw_info["streams"] if s["codec_type"] == "video")
            if Fraction(stream.get("r_frame_rate", "0/1")) != rate:
                raise unavailable()
            original = next(s for s in info["streams"] if s["codec_type"] == "video")
            if any(stream.get(k) != original.get(k) for k in ("width", "height", "codec_name")):
                raise unavailable()
            command = [
                find_tool("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-y",
                "-r",
                str(rate),
                "-i",
                str(paths[0]),
            ]
            if audio:
                if not math.isfinite(len(audio) / 8000) or len(audio) / 8000 > duration + 0.1:
                    raise unavailable()
                command += ["-f", "alaw", "-ar", "8000", "-ac", "1", "-i", str(paths[1])]
            command += ["-map", "0:v:0", "-c:v", "copy"]
            if audio:
                command += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "64k"]
            command += ["-movflags", "+faststart", str(paths[2])]
            result = run_media(command, capture_output=True, check=False, timeout=60)
            if result.returncode:
                raise unavailable()
            paths[2].chmod(0o600)
            repaired = probe(paths[2])
            repaired_video = next(s for s in repaired["streams"] if s["codec_type"] == "video")
            if (
                int(repaired_video.get("nb_frames", 0)) != frames
                or abs(float(repaired["format"]["duration"]) - duration) > 0.1
            ):
                raise unavailable()
            validate_decode(paths[2])
            evidence = {
                "method": "hevc_vui_mdat",
                "frames": frames,
                "fps": str(rate),
                "seconds": duration,
                "audio_bytes": len(audio),
                "original_sha256": item["sha256"],
            }
            record(root, "source_repaired", key=key, ok=True, source_sha256=item["sha256"])
            yield paths[2], repaired, evidence
        except (ValueError, StopIteration, OSError, subprocess.TimeoutExpired) as error:
            raise unavailable() from error
        finally:
            for path in paths:
                path.unlink(missing_ok=True)
