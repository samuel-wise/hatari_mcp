"""Hermetic tests for checkpoint, timeline and divergence logic."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from time_travel_debugger.model import (  # noqa: E402
    Checkpoint,
    MemoryRegion,
    MemorySnapshot,
    Timeline,
    canonical_register,
    diff_checkpoints,
    memory_diff_ranges,
    normalize_registers,
    parse_memory_regions,
)


def _memory(address: int, data: bytes) -> MemorySnapshot:
    return MemorySnapshot(
        address=address,
        length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _checkpoint(
    checkpoint_id: str,
    *,
    pc: str = "0x1000",
    registers: dict[str, str] | None = None,
    memory: list[MemorySnapshot] | None = None,
    frame: str = "f" * 64,
    vbl: int = 10,
    timestamp: float = 1000.0,
    index: int = 1,
    source: str = "capture",
) -> Checkpoint:
    merged = {"pc": pc, **(registers or {})}
    return Checkpoint(
        id=checkpoint_id,
        timestamp=timestamp,
        index=index,
        vbl=vbl,
        source=source,
        state_path=f"/tmp/{checkpoint_id}/state.snap",
        screenshot_path=f"/tmp/{checkpoint_id}/screen.png",
        frame_sha256=frame,
        registers=merged,
        memory=memory or [],
    )


def test_memory_region_parsing_and_validation() -> None:
    assert MemoryRegion.parse("0x70000:64") == MemoryRegion(0x70000, 64)
    assert MemoryRegion.parse("$70000:$40") == MemoryRegion(0x70000, 64)
    assert parse_memory_regions("0x10000:16, 0x20000:32") == [
        MemoryRegion(0x10000, 16),
        MemoryRegion(0x20000, 32),
    ]
    with pytest.raises(ValueError):
        MemoryRegion.parse("nonsense")
    with pytest.raises(ValueError):
        MemoryRegion(0x1000, 0)
    with pytest.raises(ValueError):
        MemoryRegion(0x1000, 65537)


def test_register_normalization() -> None:
    assert canonical_register("$00000010") == "0x10"
    assert canonical_register(16) == "0x10"
    assert canonical_register("16") == "0x10"
    payload = {
        "raw": "ignored",
        "registers": {"d0": "$00000001", "pc": "0x00fc65fc", "sr": "2700"},
    }
    assert normalize_registers(payload) == {
        "d0": "0x1",
        "pc": "0xfc65fc",
        "sr": "0xa8c",
    }


def test_checkpoint_serialization_roundtrip(tmp_path: Path) -> None:
    payload = b"\x01\x02\x03\x04"
    checkpoint = _checkpoint(
        "cp-000001",
        registers={"d0": "0x1", "a7": "0x2000"},
        memory=[_memory(0x70000, payload)],
        timestamp=1234.5,
        vbl=42,
    )
    data = checkpoint.to_dict()
    assert data["schema"] == 1
    assert data["memory"][0]["base64"] is not None
    restored = Checkpoint.from_dict(json.loads(json.dumps(data)))
    assert restored.id == checkpoint.id
    assert restored.registers == checkpoint.registers
    assert restored.memory[0].bytes() == payload
    assert restored.vbl == 42
    assert restored.timestamp == pytest.approx(1234.5)

    binary = tmp_path / "mem.bin"
    binary.write_bytes(payload)
    on_disk = _checkpoint("cp-000002", memory=[])
    on_disk.state_path = str(tmp_path / "state.snap")
    on_disk.screenshot_path = str(tmp_path / "screen.png")
    on_disk.memory = [
        MemorySnapshot(0x70000, len(payload), hashlib.sha256(payload).hexdigest(), str(binary))
    ]
    relative = on_disk.to_dict(base_dir=tmp_path)
    assert relative["state_path"].startswith("/tmp/") is False
    assert relative["memory"][0]["path"] == "mem.bin"
    roundtrip = Checkpoint.from_dict(relative, base_dir=tmp_path)
    assert roundtrip.memory[0].path == str(binary)
    assert roundtrip.memory[0].bytes() == payload


def test_timeline_ordering_lookup_and_filtering() -> None:
    timeline = Timeline(
        [
            _checkpoint("cp-000003", timestamp=30.0, index=3, vbl=30),
            _checkpoint("cp-000001", timestamp=10.0, index=1, vbl=10),
            _checkpoint("cp-000002", timestamp=20.0, index=2, vbl=20),
            _checkpoint(
                "cp-000004",
                timestamp=40.0,
                index=4,
                vbl=40,
                source="replay",
            ),
        ]
    )
    assert [item.id for item in timeline.ordered()] == [
        "cp-000001",
        "cp-000002",
        "cp-000003",
        "cp-000004",
    ]
    assert [item.id for item in timeline.recorded()] == [
        "cp-000001",
        "cp-000002",
        "cp-000003",
    ]
    assert [item.id for item in timeline.replays()] == ["cp-000004"]
    assert timeline.latest.id == "cp-000004"
    assert timeline.nearest_before_vbl(25).id == "cp-000002"
    assert timeline.next_index() == 5
    assert [item.id for item in timeline.between("cp-000001", "cp-000003")] == [
        "cp-000002",
        "cp-000003",
    ]
    assert [item.id for item in timeline.between("cp-000001", "cp-000003", include_start=True)] == [
        "cp-000001",
        "cp-000002",
        "cp-000003",
    ]
    with pytest.raises(ValueError):
        timeline.between("cp-000003", "cp-000001")


def test_timeline_pruning_and_protection() -> None:
    timeline = Timeline(
        [
            _checkpoint(f"cp-{index:06d}", timestamp=float(index), index=index)
            for index in range(1, 6)
        ]
    )
    removed = timeline.prune(3)
    assert [item.id for item in removed] == ["cp-000001", "cp-000002"]
    assert [item.id for item in timeline.ordered()] == [
        "cp-000003",
        "cp-000004",
        "cp-000005",
    ]

    timeline = Timeline(
        [
            _checkpoint(f"cp-{index:06d}", timestamp=float(index), index=index)
            for index in range(1, 6)
        ]
    )
    removed = timeline.prune(3, protected={"cp-000001"})
    assert [item.id for item in removed] == ["cp-000002", "cp-000003"]
    assert "cp-000001" in timeline
    assert len(timeline) == 3

    with pytest.raises(ValueError):
        timeline.prune(0)


def test_diff_precedence_and_first_divergence() -> None:
    before = _checkpoint(
        "cp-000001",
        pc="0x1000",
        registers={"d0": "0x1", "d1": "0x2"},
        memory=[_memory(0x70000, b"\x00\x01\x02\x03")],
        frame="a" * 64,
    )
    identical = _checkpoint(
        "cp-000002",
        pc="0x1000",
        registers={"d0": "0x1", "d1": "0x2"},
        memory=[_memory(0x70000, b"\x00\x01\x02\x03")],
        frame="a" * 64,
    )
    report = diff_checkpoints(before, identical)
    assert report.match
    assert report.first is None

    register_diff = _checkpoint(
        "cp-000003", registers={"d0": "0x9", "d1": "0x2"}, memory=[_memory(0x70000, b"\x00\x01\x02\x03")]
    )
    report = diff_checkpoints(before, register_diff)
    assert report.first is not None
    assert report.first.category == "register"
    assert report.first.key == "d0"

    pc_diff = _checkpoint(
        "cp-000004",
        pc="0x2000",
        registers={"d0": "0x9", "d1": "0x2"},
        memory=[_memory(0x70000, b"\x00\x01\xff\x03")],
        frame="b" * 64,
    )
    report = diff_checkpoints(before, pc_diff)
    assert [item.category for item in report.divergences[:2]] == ["pc", "register"]
    assert report.first.category == "pc"

    memory_diff = _checkpoint(
        "cp-000005",
        registers={"d0": "0x1", "d1": "0x2"},
        memory=[_memory(0x70000, b"\x00\x09\x02\x03")],
    )
    report = diff_checkpoints(before, memory_diff)
    assert report.first is not None
    assert report.first.category == "memory"
    assert report.first.offset == 1
    assert "0x70001" in report.first.detail

    frame_diff = _checkpoint(
        "cp-000006",
        registers={"d0": "0x1", "d1": "0x2"},
        memory=[_memory(0x70000, b"\x00\x01\x02\x03")],
        frame="b" * 64,
    )
    report = diff_checkpoints(before, frame_diff)
    assert report.first is not None
    assert report.first.category == "framebuffer"


def test_memory_diff_ranges() -> None:
    before = _checkpoint(
        "cp-000001",
        memory=[_memory(0x70000, b"\x00\x01\x02\x03\x04\x05\x06\x07")],
    )
    after = _checkpoint(
        "cp-000002",
        memory=[_memory(0x70000, b"\x00\x01\xff\x03\x04\xfe\xff\x07")],
    )
    ranges = memory_diff_ranges(before, after)
    assert [item.as_dict() for item in ranges] == [
        {"address": 0x70002, "length": 1, "before_hex": "02", "after_hex": "ff"},
        {"address": 0x70005, "length": 2, "before_hex": "0506", "after_hex": "feff"},
    ]
    assert memory_diff_ranges(before, after, address=0x80000) == []
