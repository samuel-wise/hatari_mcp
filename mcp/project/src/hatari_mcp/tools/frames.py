"""BMP frame capture, pixel inspection, hashing and diffing."""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

MAX_PIXELS = 50_000_000
FRAME_FORMATS = {"bmp", "png"}
MAX_READ_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class BMPImage:
    width: int
    height: int
    bpp: int
    top_down: bool
    rgba: bytes

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise error(
                "out_of_bounds",
                f"Pixel ({x}, {y}) is outside {self.width}x{self.height}.",
                "Use coordinates inside the captured frame.",
            )
        offset = (y * self.width + x) * 4
        return tuple(self.rgba[offset : offset + 4])  # type: ignore[return-value]

    def region_rgba(self, x: int, y: int, width: int, height: int) -> bytes:
        if width <= 0 or height <= 0:
            raise error("bad_option", "Region width and height must be positive.", "Use width >= 1 and height >= 1.")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise error(
                "out_of_bounds",
                f"Region ({x}, {y}, {width}, {height}) is outside {self.width}x{self.height}.",
                "Use a region fully inside the captured frame.",
            )
        row_bytes = width * 4
        out = bytearray(row_bytes * height)
        for row in range(height):
            source = ((y + row) * self.width + x) * 4
            target = row * row_bytes
            out[target : target + row_bytes] = self.rgba[source : source + row_bytes]
        return bytes(out)

    def sha256(self) -> str:
        return hashlib.sha256(self.rgba).hexdigest()


