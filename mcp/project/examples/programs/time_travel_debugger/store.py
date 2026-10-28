"""On-disk timeline store for the time-travel debugger.

Layout under the app state directory::

    <root>/
      timeline.json
      checkpoints/<id>/checkpoint.json
      checkpoints/<id>/state.snap
      checkpoints/<id>/screen.png
      checkpoints/<id>/mem-*.bin

Paths inside JSON are stored relative to ``<root>`` so a session can be moved
or inspected without leaking host-specific absolute paths.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

try:  # package import when examples/programs is on sys.path
    from common.config import atomic_write_json
except ImportError:  # direct script import
    from config import atomic_write_json  # type: ignore[no-redef]

from .model import Checkpoint, ModelError, Timeline

TIMELINE_SCHEMA = 1


class TimelineStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir = self.root / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.root / "timeline.json"
        self.load_error: str | None = None

    # -- paths ---------------------------------------------------------

    def checkpoint_dir(self, checkpoint_id: str) -> Path:
        if not checkpoint_id or "/" in checkpoint_id or "\\" in checkpoint_id:
            raise ModelError(f"invalid checkpoint id: {checkpoint_id!r}")
        if checkpoint_id in {".", ".."}:
            raise ModelError(f"invalid checkpoint id: {checkpoint_id!r}")
        return self.checkpoints_dir / checkpoint_id

    # -- timeline ------------------------------------------------------

    def load_timeline(self) -> Timeline:
        self.load_error = None
        if not self.timeline_path.is_file():
            return Timeline()
        try:
            payload = json.loads(self.timeline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            return Timeline()
        if not isinstance(payload, dict):
            self.load_error = "timeline.json is not an object"
            return Timeline()
        entries = payload.get("checkpoints")
        return Timeline.from_list(entries, base_dir=self.root)

    def save_timeline(self, timeline: Timeline, *, session_id: str | None = None) -> Path:
        payload = {
            "schema": TIMELINE_SCHEMA,
            "session_id": session_id,
            "checkpoints": [
                checkpoint.to_dict(base_dir=self.root) for checkpoint in timeline.ordered()
            ],
        }
        return atomic_write_json(self.timeline_path, payload)

    # -- checkpoints ---------------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint) -> Path:
        directory = self.checkpoint_dir(checkpoint.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "checkpoint.json"
        atomic_write_json(
            target, checkpoint.to_dict(base_dir=self.root), mode=0o644
        )
        return target

    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        target = self.checkpoint_dir(checkpoint_id) / "checkpoint.json"
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return Checkpoint.from_dict(payload, base_dir=self.root)

    def checkpoint_files(self, checkpoint: Checkpoint) -> dict[str, str | None]:
        return {
            "directory": str(self.checkpoint_dir(checkpoint.id)),
            "state": checkpoint.state_path,
            "screenshot": checkpoint.screenshot_path,
            "memory": [item.path for item in checkpoint.memory],
        }

    def delete_checkpoint(self, checkpoint: Checkpoint | str) -> bool:
        checkpoint_id = checkpoint.id if isinstance(checkpoint, Checkpoint) else str(checkpoint)
        directory = self.checkpoint_dir(checkpoint_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return not directory.exists()

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "timeline": str(self.timeline_path),
            "load_error": self.load_error,
        }
