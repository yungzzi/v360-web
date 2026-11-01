"""Verified type-29 three-view layout: primary plus two regions of channel 5."""

from .errors import CameraError


def views(settings):
    result = [{"id": 0, "name": "主镜头", "recordings": True, "ptz": True}]
    if getattr(settings, "live_layout", "single") == "dual-three":
        result += [
            {"id": i, "name": name, "recordings": False, "ptz": False}
            for i, name in [(1, "镜头二"), (2, "镜头三")]
        ]
    return result


def source_view(settings, lens):
    if type(lens) is not int or lens not in [v["id"] for v in views(settings)]:
        raise CameraError("lens_unavailable", "This view is not configured.")
    return (settings.live_channel, None) if lens == 0 else (5, "left" if lens == 1 else "right")


def crop_filter(region):
    if region not in ("left", "right"):
        raise CameraError("lens_unavailable", "Unsupported view region.")
    # Native renderMode 1/2: [0,.2,.6,.8] and [.4,.2,1,.8].
    x = "0" if region == "left" else "iw*2/5"
    return f"crop=iw*3/5:ih*3/5:{x}:ih/5,scale=640:360"
