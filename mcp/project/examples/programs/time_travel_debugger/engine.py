"""Checkpoint capture and approximate replay on top of the Hatari MCP server."""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # package import
    from common import paths
    from common.debug_session import DebugSession
    from common.mcp_client import MCPClient
except ImportError as exc:  # pragma: no cover - import error is user-facing
    raise ImportError(
        "Add mcp/project/examples/programs to sys.path so the shared common "
        "package can be imported"
    ) from exc

from .model import (
    Checkpoint,
    DiffReport,
    Divergence,
    MemoryRegion,
    MemorySnapshot,
    diff_checkpoints,
    format_memory_regions,
    normalize_registers,
    parse_int,
    parse_memory_regions,
)
from .store import TimelineStore

REPLAY_NOTE = (
    "Approximate replay: stock Hatari has no frame-locked input replay (P7.5). "
    "Checkpoints are host-timed snapshot comparisons, not deterministic "
    "input-exact rewind."
)

DEFAULT_MEMORY = "0x70000:64"
SAMPLE_MEMORY = "0x70000:64"


class EngineError(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ReplayCheck:
    reference: Checkpoint
    replay: Checkpoint
    report: DiffReport

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference.id,
            "replay_id": self.replay.id,
            "match": self.report.match,
            "first": self.report.first.as_dict() if self.report.first else None,
            "divergences": [item.as_dict() for item in self.report.divergences],
        }


@dataclass
class ReplayResult:
    start_id: str
    target_id: str | None
    mode: str
    checks: list[ReplayCheck] = field(default_factory=list)
    captures: list[Checkpoint] = field(default_factory=list)
    steps: int = 0
    note: str = REPLAY_NOTE
    approximate: bool = True

    @property
    def matched(self) -> bool:
        return bool(self.checks) and all(item.report.match for item in self.checks)

    @property
    def first_divergence(self) -> Divergence | None:
        for item in self.checks:
            if item.report.first is not None:
                return item.report.first
        return None

    @property
    def first_failed_check(self) -> ReplayCheck | None:
        return next((item for item in self.checks if not item.report.match), None)

    def summary(self) -> str:
        if not self.checks:
            return "replay produced no comparison checkpoint"
        if self.matched:
            return (
                f"approximate replay matched {len(self.checks)} checkpoint(s) "
                "in PC, registers, memory and framebuffer"
            )
        first = self.first_divergence
        assert first is not None
        return f"approximate replay diverged: {first.describe()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_id": self.start_id,
            "target_id": self.target_id,
            "mode": self.mode,
            "steps": self.steps,
            "approximate": self.approximate,
            "note": self.note,
            "matched": self.matched,
            "summary": self.summary(),
            "first_divergence": self.first_divergence.as_dict() if self.first_divergence else None,
            "captures": [item.id for item in self.captures],
            "checks": [item.as_dict() for item in self.checks],
        }


