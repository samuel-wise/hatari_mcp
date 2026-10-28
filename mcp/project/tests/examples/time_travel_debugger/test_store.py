"""Hermetic tests for the on-disk checkpoint timeline store."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from time_travel_debugger.model import Checkpoint, MemorySnapshot, Timeline  # noqa: E402
from time_travel_debugger.store import TimelineStore  # noqa: E402


def _checkpoint(directory: Path, checkpoint_id: str, *, timestamp: float, index: int) -> Checkpoint:
    state = directory / "state.snap"
    state.write_bytes(b"snapshot")
    screenshot = directory / "screen.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    payload = bytes([index, index + 1, index + 2])
    memory_path = directory / "mem.bin"
    memory_path.write_bytes(payload)
    return Checkpoint(
        id=checkpoint_id,
        timestamp=timestamp,
        index=index,
        vbl=index * 10,
        state_path=str(state),
        screenshot_path=str(screenshot),
        frame_sha256=hashlib.sha256(payload).hexdigest(),
        registers={"pc": f"0x{index:x}", "d0": "0x0"},
        memory=[
            MemorySnapshot(
                address=0x70000,
                length=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                path=str(memory_path),
            )
        ],
    )


def test_store_roundtrip_and_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "session"
    store = TimelineStore(root)
    timeline = Timeline()
    first_dir = store.checkpoint_dir("cp-000002")
    second_dir = store.checkpoint_dir("cp-000001")
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    newest = _checkpoint(first_dir, "cp-000002", timestamp=20.0, index=2)
    oldest = _checkpoint(second_dir, "cp-000001", timestamp=10.0, index=1)
    timeline.add(newest)
    timeline.add(oldest)
    store.save_checkpoint(newest)
    store.save_checkpoint(oldest)
    store.save_timeline(timeline, session_id="unit")

    payload = json.loads(store.timeline_path.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["session_id"] == "unit"
    assert [item["id"] for item in payload["checkpoints"]] == ["cp-000001", "cp-000002"]
    assert payload["checkpoints"][0]["state_path"] == "checkpoints/cp-000001/state.snap"
    assert "base64" not in payload["checkpoints"][0]["memory"][0] or payload["checkpoints"][0]["memory"][0]["base64"] is None

    loaded = store.load_timeline()
    assert store.load_error is None
    assert [item.id for item in loaded.ordered()] == ["cp-000001", "cp-000002"]
    restored = loaded.get("cp-000001")
    assert restored is not None
    assert restored.state_path == str(second_dir / "state.snap")
    assert restored.memory[0].bytes() == bytes([1, 2, 3])

    sidecar = store.load_checkpoint("cp-000002")
    assert sidecar is not None
    assert sidecar.id == "cp-000002"
    assert sidecar.memory[0].bytes() == bytes([2, 3, 4])


def test_store_handles_corrupt_timeline_and_deletion(tmp_path: Path) -> None:
    root = tmp_path / "session"
    store = TimelineStore(root)
    store.timeline_path.write_text("{broken", encoding="utf-8")
    timeline = store.load_timeline()
    assert len(timeline) == 0
    assert store.load_error is not None and "JSONDecodeError" in store.load_error

    directory = store.checkpoint_dir("cp-000009")
    directory.mkdir(parents=True)
    checkpoint = _checkpoint(directory, "cp-000009", timestamp=1.0, index=9)
    store.save_checkpoint(checkpoint)
    assert directory.is_dir()
    assert store.delete_checkpoint(checkpoint)
    assert not directory.exists()
    assert not store.delete_checkpoint(checkpoint)
