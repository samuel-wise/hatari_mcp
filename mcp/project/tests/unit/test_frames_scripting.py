from __future__ import annotations

import asyncio
import functools
import hashlib
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hatari_mcp.errors import error
from hatari_mcp.tools import frames, scripting

def async_test(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


PIXELS_24 = [
    [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
    [(255, 255, 255), (0, 0, 0), (10, 20, 30)],
]


def make_bmp(
    width: int,
    height: int,
    pixels: list[list[tuple[int, ...]]],
    *,
    bpp: int = 24,
    top_down: bool = False,
) -> bytes:
    stride = ((width * bpp + 31) // 32) * 4
    data = bytearray(54 + stride * height)
    data[0:2] = b"BM"
    struct.pack_into("<I", data, 2, len(data))
    struct.pack_into("<I", data, 10, 54)
    struct.pack_into("<I", data, 14, 40)
    struct.pack_into("<i", data, 18, width)
    struct.pack_into("<i", data, 22, -height if top_down else height)
    struct.pack_into("<H", data, 26, 1)
    struct.pack_into("<H", data, 28, bpp)
    struct.pack_into("<I", data, 30, 0)
    struct.pack_into("<I", data, 34, stride * height)
    step = bpp // 8
    for y in range(height):
        row = y if top_down else height - 1 - y
        for x, pixel in enumerate(pixels[y]):
            offset = 54 + row * stride + x * step
            red, green, blue = pixel[:3]
            data[offset] = blue
            data[offset + 1] = green
            data[offset + 2] = red
            if bpp == 32:
                data[offset + 3] = pixel[3]
    return bytes(data)


def rgba_bytes(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    assert len(pixels) == width * height
    return bytes(channel for pixel in pixels for channel in pixel)


class FakeToolServer:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorator(function):
            self.tools[kwargs["name"]] = function
            return function

        return decorator


class FakeSupervisor:
    def __init__(self, bmp: bytes | None = None) -> None:
        self.commands: list[str] = []
        self.debug_commands: list[str] = []
        self.runtime = None
        self.bmp = bmp or make_bmp(3, 2, PIXELS_24)

    async def _socket_command(self, command: str) -> None:
        self.commands.append(command)

    async def debug(self, command: str, **_kwargs: Any) -> dict[str, Any]:
        self.debug_commands.append(command)
        if command.startswith("screenshot "):
            target = Path(command.split('"', 2)[1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.bmp)
        return {
            "ok": True,
            "raw": "",
            "lines": [],
            "channel": "socket",
            "state": "running",
            "timed_out": False,
        }


class FakeApp:
    def __init__(self, root: Path, bmp: bytes | None = None) -> None:
        self.root = root
        self.supervisor = FakeSupervisor(bmp)
        self.supervisor.runtime = SimpleNamespace(path=root)

    def guard_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        target = Path(path).expanduser().resolve()
        if must_exist and not target.exists():
            raise error("invalid_path", f"Path does not exist: {target}", "Check the path.")
        return target


def build_tools(tmp_path: Path, bmp: bytes | None = None):
    server = FakeToolServer()
    app = FakeApp(tmp_path, bmp)
    frames.register(server, app)
    scripting.register(server, app)
    return server.tools, app


def test_decode_bmp_24_bottom_up_padding_and_bgr():
    image = frames.decode_bmp(make_bmp(3, 2, PIXELS_24))

    assert image.width == 3
    assert image.height == 2
    assert image.bpp == 24
    assert image.top_down is False
    assert image.pixel(0, 0) == (255, 0, 0, 255)
    assert image.pixel(1, 0) == (0, 255, 0, 255)
    assert image.pixel(2, 0) == (0, 0, 255, 255)
    assert image.pixel(0, 1) == (255, 255, 255, 255)
    assert image.pixel(1, 1) == (0, 0, 0, 255)
    assert image.pixel(2, 1) == (10, 20, 30, 255)


def test_decode_bmp_32_top_down_and_opaque_alpha_fallback():
    pixels = [
        [(10, 20, 30, 0), (1, 2, 3, 0)],
        [(4, 5, 6, 0), (7, 8, 9, 0)],
    ]
    image = frames.decode_bmp(make_bmp(2, 2, pixels, bpp=32, top_down=True))

    assert image.top_down is True
    assert image.bpp == 32
    assert image.pixel(0, 0) == (10, 20, 30, 255)
    assert image.pixel(1, 1) == (7, 8, 9, 255)


def test_decode_bmp_32_preserves_nonzero_alpha():
    pixels = [[(1, 2, 3, 128), (4, 5, 6, 64)]]
    image = frames.decode_bmp(make_bmp(2, 1, pixels, bpp=32))

    assert image.pixel(0, 0) == (1, 2, 3, 128)
    assert image.pixel(1, 0) == (4, 5, 6, 64)


def test_write_bmp24_roundtrip(tmp_path):
    pixels = [(1, 2, 3, 255), (4, 5, 6, 255), (7, 8, 9, 255), (10, 11, 12, 255)]
    target = tmp_path / "roundtrip.bmp"
    frames.write_bmp24(target, 2, 2, rgba_bytes(2, 2, pixels))

    image = frames.read_bmp(target)

    assert image.width == 2
    assert image.height == 2
    assert image.pixel(0, 0) == (1, 2, 3, 255)
    assert image.pixel(1, 1) == (10, 11, 12, 255)


@async_test
async def test_screen_frame_capture_pixel_and_region_hash(tmp_path):
    tools, app = build_tools(tmp_path)

    captured = await tools["screen_frame_capture"](path=str(tmp_path / "frame.bmp"))
    assert captured["width"] == 3
    assert captured["height"] == 2
    assert captured["bytes"] > 54
    assert captured["format"] == "bmp"
    assert captured["sha256"] == frames._file_sha256(tmp_path / "frame.bmp")
    assert app.supervisor.debug_commands

    pixel = await tools["screen_pixel_get"](x=2, y=1)
    assert pixel["rgba"] == [10, 20, 30, 255]
    assert pixel["hex"] == "#0a141e"

    region = await tools["screen_region_hash"](x=0, y=0, width=1, height=1)
    assert region["width"] == 1
    assert region["height"] == 1
    assert region["hash"] == region["region_sha256"]
    assert region["region_sha256"] == hashlib.sha256(bytes((255, 0, 0, 255))).hexdigest()

    with pytest.raises(ToolError):
        await tools["screen_pixel_get"](x=99, y=0)


@async_test
async def test_screen_wait_stable_matches_identical_consecutive_frames(tmp_path):
    tools, _app = build_tools(tmp_path)

    result = await tools["screen_wait_stable"](timeout_ms=100, interval_ms=0, threshold=0)

    assert result["stable"] is True
    assert result["frame_count"] == 2
    assert result["timed_out"] is False
    assert result["vbl_locked"] is False
    assert result["samples"][0]["sha256"] == result["samples"][1]["sha256"]


@async_test
async def test_screen_frame_diff_reports_bbox_and_writes_diff_image(tmp_path):
    tools, _app = build_tools(tmp_path)
    left_path = tmp_path / "left.bmp"
    right_path = tmp_path / "right.bmp"
    left = rgba_bytes(2, 2, [(0, 0, 0, 255)] * 4)
    right_pixels = [(255, 0, 0, 255), (0, 0, 0, 255), (0, 0, 0, 255), (0, 255, 0, 255)]
    frames.write_bmp24(left_path, 2, 2, left)
    frames.write_bmp24(right_path, 2, 2, rgba_bytes(2, 2, right_pixels))

    result = await tools["screen_frame_diff"](path_a=str(left_path), path_b=str(right_path))

    assert result["equal"] is False
    assert result["changed_pixels"] == 2
    assert result["total_pixels"] == 4
    assert result["bbox"] == {"x": 0, "y": 0, "width": 2, "height": 2}
    diff_path = Path(result["diff_path"])
    assert diff_path.is_file()
    assert frames.read_bmp(diff_path).width == 2
    diff_path.unlink()


@async_test
async def test_input_sequence_executes_events_in_order(tmp_path):
    tools, app = build_tools(tmp_path)

    result = await tools["input_sequence"](
        events=[
            {"type": "keydown", "key": "ctrl"},
            {"type": "key", "key": "a"},
            {"type": "keyup", "key": "ctrl"},
            {"type": "text", "text": "a"},
            {"type": "mouse_move", "dx": 5, "dy": -3},
            {"type": "mouse_button", "button": "left", "action": "click"},
        ],
        interval_ms=0,
    )

    assert result["ok"] is True
    assert result["host_timed"] is True
    assert result["frame_locked"] is False
    assert result["count"] == 6
    assert app.supervisor.commands == [
        "hatari-event keydown 29",
        "hatari-event keypress 30",
        "hatari-event keyup 29",
        "hatari-event keypress 30",
        "hatari-event mousemove 5 -3",
        "hatari-event leftdown",
        "hatari-event leftup",
    ]


@async_test
async def test_input_sequence_rejects_invalid_events_before_running(tmp_path):
    tools, app = build_tools(tmp_path)

    with pytest.raises(ToolError) as excinfo:
        await tools["input_sequence"](events=[{"type": "mystery"}], interval_ms=0)

    assert "bad_option" in str(excinfo.value)
    assert app.supervisor.commands == []


@async_test
async def test_input_key_combo_presses_and_releases_reversed(tmp_path):
    tools, app = build_tools(tmp_path)

    result = await tools["input_key_combo"](["ctrl", "c"], hold_ms=0)

    assert result["scancodes"] == [29, 32]
    assert app.supervisor.commands == [
        "hatari-event keydown 29",
        "hatari-event keydown 32",
        "hatari-event keyup 32",
        "hatari-event keyup 29",
    ]


@async_test
async def test_input_mouse_click_rejects_absolute_and_clicks_current_position(tmp_path):
    tools, app = build_tools(tmp_path)

    with pytest.raises(ToolError) as excinfo:
        await tools["input_mouse_click"](x=10, y=20)
    assert "unsupported" in str(excinfo.value)
    assert app.supervisor.commands == []

    result = await tools["input_mouse_click"](button="right")

    assert result["absolute"] is False
    assert result["button"] == "right"
    assert app.supervisor.commands == ["hatari-event rightdown", "hatari-event rightup"]


@async_test
async def test_input_sequence_save_load_and_replay(tmp_path):
    tools, app = build_tools(tmp_path)
    path = tmp_path / "sequence.json"
    events = [
        {"type": "key", "key": "a"},
        {"type": "mouse_move", "dx": 1, "dy": 2},
    ]

    saved = await tools["input_sequence_save"](path=str(path), events=events)
    loaded = await tools["input_sequence_load"](path=str(path))
    replayed = await tools["input_replay"](path=str(path), interval_ms=0)

    assert saved["count"] == 2
    assert path.is_file()
    assert loaded["events"] == events
    assert replayed["path"] == str(path.resolve())
    assert replayed["count"] == 2
    assert app.supervisor.commands == [
        "hatari-event keypress 30",
        "hatari-event mousemove 1 2",
    ]