class CheckpointEngine:
    """Capture, load and compare time-travel checkpoints."""

    def __init__(
        self,
        client: MCPClient,
        *,
        state_base: str | Path | None = None,
        session_id: str | None = None,
        machine: str = "st",
        tos: str | Path | None = None,
        memory_regions: str | Iterable[MemoryRegion] | None = None,
        retention: int = 24,
    ):
        self.client = client
        self.machine = machine
        base = Path(state_base).expanduser() if state_base else paths.state_dir()
        self.session_id = session_id or time.strftime("session-%Y%m%d-%H%M%S", time.localtime())
        self.root = (base / "time_travel_debugger" / self.session_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = TimelineStore(self.root)
        self.timeline = self.store.load_timeline()
        self.session = DebugSession(
            client,
            state_dir=self.root / "emulator",
            machine=machine,
            tos=tos,
        )
        self.regions = (
            parse_memory_regions(memory_regions)
            if memory_regions is not None
            else parse_memory_regions(DEFAULT_MEMORY)
        )
        self.retention = max(0, int(retention))
        self.selected_id: str | None = None
        self.loaded_id: str | None = None
        self.last_error: str | None = None

    # -- lifecycle -----------------------------------------------------

    def start_emutos(self, *, timeout: float = 60.0, wait: bool = True) -> dict[str, Any]:
        return self.session.start_emutos(machine=self.machine, wait=wait, timeout=timeout)

    def load_prg(self, prg: str | Path, *, timeout: float = 60.0, wait: bool = True) -> dict[str, Any]:
        return self.session.load_prg(prg, machine=self.machine, wait=wait, timeout=timeout)

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        return self.session.stop(force=force)

    def status(self) -> dict[str, Any]:
        return self.session.status()

    # -- configuration -------------------------------------------------

    def set_memory_regions(self, spec: str | Iterable[MemoryRegion]) -> list[MemoryRegion]:
        regions = parse_memory_regions(spec)
        if not regions:
            raise EngineError("at least one memory region is required")
        self.regions = regions
        return regions

    @property
    def memory_spec(self) -> str:
        return format_memory_regions(self.regions)

    def set_retention(self, value: int) -> int:
        self.retention = max(0, int(value))
        return self.retention

    # -- guest clocks --------------------------------------------------

    def read_vbl(self) -> int | None:
        try:
            result = self.client.call_tool(
                "dbg_evaluate", expression="($462).l", timeout=5.0
            )
            value = parse_int(result.get("value"))
            if value is not None:
                return value
        except BaseException:
            pass
        try:
            raw = self.session.memory_read(0x462, 4).get("hex", "")
            data = bytes.fromhex(raw)
            if len(data) == 4:
                return int.from_bytes(data, "big")
        except BaseException:
            pass
        return None

    def wait_until_vbl(self, target: int, *, timeout: float = 60.0) -> int | None:
        deadline = time.monotonic() + max(0.5, timeout)
        last: int | None = None
        while time.monotonic() < deadline:
            last = self.read_vbl()
            if last is not None and last >= target:
                return last
            time.sleep(0.02)
        raise EngineError(f"VBL {target} not reached within {timeout:.1f}s (last={last})")

    def wait_vbls(self, count: int) -> int | None:
        start = self.read_vbl()
        if start is None:
            time.sleep(max(0.05, count / 50.0))
            return None
        return self.wait_until_vbl(start + max(1, int(count)))

    # -- capture -------------------------------------------------------

    def capture_checkpoint(
        self,
        *,
        resume: bool = True,
        source: str = "capture",
        reference_id: str | None = None,
        label: str = "",
        wait_vbls: int | None = None,
        apply_retention: bool = True,
        protected: Iterable[str] = (),
    ) -> Checkpoint:
        if wait_vbls:
            if self.session.at_prompt:
                self.session.continue_run()
            self.wait_vbls(wait_vbls)
        try:
            checkpoint = self._capture_current(
                source=source,
                reference_id=reference_id,
                label=label,
            )
        finally:
            if resume and self.session.at_prompt:
                try:
                    self.session.continue_run()
                except BaseException:
                    pass

        self.timeline.add(checkpoint)
        self.selected_id = checkpoint.id
        if apply_retention:
            protected_ids = {checkpoint.id, *protected}
            if self.loaded_id:
                protected_ids.add(self.loaded_id)
            self._prune(protected=protected_ids)
        else:
            self.store.save_timeline(self.timeline, session_id=self.session_id)
        return checkpoint

    def capture_after_vbls(self, count: int, **kwargs: Any) -> Checkpoint:
        return self.capture_checkpoint(wait_vbls=count, **kwargs)

    def _capture_current(
        self, *, source: str, reference_id: str | None, label: str
    ) -> Checkpoint:
        started = time.monotonic()
        index = self.timeline.next_index()
        checkpoint_id = f"cp-{index:06d}"
        directory = self.store.checkpoint_dir(checkpoint_id)
        directory.mkdir(parents=True, exist_ok=True)

        state_path = directory / "state.snap"
        screenshot_path = directory / "screen.png"

        save_started = time.monotonic()
        saved = self.client.call_tool(
            "dbg_state_save", path=str(state_path), timeout=40.0
        )
        save_ms = (time.monotonic() - save_started) * 1000.0
        if not state_path.is_file():
            raise EngineError(f"state save produced no file: {state_path}")

        capture = self.client.call_tool(
            "screen_frame_capture",
            path=str(screenshot_path),
            format="png",
            timeout=30.0,
        )
        if not screenshot_path.is_file():
            raise EngineError(f"screenshot capture produced no file: {screenshot_path}")
        frame_sha = str(capture.get("sha256") or _file_sha256(screenshot_path))

        register_payload = self.session.registers()
        registers = normalize_registers(register_payload)

        memory: list[MemorySnapshot] = []
        for offset, region in enumerate(self.regions):
            result = self.session.memory_read(region.address, region.length)
            try:
                data = bytes.fromhex(str(result.get("hex", "")))
            except ValueError as exc:
                raise EngineError(
                    f"invalid memory payload for {region.label}: {exc}"
                ) from exc
            if len(data) != region.length:
                raise EngineError(
                    f"memory read for {region.label} returned {len(data)} bytes, "
                    f"expected {region.length}"
                )
            binary_path = directory / f"mem-{offset}-0x{region.address:x}.bin"
            binary_path.write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            memory.append(
                MemorySnapshot(
                    address=region.address,
                    length=region.length,
                    sha256=sha,
                    path=str(binary_path),
                    data=data,
                )
            )

        vbl = self.read_vbl()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        checkpoint = Checkpoint(
            id=checkpoint_id,
            timestamp=time.time(),
            index=index,
            vbl=vbl,
            label=label,
            source=source,
            reference_id=reference_id,
            state_path=str(state_path),
            screenshot_path=str(screenshot_path),
            frame_sha256=frame_sha,
            registers=registers,
            memory=memory,
            metadata={
                "capture_ms": round(elapsed_ms, 3),
                "state_save_ms": round(save_ms, 3),
                "state_bytes": state_path.stat().st_size,
                "screenshot_bytes": screenshot_path.stat().st_size,
                "screenshot_width": capture.get("width"),
                "screenshot_height": capture.get("height"),
                "memory_regions": [region.to_dict() for region in self.regions],
                "saved": {
                    key: value
                    for key, value in (saved or {}).items()
                    if key in {"ok", "path", "raw", "channel"}
                },
                "register_raw": register_payload.get("raw"),
                "approximate": True,
            },
        )
        self.store.save_checkpoint(checkpoint)
        return checkpoint

    def _prune(self, *, protected: Iterable[str] = ()) -> list[Checkpoint]:
        if self.retention <= 0:
            self.store.save_timeline(self.timeline, session_id=self.session_id)
            return []
        protected_ids = {str(item) for item in protected}
        removed = self.timeline.prune(self.retention, protected=protected_ids)
        for checkpoint in removed:
            self.store.delete_checkpoint(checkpoint)
            if self.selected_id == checkpoint.id:
                self.selected_id = None
            if self.loaded_id == checkpoint.id:
                self.loaded_id = None
        self.store.save_timeline(self.timeline, session_id=self.session_id)
        return removed

    def prune(self) -> list[Checkpoint]:
        return self._prune(protected={item for item in (self.selected_id, self.loaded_id) if item})

    # -- load and inspect ---------------------------------------------

    def resolve(self, checkpoint: Checkpoint | str | None) -> Checkpoint:
        if isinstance(checkpoint, Checkpoint):
            return checkpoint
        item = self.timeline.get(checkpoint)
        if item is None:
            raise EngineError(f"unknown checkpoint: {checkpoint}")
        return item

    def load_checkpoint(
        self, checkpoint: Checkpoint | str, *, stop: bool = False
    ) -> Checkpoint:
        item = self.resolve(checkpoint)
        if not Path(item.state_path).is_file():
            raise EngineError(f"checkpoint state file is missing: {item.state_path}")
        if stop:
            self.ensure_prompt()
        # stateload works both while running (over the control socket) and at
        # the debugger prompt (over stdin).  Do not force a break-in for a
        # plain rewind: stock Hatari's repeated break-in path is unreliable.
        self.session.load_snapshot(item.state_path)
        self.loaded_id = item.id
        self.selected_id = item.id
        return item

    def ensure_prompt(self) -> None:
        if self.session.at_prompt:
            return
        try:
            self.session.ensure_prompt(timeout=15.0)
        except BaseException as exc:
            raise EngineError(
                "could not enter the debugger prompt. Stock Hatari's repeated "
                "break-in path can become unresponsive; load a checkpoint or "
                "restart EmuTOS, then retry"
            ) from exc

    def _disassemble(self, address: int | str, count: int = 16) -> dict[str, Any]:
        """Disassemble via Hatari at the prompt, or via m68k objdump while running."""
        if self.session.at_prompt:
            try:
                return self.session.disassemble(address, count=count)
            except BaseException:
                pass
        try:
            start = parse_int(address)
            if start is None:
                raise ValueError(f"non-numeric address: {address}")
            length = max(1, int(count)) * 8
            payload = bytes.fromhex(self.session.memory_read(start, length).get("hex", ""))
            if not payload:
                raise ValueError("no memory bytes returned")
            fd, path = tempfile.mkstemp(prefix="tt-disasm-", suffix=".bin")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                for tool in ("m68k-linux-gnu-objdump", "m68k-atari-mint-objdump", "objdump"):
                    try:
                        result = subprocess.run(
                            [
                                tool,
                                "-b",
                                "binary",
                                "-m",
                                "m68k:68000",
                                "--adjust-vma",
                                f"0x{start:x}",
                                "-D",
                                "--no-show-raw-insn",
                                path,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        continue
                    if result.returncode == 0:
                        lines = [
                            line.rstrip()
                            for line in result.stdout.splitlines()
                            if line.strip() and not line.rstrip().endswith(".data>:")
                        ]
                        return {
                            "ok": True,
                            "raw": "\n".join(lines),
                            "lines": lines,
                            "source": "objdump",
                            "address": address,
                            "count": count,
                        }
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        except BaseException as exc:
            return {
                "ok": False,
                "error": f"hatari disassembly failed; objdump fallback failed: {exc}",
            }
        return {
            "ok": False,
            "error": "hatari disassembly failed and no m68k objdump is available",
        }

    def inspect_checkpoint(self, checkpoint: Checkpoint | str) -> dict[str, Any]:
        item = self.load_checkpoint(checkpoint)
        registers = normalize_registers(self.session.registers())
        pc = registers.get("pc", "0x0")
        disassembly = self._disassemble(pc, count=16)
        memory: list[dict[str, Any]] = []
        for region in self.regions:
            try:
                result = self.session.memory_read(region.address, region.length)
                raw = bytes.fromhex(str(result.get("hex", "")))
                memory.append(
                    {
                        "address": region.address,
                        "length": region.length,
                        "hex": raw.hex(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            except BaseException as exc:
                memory.append(
                    {
                        "address": region.address,
                        "length": region.length,
                        "error": str(exc),
                    }
                )
        return {
            "checkpoint": item,
            "registers": registers,
            "disassembly": disassembly,
            "memory": memory,
            "screenshot_path": item.screenshot_path,
            "status": self.status(),
        }

    def inspect_live(self) -> dict[str, Any]:
        """Read the current debugger context without loading a checkpoint."""
        registers = normalize_registers(self.session.registers())
        pc = registers.get("pc", "0x0")
        disassembly = self._disassemble(pc, count=16)
        memory: list[dict[str, Any]] = []
        for region in self.regions:
            try:
                result = self.session.memory_read(region.address, region.length)
                raw = bytes.fromhex(str(result.get("hex", "")))
                memory.append(
                    {
                        "address": region.address,
                        "length": region.length,
                        "hex": raw.hex(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            except BaseException as exc:
                memory.append(
                    {
                        "address": region.address,
                        "length": region.length,
                        "error": str(exc),
                    }
                )
        return {
            "checkpoint": None,
            "registers": registers,
            "disassembly": disassembly,
            "memory": memory,
            "screenshot_path": None,
            "status": self.status(),
        }

    def step(self, count: int = 1) -> dict[str, Any]:
        self.ensure_prompt()
        return self.session.step(count=count)

    def continue_run(self) -> dict[str, Any]:
        return self.session.continue_run()

    # -- replay --------------------------------------------------------

    def replay_sequence(self, start_id: str, target_id: str) -> ReplayResult:
        start = self.resolve(start_id)
        target = self.resolve(target_id)
        sequence = self.timeline.between(start.id, target.id, include_start=False)
        if not sequence:
            # Start and target are the same checkpoint: compare the loaded
            # checkpoint with itself so callers get a truthful matched result.
            self.load_checkpoint(start)
            replay = self.capture_checkpoint(
                resume=False,
                source="replay",
                reference_id=start.id,
                label=f"replay of {start.id}",
                apply_retention=False,
            )
            report = diff_checkpoints(start, replay)
            result = ReplayResult(
                start_id=start.id,
                target_id=target.id,
                mode="sequence",
                checks=[ReplayCheck(start, replay, report)],
                captures=[replay],
            )
            self._prune(protected={start.id, target.id, replay.id})
            return result

        self.load_checkpoint(start)
        result = ReplayResult(start_id=start.id, target_id=target.id, mode="sequence")
        replay_ids: list[str] = []
        for reference in sequence:
            self._advance_to(reference)
            replay = self.capture_checkpoint(
                resume=False,
                source="replay",
                reference_id=reference.id,
                label=f"replay of {reference.id}",
                apply_retention=False,
            )
            replay_ids.append(replay.id)
            result.captures.append(replay)
            report = diff_checkpoints(reference, replay)
            result.checks.append(ReplayCheck(reference, replay, report))
            if not report.match:
                break
        self._prune(protected={start.id, target.id, *replay_ids})
        return result

    def replay_to_target(self, start_id: str, target_id: str) -> ReplayResult:
        start = self.resolve(start_id)
        target = self.resolve(target_id)
        self.load_checkpoint(start)
        self._advance_to(target)
        replay = self.capture_checkpoint(
            resume=False,
            source="replay",
            reference_id=target.id,
            label=f"replay of {target.id}",
            apply_retention=False,
        )
        report = diff_checkpoints(target, replay)
        result = ReplayResult(
            start_id=start.id,
            target_id=target.id,
            mode="continue",
            checks=[ReplayCheck(target, replay, report)],
            captures=[replay],
        )
        self._prune(protected={start.id, target.id, replay.id})
        return result

    def replay_steps(
        self, start_id: str, target_id: str | None, *, steps: int = 1
    ) -> ReplayResult:
        start = self.resolve(start_id)
        target = self.resolve(target_id) if target_id else None
        self.load_checkpoint(start, stop=True)
        step_count = max(1, int(steps))
        self.session.step(step_count)
        replay = self.capture_checkpoint(
            resume=False,
            source="replay",
            reference_id=target.id if target else None,
            label=f"step replay from {start.id}",
            apply_retention=False,
        )
        checks: list[ReplayCheck] = []
        if target is not None:
            checks.append(ReplayCheck(target, replay, diff_checkpoints(target, replay)))
        result = ReplayResult(
            start_id=start.id,
            target_id=target.id if target else None,
            mode="step",
            steps=step_count,
            checks=checks,
            captures=[replay],
        )
        protected = {start.id, replay.id}
        if target is not None:
            protected.add(target.id)
        self._prune(protected=protected)
        return result

    def _advance_to(self, reference: Checkpoint) -> None:
        if reference.vbl is None:
            if self.session.at_prompt:
                self.session.continue_run()
            time.sleep(0.5)
            return
        current = self.read_vbl()
        if current is not None and current >= reference.vbl:
            return
        if self.session.at_prompt:
            self.session.continue_run()
        self.wait_until_vbl(reference.vbl, timeout=60.0)

    # -- convenience ---------------------------------------------------

    def recorded(self) -> list[Checkpoint]:
        return self.timeline.recorded()

    def replays(self) -> list[Checkpoint]:
        return self.timeline.replays()

    def info(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "session_id": self.session_id,
            "memory": self.memory_spec,
            "retention": self.retention,
            "checkpoints": len(self.timeline.recorded()),
            "replays": len(self.timeline.replays()),
            "load_error": self.store.load_error,
        }

    def __enter__(self) -> "CheckpointEngine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
