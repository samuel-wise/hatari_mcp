from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from hatari_mcp.services.scenario import (
    ArtifactBundle,
    determinism_args,
    determinism_report,
    replay_bundle,
)
from hatari_mcp.tools import frames


def bmp_bytes() -> bytes:
    target = Path("/tmp") / "determinism-test.bmp"
    frames.write_bmp24(target, 3, 1, bytes([1, 2, 3, 255, 4, 5, 6, 255, 7, 8, 9, 255]))
    data = target.read_bytes()
    target.unlink(missing_ok=True)
    return data


class ReplayBackend:
    def __init__(self) -> None:
        self.bmp = bmp_bytes()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.state = "stopped"
        self.closed = False
        self.shot_dir: Path | None = None
        self.shot_count = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        self.calls.append((name, args))
        if name == "emu_start":
            self.state = "running"
            args_list = list(args.get("args") or [])
            for index, token in enumerate(args_list):
                if token == "--screenshot-dir" and index + 1 < len(args_list):
                    self.shot_dir = Path(args_list[index + 1])
            return {"ok": True, "state": "running"}
        if name == "emu_shortcut" and args.get("name") == "screenshot" and self.shot_dir:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            self.shot_count += 1
            (self.shot_dir / f"grab{self.shot_count:04d}.bmp").write_bytes(self.bmp)
            return {"ok": True, "shortcut": "screenshot"}
        if name == "emu_status":
            return {"ok": True, "state": self.state}
        if name == "emu_pause":
            self.state = "remote_paused"
            return {"ok": True, "state": self.state}
        if name == "emu_resume":
            self.state = "running"
            return {"ok": True, "state": self.state}
        if name == "emu_stop":
            self.state = "exited"
            return {"ok": True, "state": "exited"}
        if name == "dbg_command":
            target = Path(args["command"].split(" ", 1)[1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.bmp)
            return {"ok": True, "raw": ""}
        return {"ok": True}


def test_determinism_profiles_and_decoded_rgba_hash():
    strict = determinism_args("strict")
    assert "--cpu-exact" in strict
    assert "--fastfdc" in strict
    assert determinism_args("off") == []
    report = determinism_report("strict")
    assert report["vbl_locked"] is False
    assert report["fidelity"] == "approximate-frame"
    assert report["residual_nondeterminism"]

    image = frames.decode_bmp(bmp_bytes())
    assert image.width == 3
    assert image.sha256() == frames.decode_bmp(bmp_bytes()).sha256()


def make_replay_bundle(tmp_path: Path, expected: str | None) -> Path:
    bundle = ArtifactBundle.create(tmp_path / "artifacts", "run", "case")
    bundle.write_json(
        "replay/manifest.json",
        {
            "schema": "hatari-mcp.replay/1",
            "version": 1,
            "scenario": "demo",
            "fidelity": "approximate",
            "frame_locked": False,
            "start": {"profile": "st"},
        },
    )
    bundle.write_text(
        "replay/events.jsonl",
        json.dumps({"tool": "input_text", "args": {"text": "a"}}) + "\n",
    )
    if expected is not None:
        bundle.write_text(
            "replay/checkpoints.jsonl",
            json.dumps(
                {
                    "kind": "screen_rgba_sha256",
                    "value": expected,
                    "step_id": "capture",
                }
            )
            + "\n",
        )
    return bundle.finalize("passed", {"scenario": "demo"})


def test_replay_matches_checkpoint_and_labels_approximate(tmp_path):
    expected = frames.decode_bmp(bmp_bytes()).sha256()
    bundle = make_replay_bundle(tmp_path, expected)
    backend = ReplayBackend()

    result = asyncio.run(replay_bundle(bundle, backend_factory=lambda: backend))

    assert result["ok"] is True
    assert result["fidelity"] == "approximate"
    assert result["frame_locked"] is False
    assert result["expected"] == expected
    assert result["actual"] == expected
    assert backend.closed is True
    assert ("input_text", {"text": "a"}) in backend.calls


def test_replay_reports_first_divergence(tmp_path):
    bundle = make_replay_bundle(tmp_path, "0" * 64)
    backend = ReplayBackend()

    result = asyncio.run(replay_bundle(bundle, backend_factory=lambda: backend))

    assert result["ok"] is False
    assert result["first_divergence"]["kind"] == "screen_rgba_sha256"
    assert result["first_divergence"]["expected"] == "0" * 64