def _as_int(value: Any, name: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("bad_option", f"{name} must be an integer.", "Pass a whole number.")
    if minimum is not None and value < minimum:
        raise error("bad_option", f"{name} must be >= {minimum}.", "Use a value inside the allowed range.")
    if maximum is not None and value > maximum:
        raise error("bad_option", f"{name} must be <= {maximum}.", "Use a value inside the allowed range.")
    return value


def decode_bmp(data: bytes) -> BMPImage:
    """Decode an uncompressed 24- or 32-bit BMP into top-down RGBA."""
    if len(data) < 54:
        raise error("bad_image", "BMP data is shorter than a file and info header.", "Capture a fresh BMP with screen_frame_capture.")
    if data[:2] != b"BM":
        raise error("bad_image", "Not a BMP file.", "Use an uncompressed .bmp frame.")
    try:
        offset = struct.unpack_from("<I", data, 10)[0]
        dib_size = struct.unpack_from("<I", data, 14)[0]
        width, raw_height = struct.unpack_from("<ii", data, 18)
        planes, bpp = struct.unpack_from("<HH", data, 26)
        compression = struct.unpack_from("<I", data, 30)[0]
    except struct.error as exc:
        raise error("bad_image", f"Truncated BMP header: {exc}", "Capture a fresh BMP.") from exc

    if dib_size < 40:
        raise error("unsupported_format", f"Unsupported BMP header size: {dib_size}.", "Use a BITMAPINFOHEADER BMP.")
    if planes != 1:
        raise error("bad_image", f"Unsupported BMP plane count: {planes}.", "Use a single-plane BMP.")
    if bpp not in (24, 32):
        raise error("unsupported_format", f"Unsupported BMP bit depth: {bpp}.", "Use an uncompressed 24- or 32-bit BMP.")
    if compression != 0:
        raise error("unsupported_format", f"Unsupported BMP compression: {compression}.", "Use an uncompressed BI_RGB BMP.")
    if width <= 0 or raw_height == 0:
        raise error("bad_image", f"Invalid BMP dimensions: {width}x{raw_height}.", "Capture a fresh BMP.")

    height = abs(raw_height)
    if width * height > MAX_PIXELS:
        raise error("image_too_large", f"BMP has {width * height} pixels.", "Capture a smaller region or frame.")

    stride = ((width * bpp + 31) // 32) * 4
    required = offset + stride * height
    if offset < 14 + dib_size or required > len(data):
        raise error("bad_image", "BMP pixel data is truncated.", "Capture a fresh BMP.")

    bytes_per_pixel = bpp // 8
    top_down = raw_height < 0
    out = bytearray(width * height * 4)
    alpha_nonzero = False

    for y in range(height):
        source_row = y if top_down else height - 1 - y
        source = offset + source_row * stride
        target = y * width * 4
        for x in range(width):
            pixel = source + x * bytes_per_pixel
            blue = data[pixel]
            green = data[pixel + 1]
            red = data[pixel + 2]
            alpha = data[pixel + 3] if bpp == 32 else 255
            if bpp == 32 and alpha:
                alpha_nonzero = True
            out[target + x * 4] = red
            out[target + x * 4 + 1] = green
            out[target + x * 4 + 2] = blue
            out[target + x * 4 + 3] = alpha

    if bpp == 32 and not alpha_nonzero:
        out[3::4] = b"\xff" * (width * height)

    return BMPImage(width=width, height=height, bpp=bpp, top_down=top_down, rgba=bytes(out))


def read_bmp(path: Path) -> BMPImage:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise error("invalid_path", f"Cannot stat frame: {exc}", "Check the output path.") from exc
    if size > MAX_READ_BYTES:
        raise error("image_too_large", f"Frame is {size} bytes.", "Use a smaller capture.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise error("invalid_path", f"Cannot read frame: {exc}", "Check the output path.") from exc
    return decode_bmp(data)


def write_bmp24(path: Path, width: int, height: int, rgba: bytes) -> None:
    if width <= 0 or height <= 0:
        raise error("bad_option", "BMP width and height must be positive.", "Use positive dimensions.")
    if len(rgba) != width * height * 4:
        raise error("bad_image", "RGBA buffer size does not match dimensions.", "Pass width * height * 4 bytes.")
    stride = ((width * 24 + 31) // 32) * 4
    pixel_bytes = stride * height
    offset = 14 + 40
    header = struct.pack("<2sIHHI", b"BM", offset + pixel_bytes, 0, 0, offset)
    info = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        pixel_bytes,
        2835,
        2835,
        0,
        0,
    )
    out = bytearray(header + info + b"\x00" * pixel_bytes)
    for y in range(height):
        target_row = height - 1 - y
        target = offset + target_row * stride
        source = y * width * 4
        for x in range(width):
            pixel = source + x * 4
            out[target + x * 3] = rgba[pixel + 2]
            out[target + x * 3 + 1] = rgba[pixel + 1]
            out[target + x * 3 + 2] = rgba[pixel]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(out)
    except OSError as exc:
        raise error("invalid_path", f"Cannot write BMP: {exc}", "Check the output directory.") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_path(app: App, fmt: str) -> Path:
    runtime = app.supervisor.runtime
    base = runtime.path if runtime else Path("/tmp") / "hatari_mcp_frames"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"frame_{uuid.uuid4().hex}.{fmt}"


async def _capture(app: App, target: Path, fmt: str) -> dict[str, Any]:
    if '"' in str(target):
        raise error("bad_option", "Capture paths cannot contain a double quote.", "Use a simple path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    result = await app.supervisor.debug(f'screenshot "{target}"', timeout_ms=10000)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if target.is_file() and target.stat().st_size > 0:
            break
        await asyncio.sleep(0.02)
    if not target.is_file() or target.stat().st_size == 0:
        raise error(
            "capture_failed",
            f"Hatari did not write the {fmt} screenshot.",
            "Check log_read; rendering must be enabled and the path writable.",
            raw=result.get("raw"),
        )
    return {
        "result": result,
        "size": target.stat().st_size,
        "sha256": _file_sha256(target),
    }


async def _capture_bmp(app: App, target: Path | None = None) -> tuple[Path, BMPImage]:
    path = target or _capture_path(app, "bmp")
    info = await _capture(app, path, "bmp")
    return path, read_bmp(path)


def _capture_summary(app: App, target: Path) -> dict[str, Any]:
    runtime = app.supervisor.runtime
    return {
        "path": str(target),
        "runtime_dir": str(runtime.path) if runtime else None,
    }


def _max_channel_delta(left: bytes, right: bytes) -> int:
    if len(left) != len(right):
        raise error("dimension_mismatch", "Frames have different dimensions.", "Compare frames from the same screen mode.")
    maximum = 0
    for index in range(0, len(left), 4):
        for channel in range(4):
            delta = abs(left[index + channel] - right[index + channel])
            if delta > maximum:
                maximum = delta
    return maximum


def _diff_images(left: BMPImage, right: BMPImage) -> dict[str, Any]:
    if left.width != right.width or left.height != right.height:
        raise error(
            "dimension_mismatch",
            f"Frame dimensions differ: {left.width}x{left.height} vs {right.width}x{right.height}.",
            "Capture both frames in the same screen mode; resizing is not implemented.",
        )

    total_delta = 0
    max_delta = 0
    changed_pixels = 0
    min_x = left.width
    min_y = left.height
    max_x = -1
    max_y = -1
    diff_rgba = bytearray(left.rgba)

    for index in range(0, len(left.rgba), 4):
        changed = False
        for channel in range(4):
            delta = abs(left.rgba[index + channel] - right.rgba[index + channel])
            if delta:
                changed = True
                total_delta += delta
                if delta > max_delta:
                    max_delta = delta
        if not changed:
            continue
        changed_pixels += 1
        pixel_index = index // 4
        x = pixel_index % left.width
        y = pixel_index // left.width
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        diff_rgba[index] = 255
        diff_rgba[index + 1] = 0
        diff_rgba[index + 2] = 0
        diff_rgba[index + 3] = 255

    descriptor, name = tempfile.mkstemp(prefix="hatari_frame_diff_", suffix=".bmp", dir="/tmp")
    os.close(descriptor)
    diff_path = Path(name)
    try:
        write_bmp24(diff_path, left.width, left.height, bytes(diff_rgba))
    except Exception:
        diff_path.unlink(missing_ok=True)
        raise
    total_channels = left.pixel_count * 4
    bbox = None
    if changed_pixels:
        bbox = {
            "x": min_x,
            "y": min_y,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        }
    return {
        "equal": changed_pixels == 0,
        "width": left.width,
        "height": left.height,
        "total_pixels": left.pixel_count,
        "changed_pixels": changed_pixels,
        "changed_percent": round((changed_pixels / left.pixel_count) * 100.0, 6),
        "mean_channel_delta": round(total_delta / total_channels, 6),
        "max_channel_delta": max_delta,
        "bbox": bbox,
        "sha256_a": left.sha256(),
        "sha256_b": right.sha256(),
        "diff_path": str(diff_path),
        "diff_bytes": diff_path.stat().st_size,
        "diff_sha256": _file_sha256(diff_path),
    }


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="screen_frame_capture",
        description=(
            "Capture a frame with the debugger screenshot command. Prefers BMP so the server can "
            "decode dimensions and pixels with the standard library. PNG capture is returned as a "
            "file without stdlib pixel decoding."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_frame_capture(path: str | None = None, format: str = "bmp") -> dict[str, Any]:
        fmt = format.lower().lstrip(".")
        if fmt not in FRAME_FORMATS:
            raise error("bad_option", f"Unsupported frame format: {format}", "Use bmp or png.")
        if path:
            target = app.guard_path(path)
            if target.suffix:
                fmt = target.suffix.lower().lstrip(".")
                if fmt not in FRAME_FORMATS:
                    raise error(
                        "bad_option",
                        f"Unsupported frame extension: {target.suffix}",
                        "Use a .bmp or .png path.",
                    )
            else:
                target = target.with_suffix("." + fmt)
        else:
            target = _capture_path(app, fmt)
        info = await _capture(app, target, fmt)
        result: dict[str, Any] = {
            "ok": True,
            "path": str(target),
            "format": fmt,
            "bytes": info["size"],
            "sha256": info["sha256"],
            "width": None,
            "height": None,
            "raw": info["result"].get("raw"),
            **_capture_summary(app, target),
        }
        if fmt == "bmp":
            image = read_bmp(target)
            result.update(width=image.width, height=image.height, bpp=image.bpp, top_down=image.top_down)
        else:
            result["note"] = "PNG capture is file-only; use format=bmp for stdlib pixel decoding."
        return result

    @server.tool(
        name="screen_frame_info",
        description="Return dimensions, byte size and SHA-256 for an existing BMP frame, or capture one first.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_frame_info(path: str | None = None) -> dict[str, Any]:
        if path:
            target = app.guard_path(path, must_exist=True)
            image = read_bmp(target)
            size = target.stat().st_size
            digest = _file_sha256(target)
        else:
            target, image = await _capture_bmp(app)
            size = target.stat().st_size
            digest = _file_sha256(target)
        return {
            "ok": True,
            "path": str(target),
            "format": target.suffix.lower().lstrip(".") or "bmp",
            "width": image.width,
            "height": image.height,
            "bpp": image.bpp,
            "top_down": image.top_down,
            "bytes": size,
            "sha256": digest,
            "rgba_sha256": image.sha256(),
            **_capture_summary(app, target),
        }

    @server.tool(
        name="screen_pixel_get",
        description="Capture a BMP frame and return the RGBA value at one screen coordinate.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_pixel_get(x: int, y: int) -> dict[str, Any]:
        x = _as_int(x, "x", 0)
        y = _as_int(y, "y", 0)
        target, image = await _capture_bmp(app)
        red, green, blue, alpha = image.pixel(x, y)
        return {
            "ok": True,
            "x": x,
            "y": y,
            "rgba": [red, green, blue, alpha],
            "rgb": [red, green, blue],
            "hex": f"#{red:02x}{green:02x}{blue:02x}",
            "rgba_hex": f"#{red:02x}{green:02x}{blue:02x}{alpha:02x}",
            "pixel_sha256": hashlib.sha256(bytes((red, green, blue, alpha))).hexdigest(),
            "frame_sha256": image.sha256(),
            "file_sha256": _file_sha256(target),
            "width": image.width,
            "height": image.height,
            "path": str(target),
        }

    @server.tool(
        name="screen_region_hash",
        description=(
            "Capture a BMP frame and hash a rectangular region as canonical RGBA bytes. "
            "Supported algorithms are those exposed by hashlib (for example sha256, sha1 or md5)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_region_hash(
        x: int,
        y: int,
        width: int,
        height: int,
        algorithm: str = "sha256",
    ) -> dict[str, Any]:
        x = _as_int(x, "x", 0)
        y = _as_int(y, "y", 0)
        width = _as_int(width, "width", 1)
        height = _as_int(height, "height", 1)
        algorithm = algorithm.lower().strip()
        try:
            hashlib.new(algorithm)
        except (ValueError, TypeError):
            raise error("bad_option", f"Unknown hash algorithm: {algorithm}", "Use a hashlib algorithm name such as sha256.")
        target, image = await _capture_bmp(app)
        region = image.region_rgba(x, y, width, height)
        digest = hashlib.new(algorithm, region).hexdigest()
        return {
            "ok": True,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "algorithm": algorithm,
            "hash": digest,
            "region_hash": digest,
            "region_sha256": hashlib.sha256(region).hexdigest(),
            "frame_sha256": image.sha256(),
            "pixel_count": width * height,
            "bytes": len(region),
            "path": str(target),
        }

    @server.tool(
        name="screen_frame_diff",
        description=(
            "Compare two BMP frames. Returns changed pixel count, bounding box, mean/max channel "
            "deltas, and a diff BMP written under /tmp."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_frame_diff(path_a: str, path_b: str) -> dict[str, Any]:
        left_path = app.guard_path(path_a, must_exist=True)
        right_path = app.guard_path(path_b, must_exist=True)
        return {
            "ok": True,
            **_diff_images(read_bmp(left_path), read_bmp(right_path)),
            "path_a": str(left_path),
            "path_b": str(right_path),
        }

    @server.tool(
        name="screen_frames_diff",
        description=(
            "Capture a new frame and compare it with a previous frame. Without previous, capture "
            "one frame, wait briefly, capture another, then compare. Diff images are BMP files under /tmp."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_frames_diff(previous: str | None = None) -> dict[str, Any]:
        delay_ms = 200
        if previous:
            previous_path = app.guard_path(previous, must_exist=True)
            previous_image = read_bmp(previous_path)
        else:
            previous_path, previous_image = await _capture_bmp(app)
            await asyncio.sleep(delay_ms / 1000.0)
        current_path, current_image = await _capture_bmp(app)
        return {
            "ok": True,
            **_diff_images(previous_image, current_image),
            "previous": str(previous_path),
            "current": str(current_path),
            "delay_ms": delay_ms if previous is None else 0,
        }

    @server.tool(
        name="screen_wait_stable",
        description=(
            "Poll screenshots until two consecutive frames match or the timeout expires. This is "
            "host-timed polling, not VBL-locked. Matching captures do not prove the guest is idle."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_wait_stable(
        timeout_ms: int = 5000,
        interval_ms: int = 100,
        threshold: int = 0,
    ) -> dict[str, Any]:
        timeout_ms = _as_int(timeout_ms, "timeout_ms", 0, 300000)
        interval_ms = _as_int(interval_ms, "interval_ms", 0, 60000)
        threshold = _as_int(threshold, "threshold", 0, 255)
        started = time.monotonic()
        deadline = started + timeout_ms / 1000.0
        samples: list[dict[str, Any]] = []
        previous_image: BMPImage | None = None
        previous_hash: str | None = None
        stable = False

        while True:
            target, image = await _capture_bmp(app)
            digest = image.sha256()
            sample: dict[str, Any] = {
                "index": len(samples),
                "sha256": digest,
                "width": image.width,
                "height": image.height,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "path": str(target),
            }
            samples.append(sample)
            if previous_image is not None and previous_hash is not None:
                if threshold == 0:
                    matched = digest == previous_hash
                else:
                    matched = _max_channel_delta(previous_image.rgba, image.rgba) <= threshold
                sample["stable_with_previous"] = matched
                if matched:
                    stable = True
                    break
            previous_image = image
            previous_hash = digest
            now = time.monotonic()
            if now >= deadline:
                break
            await asyncio.sleep(min(interval_ms / 1000.0, deadline - now))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "stable": stable,
            "frame_count": len(samples),
            "samples": samples,
            "last_hash": samples[-1]["sha256"],
            "elapsed_ms": elapsed_ms,
            "timeout_ms": timeout_ms,
            "interval_ms": interval_ms,
            "threshold": threshold,
            "timed_out": not stable,
            "vbl_locked": False,
            "note": (
                "Host-timed polling via repeated screenshots; not VBL-locked. Two matching captures "
                "do not prove that the guest has stopped changing the screen."
            ),
        }
