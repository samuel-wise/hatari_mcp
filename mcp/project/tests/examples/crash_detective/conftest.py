"""Shared paths and fixtures for the crash-detective tests."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))


def write_bmp(path: Path, *, width: int = 32, height: int = 24, rgb=(0, 0, 0)) -> Path:
    """Write a tiny uncompressed 24-bit BMP without external dependencies."""
    row_size = ((width * 3 + 3) // 4) * 4
    padding = row_size - width * 3
    rows = []
    red, green, blue = rgb
    for _ in range(height):
        row = bytes((blue, green, red)) * width + b"\0" * padding
        rows.append(row)
    pixel_data = b"".join(rows)
    header = struct.pack(
        "<2sIHHI",
        b"BM",
        54 + len(pixel_data),
        0,
        0,
        54,
    )
    info = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        len(pixel_data),
        2835,
        2835,
        0,
        0,
    )
    path.write_bytes(header + info + pixel_data)
    return path


@pytest.fixture
def bmp_factory(tmp_path: Path):
    def make(name: str = "frame.bmp", *, width: int = 32, height: int = 24, rgb=(0, 0, 0)) -> Path:
        return write_bmp(tmp_path / name, width=width, height=height, rgb=rgb)

    return make
