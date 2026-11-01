import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from v360_web.api import create_app
from v360_web.config import Settings
from v360_web.errors import CameraError
from v360_web.lenses import crop_filter, source_view, views
from v360_web.media import find_tool

TOKEN = "lens-test-token-12345678901234567890123456"


def test_layout_is_explicit_and_primary_keeps_original_channel():
    single = SimpleNamespace(live_channel=9)
    assert source_view(single, 0) == (9, None)
    with pytest.raises(CameraError):
        source_view(single, 1)
    triple = SimpleNamespace(live_layout="dual-three", live_channel=2)
    assert [v["recordings"] for v in views(triple)] == [True, False, False]
    assert source_view(triple, 1) == (5, "left")
    assert source_view(triple, 2) == (5, "right")


@pytest.mark.parametrize("lens", [-1, 3, True, "1", None])
def test_invalid_view_never_subscribes(tmp_path, monkeypatch, lens):
    app = create_app(tmp_path, TOKEN)
    monkeypatch.setattr(
        app.state.streams, "subscribe", lambda *a: pytest.fail("must not subscribe")
    )
    with TestClient(app) as client, client.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"token": TOKEN, "lens": lens})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008


def test_status_advertises_only_configured_views_and_aux_cannot_move(tmp_path, monkeypatch):
    settings = Settings(
        did="test-device",
        password="test",
        bridge_token="test",
        sdk_init="test",
        data_root=tmp_path,
        live_layout="dual-three",
    )
    monkeypatch.setattr(Settings, "load", lambda path: settings)
    app = create_app(tmp_path, TOKEN, tmp_path / "unused")
    with TestClient(app) as client:
        result = client.get("/api/status", headers={"Authorization": "Bearer " + TOKEN}).json()
        assert len(result["lenses"]) == 3
        app.state.streams.lens = 1
        with pytest.raises(CameraError) as error:
            app.state.streams.move("not-a-viewer", 1)
        assert error.value.code == "lens_controls"


@pytest.mark.parametrize(
    "region,expected", [("left", bytes([255, 0, 0])), ("right", bytes([255, 255, 0]))]
)
def test_native_crop_regions_exclude_top_bottom_and_select_different_sides(region, expected):
    # Synthetic colored frame, not camera footage: blue outside y=[.2,.8].
    raw = bytearray()
    for y in range(360):
        for x in range(640):
            raw.extend(
                (0, 0, 255)
                if not 72 <= y < 288
                else (255, 0, 0)
                if x < 256
                else (0, 255, 0)
                if x < 384
                else (255, 255, 0)
            )
    result = subprocess.run(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            "640x360",
            "-i",
            "pipe:0",
            "-vf",
            crop_filter(region),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        input=raw,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    assert len(result) == 640 * 360 * 3
    middle = (180 * 640 + 320) * 3
    assert result[middle : middle + 3] == expected
    assert not any(result[2::3])
