"""Tkinter time-travel debugger for the Hatari MCP server."""

from __future__ import annotations

import argparse
import contextlib
import math
import sys
import time
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # package import
    from common.mcp_client import MCPClient
    from common.ui import (
        ImagePanel,
        ScrollableList,
        ShutdownHooks,
        StatusLog,
        TaskRunner,
    )
except ImportError as exc:  # pragma: no cover - user-facing import error
    raise ImportError(
        "Run as a package with mcp/project/examples/programs on sys.path"
    ) from exc

from .engine import REPLAY_NOTE, CheckpointEngine, EngineError, ReplayResult
from .model import (
    Checkpoint,
    diff_checkpoints,
    format_memory_regions,
    memory_diff_ranges,
)
from .sample import build_sample

APP_TITLE = "Hatari Time-Travel Debugger"


class TimeTravelApp:
    def __init__(
        self,
        root: tk.Tk,
        engine: CheckpointEngine | None = None,
        *,
        auto_connect: bool = True,
        pending_prg: str | Path | None = None,
        pending_sample: bool = False,
    ):
        self.root = root
        self.engine = engine
        self.runner = TaskRunner(root)
        self.shutdown = ShutdownHooks()
        self.connected = False
        self.busy = False
        self._auto_job: str | None = None
        self._scale_guard = False
        self._timeline_items: list[Checkpoint] = []
        self.compare_a: Checkpoint | None = None
        self.compare_b: Checkpoint | None = None
        self.pending_prg = Path(pending_prg).expanduser() if pending_prg else None
        self.pending_sample = bool(pending_sample)

        self.auto_capture = tk.BooleanVar(value=False)
        self.cadence_mode = tk.StringVar(value="Host seconds")
        self.cadence_value = tk.DoubleVar(value=2.0)
        self.retention_value = tk.IntVar(value=engine.retention if engine else 24)
        self.memory_value = tk.StringVar(
            value=engine.memory_spec if engine else "0x70000:64"
        )
        self.status_var = tk.StringVar(value="Ready")

        self._action_widgets: list[ttk.Widget] = []
        self._build_ui()
        self.shutdown.bind_window(root)
        self.shutdown.add(self._shutdown)
        if engine is not None:
            self._log(f"session state: {engine.root}")
            self._log(f"memory regions: {engine.memory_spec}")
            if engine.store.load_error:
                self._log(f"timeline load warning: {engine.store.load_error}", level="error")
            self._refresh_timeline()
        if auto_connect and engine is not None:
            self.root.after(50, self._connect)
        else:
            self._set_controls_enabled(False)

    # -- construction --------------------------------------------------

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1320x860")
        self.root.minsize(1080, 700)

        toolbar = ttk.Frame(self.root, padding=(6, 4))
        toolbar.pack(side="top", fill="x")

        row = ttk.Frame(toolbar)
        row.pack(side="top", fill="x")
        self.boot_button = ttk.Button(row, text="Boot EmuTOS", command=self._boot_emutos)
        self.load_button = ttk.Button(row, text="Load PRG...", command=self._choose_prg)
        self.sample_button = ttk.Button(row, text="Run bundled sample", command=self._run_sample)
        self.capture_button = ttk.Button(row, text="Capture now", command=self._capture_now)
        self.stop_button = ttk.Button(row, text="Stop emulator", command=self._stop_emulator)
        self.boot_button.pack(side="left", padx=2)
        self.load_button.pack(side="left", padx=2)
        self.sample_button.pack(side="left", padx=2)
        self.capture_button.pack(side="left", padx=8)
        self.stop_button.pack(side="left", padx=2)
        self.auto_button = ttk.Checkbutton(
            row,
            text="Auto-capture",
            variable=self.auto_capture,
            command=self._toggle_auto,
        )
        self.auto_button.pack(side="left", padx=12)

        ttk.Label(row, text="Mode").pack(side="left", padx=(6, 2))
        self.mode_combo = ttk.Combobox(
            row,
            textvariable=self.cadence_mode,
            values=["Host seconds", "VBL frames"],
            state="readonly",
            width=13,
        )
        self.mode_combo.pack(side="left", padx=2)
        self.cadence_spin = ttk.Spinbox(
            row,
            from_=0.05,
            to=600.0,
            increment=0.25,
            textvariable=self.cadence_value,
            width=7,
        )
        self.cadence_spin.pack(side="left", padx=2)

        ttk.Label(row, text="Keep").pack(side="left", padx=(12, 2))
        self.retention_spin = ttk.Spinbox(
            row,
            from_=1,
            to=500,
            increment=1,
            textvariable=self.retention_value,
            width=5,
        )
        self.retention_spin.pack(side="left", padx=2)

        options = ttk.Frame(toolbar)
        options.pack(side="top", fill="x", pady=(4, 0))
        ttk.Label(options, text="Memory regions (ADDRESS:LENGTH,...)").pack(
            side="left", padx=(0, 4)
        )
        self.memory_entry = ttk.Entry(options, textvariable=self.memory_value, width=40)
        self.memory_entry.pack(side="left", padx=2)
        self.memory_apply = ttk.Button(options, text="Apply", command=self._apply_settings)
        self.memory_apply.pack(side="left", padx=2)
        ttk.Label(
            options,
            text="  Rewind = load snapshot; replay is approximate (no frame-locked input).",
            foreground="#8a4b00",
        ).pack(side="left", padx=8)

        self._action_widgets = [
            self.boot_button,
            self.load_button,
            self.sample_button,
            self.capture_button,
            self.stop_button,
            self.auto_button,
            self.memory_apply,
        ]

        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(side="top", fill="both", expand=True, padx=6, pady=4)

        left = ttk.Frame(paned, padding=2)
        paned.add(left, weight=1)
        ttk.Label(left, text="Timeline").pack(side="top", anchor="w")
        self.timeline_list = ScrollableList(left, height=22)
        self.timeline_list.pack(side="top", fill="both", expand=True)
        self.timeline_list.bind_select(self._on_timeline_select)
        self.timeline_list.listbox.bind(
            "<Double-Button-1>", lambda _event: self._rewind_selected()
        )

        self.timeline_scale = ttk.Scale(
            left,
            from_=0,
            to=0,
            orient=tk.HORIZONTAL,
            command=self._on_scale_move,
        )
        self.timeline_scale.pack(side="top", fill="x", pady=(4, 2))
        self.timeline_scale.bind("<ButtonRelease-1>", self._on_scale_release)
        self.timeline_position = ttk.Label(left, text="0 / 0")
        self.timeline_position.pack(side="top", anchor="w")

        buttons = ttk.Frame(left)
        buttons.pack(side="top", fill="x", pady=4)
        self.rewind_button = ttk.Button(buttons, text="Rewind", command=self._rewind_selected)
        self.step1_button = ttk.Button(buttons, text="Step 1", command=lambda: self._step_selected(1))
        self.step10_button = ttk.Button(buttons, text="Step 10", command=lambda: self._step_selected(10))
        self.continue_button = ttk.Button(buttons, text="Continue", command=self._continue_selected)
        self.delete_button = ttk.Button(buttons, text="Delete", command=self._delete_selected)
        for widget in (
            self.rewind_button,
            self.step1_button,
            self.step10_button,
            self.continue_button,
            self.delete_button,
        ):
            widget.pack(side="left", padx=2)
            self._action_widgets.append(widget)

        right = ttk.Frame(paned, padding=2)
        paned.add(right, weight=3)
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        screens = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(screens, text="Screens")
        screens.columnconfigure(0, weight=1)
        screens.columnconfigure(1, weight=1)
        screens.rowconfigure(1, weight=1)
        ttk.Label(screens, text="A - before / selected").grid(row=0, column=0, sticky="w")
        ttk.Label(screens, text="B - after / replay").grid(row=0, column=1, sticky="w")
        self.before_panel = ImagePanel(screens, width=480, height=320)
        self.after_panel = ImagePanel(screens, width=480, height=320)
        self.before_panel.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.after_panel.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.before_meta = ttk.Label(screens, text="-")
        self.after_meta = ttk.Label(screens, text="-")
        self.before_meta.grid(row=2, column=0, sticky="w", padx=4)
        self.after_meta.grid(row=2, column=1, sticky="w", padx=4)
        self.screen_summary = ttk.Label(screens, text="", wraplength=1100)
        self.screen_summary.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        registers_frame, self.register_text = self._text_tab("Registers")
        self.notebook.add(registers_frame, text="Registers")
        memory_frame, self.memory_text = self._text_tab("Memory")
        self.notebook.add(memory_frame, text="Memory")
        disassembly_frame, self.disassembly_text = self._text_tab("Disassembly")
        self.notebook.add(disassembly_frame, text="Disassembly")

        replay = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(replay, text="Replay")
        replay.columnconfigure(0, weight=1)
        controls = ttk.Frame(replay)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Start checkpoint").pack(side="left", padx=2)
        self.start_combo = ttk.Combobox(controls, state="readonly", width=18)
        self.start_combo.pack(side="left", padx=2)
        ttk.Label(controls, text="Target").pack(side="left", padx=(12, 2))
        self.target_combo = ttk.Combobox(controls, state="readonly", width=18)
        self.target_combo.pack(side="left", padx=2)
        ttk.Label(controls, text="Steps").pack(side="left", padx=(12, 2))
        self.steps_var = tk.IntVar(value=1)
        ttk.Spinbox(controls, from_=1, to=1000, textvariable=self.steps_var, width=6).pack(
            side="left", padx=2
        )
        self.replay_sequence_button = ttk.Button(
            controls, text="Replay sequence", command=self._replay_sequence
        )
        self.replay_continue_button = ttk.Button(
            controls, text="Continue to target", command=self._replay_continue
        )
        self.replay_step_button = ttk.Button(
            controls, text="Single-step", command=self._replay_step
        )
        self.replay_sequence_button.pack(side="left", padx=(12, 2))
        self.replay_continue_button.pack(side="left", padx=2)
        self.replay_step_button.pack(side="left", padx=2)
        self._action_widgets.extend(
            [
                self.replay_sequence_button,
                self.replay_continue_button,
                self.replay_step_button,
            ]
        )
        self.replay_note = ttk.Label(replay, text=REPLAY_NOTE, foreground="#8a4b00", wraplength=980)
        self.replay_note.grid(row=1, column=0, sticky="w", pady=6)
        self.replay_report = tk.Text(replay, height=22, wrap="word", state="disabled")
        self.replay_report.grid(row=2, column=0, sticky="nsew")
        replay.rowconfigure(2, weight=1)

        log_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(log_frame, text="Log")
        self.log = StatusLog(log_frame, height=24)
        self.log.pack(fill="both", expand=True)

        status = ttk.Frame(self.root, padding=(6, 2))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        ttk.Label(
            status,
            text="  |  rewind restores a snapshot; replay is approximate, not input-exact",
            foreground="#8a4b00",
        ).pack(side="left")

    def _text_tab(self, _name: str) -> tuple[ttk.Frame, tk.Text]:
        frame = ttk.Frame(self.notebook, padding=4)
        text = tk.Text(frame, wrap="none", height=24, state="disabled")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return frame, text

    # -- connection and shutdown --------------------------------------

    def _connect(self) -> None:
        if self.engine is None:
            self._log("no engine configured", level="error")
            return
        self._run(
            "Starting MCP server...",
            self.engine.client.start,
            on_success=self._after_connect,
            on_error=self._connect_failed,
        )

    def _after_connect(self, _client: Any) -> None:
        self.connected = True
        self._set_controls_enabled(True)
        self._log("MCP server ready")
        self._apply_settings(quiet=True)
        if self.pending_sample:
            self.pending_sample = False
            self._run_sample()
        elif self.pending_prg is not None:
            path = self.pending_prg
            self.pending_prg = None
            self._load_prg(path)

    def _connect_failed(self, exc: BaseException) -> None:
        self._set_controls_enabled(False)
        self._log(f"MCP connection failed: {exc}", level="error")

    def _shutdown(self) -> None:
        self._stop_auto()
        self.runner.close(wait=1.0)
        if self.engine is not None:
            with contextlib.suppress(BaseException):
                self.engine.stop(force=True)
            with contextlib.suppress(BaseException):
                self.engine.client.stop(timeout=15.0)

    def close(self) -> None:
        self._stop_auto()
        self.shutdown.run()

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in self._action_widgets:
            with contextlib.suppress(Exception):
                widget.configure(state=state)

    # -- task plumbing -------------------------------------------------

    def _run(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> bool:
        if self.busy:
            self._log(f"busy; skipped: {label}", level="warn")
            return False
        self.busy = True
        self.status_var.set(label)

        def success(result: Any) -> None:
            self.busy = False
            self.status_var.set("Ready")
            if on_success is not None:
                on_success(result)

        def error(exc: BaseException) -> None:
            self.busy = False
            self.status_var.set("Error")
            self._log(f"{label} failed: {type(exc).__name__}: {exc}", level="error")
            if on_error is not None:
                on_error(exc)

        self.runner.submit(fn, on_success=success, on_error=error, name=label)
        return True

    def _log(self, message: str, *, level: str = "info") -> None:
        with contextlib.suppress(Exception):
            self.log.log(message, level=level)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # -- emulator actions ---------------------------------------------

    def _boot_emutos(self) -> None:
        if self.engine is None:
            return
        self._stop_auto()
        self._run(
            "Booting EmuTOS...",
            lambda: self.engine.start_emutos(timeout=90.0),
            on_success=self._after_start,
        )

    def _choose_prg(self) -> None:
        path = filedialog.askopenfilename(
            title="Load an Atari PRG/TOS",
            filetypes=[
                ("Atari programs", "*.prg *.tos *.PRG *.TOS"),
                ("All files", "*"),
            ],
        )
        if path:
            self._load_prg(path)

    def _load_prg(self, path: str | Path) -> None:
        if self.engine is None:
            return
        self._stop_auto()
        self._run(
            f"Loading {Path(path).name}...",
            lambda: self.engine.load_prg(path, timeout=90.0),
            on_success=self._after_start,
        )

    def _run_sample(self) -> None:
        if self.engine is None:
            return
        self._stop_auto()

        def work() -> tuple[Path, dict[str, Any]]:
            prg = build_sample(self.engine.client)
            status = self.engine.load_prg(prg, timeout=90.0)
            return prg, status

        def done(result: tuple[Path, dict[str, Any]]) -> None:
            prg, status = result
            self._log(f"bundled sample loaded: {prg}")
            self._after_start(status)

        self._run("Building/loading bundled sample...", work, on_success=done)

    def _after_start(self, status: dict[str, Any]) -> None:
        self._log(f"emulator: {self._format_status(status)}")
        self._apply_settings(quiet=True)
        self._refresh_timeline()

    def _stop_emulator(self) -> None:
        if self.engine is None:
            return
        self._stop_auto()
        self._run("Stopping emulator...", self.engine.stop, on_success=self._after_stop)

    def _after_stop(self, status: dict[str, Any]) -> None:
        self._log(f"emulator: {self._format_status(status)}")
        self._set_text(self.disassembly_text, "Emulator stopped.")

    @staticmethod
    def _format_status(status: dict[str, Any]) -> str:
        state = status.get("state")
        pid = status.get("pid")
        prompt = status.get("at_prompt")
        error = status.get("last_error")
        text = f"state={state} pid={pid} prompt={prompt}"
        if error:
            text += f" last_error={error}"
        return text

    # -- capture -------------------------------------------------------

    def _apply_settings(self, *, quiet: bool = False) -> None:
        if self.engine is None:
            return
        try:
            retention = int(self.retention_value.get())
            self.engine.set_retention(retention)
            regions = self.engine.set_memory_regions(self.memory_value.get())
            self.memory_value.set(format_memory_regions(regions))
            if not quiet:
                self._log(f"settings: keep={retention} memory={self.engine.memory_spec}")
        except BaseException as exc:
            self._log(f"invalid settings: {exc}", level="error")
            if not quiet:
                messagebox.showerror("Invalid settings", str(exc), parent=self.root)

    def _capture_now(self) -> None:
        if self.engine is None:
            return
        self._apply_settings(quiet=True)
        self._run(
            "Capturing checkpoint...",
            self.engine.capture_checkpoint,
            on_success=self._after_capture,
        )

    def _after_capture(self, checkpoint: Checkpoint) -> None:
        self._present_new_checkpoint(checkpoint)
        self._log(
            f"captured {checkpoint.id} vbl={checkpoint.vbl} "
            f"frame={checkpoint.frame_sha256[:12]}"
        )

    def _present_new_checkpoint(self, checkpoint: Checkpoint) -> None:
        recorded = self.engine.recorded() if self.engine else []
        previous = checkpoint
        for index, item in enumerate(recorded):
            if item.id == checkpoint.id and index > 0:
                previous = recorded[index - 1]
                break
        self.engine.loaded_id = None
        self._show_comparison(previous, checkpoint)
        self._refresh_timeline(select_id=checkpoint.id)

    def _toggle_auto(self) -> None:
        if self.auto_capture.get():
            self._log("auto-capture on")
            self._schedule_auto(0)
        else:
            self._stop_auto(log=True)

    def _stop_auto(self, *, log: bool = False) -> None:
        if self._auto_job is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._auto_job)
            self._auto_job = None
        if self.auto_capture.get():
            self.auto_capture.set(False)
            if log:
                self._log("auto-capture off")

    def _schedule_auto(self, delay_ms: int) -> None:
        if not self.auto_capture.get():
            return
        self._auto_job = self.root.after(max(0, int(delay_ms)), self._auto_tick)

    def _auto_tick(self) -> None:
        self._auto_job = None
        if not self.auto_capture.get():
            return
        if self.busy:
            self._schedule_auto(300)
            return
        self._apply_settings(quiet=True)
        mode = self.cadence_mode.get()
        try:
            value = float(self.cadence_value.get())
        except (TypeError, ValueError):
            value = 2.0
        value = max(0.05, value)
        if mode == "VBL frames":
            work = lambda: self.engine.capture_checkpoint(wait_vbls=max(1, int(value)))
        else:
            work = self.engine.capture_checkpoint
        started = self._run(
            "Auto-capturing...",
            work,
            on_success=self._after_auto_capture,
            on_error=self._after_auto_error,
        )
        if not started:
            self._schedule_auto(300)

    def _after_auto_capture(self, checkpoint: Checkpoint) -> None:
        self._after_capture(checkpoint)
        if not self.auto_capture.get():
            return
        if self.cadence_mode.get() == "VBL frames":
            delay = 0
        else:
            try:
                delay = int(max(0.05, float(self.cadence_value.get())) * 1000)
            except (TypeError, ValueError):
                delay = 2000
        self._schedule_auto(delay)

    def _after_auto_error(self, _exc: BaseException) -> None:
        if self.auto_capture.get():
            self._schedule_auto(1000)

    # -- timeline ------------------------------------------------------

    @staticmethod
    def _checkpoint_label(checkpoint: Checkpoint) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(checkpoint.timestamp))
        vbl = "-" if checkpoint.vbl is None else str(checkpoint.vbl)
        suffix = ""
        if checkpoint.source == "replay":
            suffix = f"  [replay of {checkpoint.reference_id}]"
        if checkpoint.label:
            suffix += f"  {checkpoint.label}"
        return f"{checkpoint.id}  vbl={vbl}  {stamp}{suffix}"

    def _refresh_timeline(self, *, select_id: str | None = None) -> None:
        if self.engine is None:
            return
        items = self.engine.recorded()
        self._timeline_items = items
        self.timeline_list.set_items(items, label=self._checkpoint_label)
        self._refresh_replay_combos(items)
        if not items:
            self.timeline_scale.configure(to=0)
            self.timeline_position.configure(text="0 / 0")
            return
        last_index = len(items) - 1
        self.timeline_scale.configure(to=last_index)
        index = last_index
        if select_id is not None:
            for position, item in enumerate(items):
                if item.id == select_id:
                    index = position
                    break
        self._scale_guard = True
        with contextlib.suppress(Exception):
            self.timeline_scale.set(index)
        self._scale_guard = False
        self.timeline_position.configure(text=f"{index + 1} / {len(items)}")
        self._select_timeline_index(index, notify=False)

    def _refresh_replay_combos(self, items: list[Checkpoint]) -> None:
        ids = [item.id for item in items]
        current_start = self.start_combo.get()
        current_target = self.target_combo.get()
        self.start_combo["values"] = ids
        self.target_combo["values"] = ids
        if not ids:
            self.start_combo.set("")
            self.target_combo.set("")
            return
        if current_start not in ids:
            self.start_combo.set(ids[max(0, len(ids) - 2)])
        if current_target not in ids:
            self.target_combo.set(ids[-1])

    def _select_timeline_index(self, index: int, *, notify: bool) -> None:
        if not (0 <= index < len(self._timeline_items)):
            return
        listbox = self.timeline_list.listbox
        listbox.selection_clear(0, "end")
        listbox.selection_set(index)
        listbox.activate(index)
        listbox.see(index)
        self._scale_guard = True
        with contextlib.suppress(Exception):
            self.timeline_scale.set(index)
        self._scale_guard = False
        self.timeline_position.configure(
            text=f"{index + 1} / {len(self._timeline_items)}"
        )
        if notify:
            self._on_timeline_select(index, self._timeline_items[index])

    def _on_timeline_select(self, index: int, checkpoint: Checkpoint) -> None:
        self.compare_a = checkpoint
        self.compare_b = (
            self._timeline_items[index + 1]
            if index + 1 < len(self._timeline_items)
            else checkpoint
        )
        self.status_var.set(f"selected {checkpoint.id}")
        self._show_comparison(self.compare_a, self.compare_b)

    def _on_scale_move(self, value: str) -> None:
        if self._scale_guard or not self._timeline_items:
            return
        index = int(round(float(value)))
        index = max(0, min(index, len(self._timeline_items) - 1))
        self.timeline_position.configure(
            text=f"{index + 1} / {len(self._timeline_items)}"
        )

    def _on_scale_release(self, _event: Any) -> None:
        if not self._timeline_items:
            return
        index = int(round(float(self.timeline_scale.get())))
        self._select_timeline_index(index, notify=True)

    def _selected_checkpoint(self) -> Checkpoint | None:
        current = self.timeline_list.selection()
        if current is not None:
            return current[1]
        if self.engine is not None:
            return self.engine.timeline.get(self.engine.selected_id)
        return None

    def _delete_selected(self) -> None:
        if self.engine is None:
            return
        checkpoint = self._selected_checkpoint()
        if checkpoint is None:
            return
        if not messagebox.askyesno(
            "Delete checkpoint",
            f"Delete {checkpoint.id} and its state/screenshot/memory files?",
            parent=self.root,
        ):
            return
        self.engine.timeline.remove(checkpoint.id)
        self.engine.store.delete_checkpoint(checkpoint)
        self.engine.store.save_timeline(
            self.engine.timeline, session_id=self.engine.session_id
        )
        self._log(f"deleted {checkpoint.id}")
        self._refresh_timeline()

    # -- load, step, inspect ------------------------------------------

    def _rewind_selected(self) -> None:
        if self.engine is None:
            return
        checkpoint = self._selected_checkpoint()
        if checkpoint is None:
            self._log("select a checkpoint first", level="warn")
            return
        self._stop_auto()
        self._run(
            f"Rewinding to {checkpoint.id}...",
            lambda: self.engine.inspect_checkpoint(checkpoint.id),
            on_success=self._after_inspect,
        )

    def _after_inspect(self, result: dict[str, Any]) -> None:
        checkpoint: Checkpoint = result["checkpoint"]
        recorded = self.engine.recorded() if self.engine else []
        previous = checkpoint
        for index, item in enumerate(recorded):
            if item.id == checkpoint.id and index > 0:
                previous = recorded[index - 1]
                break
        self._show_comparison(previous, checkpoint, live=result)
        self._refresh_timeline(select_id=checkpoint.id)
        self._log(
            f"rewound to {checkpoint.id} (pc={result['registers'].get('pc')}); "
            "snapshot loaded, inspection is live"
        )

    def _step_selected(self, count: int) -> None:
        if self.engine is None:
            return
        checkpoint = self._selected_checkpoint()
        if checkpoint is None:
            self._log("select a checkpoint first", level="warn")
            return
        self._stop_auto()

        def work() -> dict[str, Any]:
            if self.engine.loaded_id != checkpoint.id:
                self.engine.load_checkpoint(checkpoint)
            self.engine.step(count)
            return self.engine.inspect_live()

        def done(result: dict[str, Any]) -> None:
            self._set_disassembly(result)
            self._show_comparison(self.compare_a, self.compare_b, live=result)
            self._log(f"stepped {count} instruction(s) from {checkpoint.id}")

        self._run(f"Stepping {count}...", work, on_success=done)

    def _continue_selected(self) -> None:
        if self.engine is None:
            return
        checkpoint = self._selected_checkpoint()
        if checkpoint is None:
            self._log("select a checkpoint first", level="warn")
            return
        self._stop_auto()

        def work() -> dict[str, Any]:
            if self.engine.loaded_id != checkpoint.id:
                self.engine.load_checkpoint(checkpoint)
            return self.engine.continue_run()

        def done(_result: dict[str, Any]) -> None:
            self._log(f"continued from {checkpoint.id}; capture now to record a new state")

        self._run(f"Continuing from {checkpoint.id}...", work, on_success=done)

    # -- replay --------------------------------------------------------

    def _replay_inputs(self) -> tuple[str, str] | None:
        start = self.start_combo.get()
        target = self.target_combo.get()
        if not start or not target:
            self._log("select start and target checkpoints", level="warn")
            return None
        return start, target

    def _replay_sequence(self) -> None:
        if self.engine is None:
            return
        pair = self._replay_inputs()
        if pair is None:
            return
        start, target = pair
        self._stop_auto()
        self._run(
            f"Replaying {start} -> {target}...",
            lambda: self.engine.replay_sequence(start, target),
            on_success=self._after_replay,
        )

    def _replay_continue(self) -> None:
        if self.engine is None:
            return
        pair = self._replay_inputs()
        if pair is None:
            return
        start, target = pair
        self._stop_auto()
        self._run(
            f"Continuing replay {start} -> {target}...",
            lambda: self.engine.replay_to_target(start, target),
            on_success=self._after_replay,
        )

    def _replay_step(self) -> None:
        if self.engine is None:
            return
        pair = self._replay_inputs()
        if pair is None:
            return
        start, target = pair
        steps = max(1, int(self.steps_var.get()))
        self._stop_auto()
        self._run(
            f"Step replay {start} ({steps})...",
            lambda: self.engine.replay_steps(start, target, steps=steps),
            on_success=self._after_replay,
        )

    def _after_replay(self, result: ReplayResult) -> None:
        if result.checks:
            last = result.checks[-1]
            self.compare_a = last.reference
            self.compare_b = last.replay
            self._show_comparison(last.reference, last.replay)
        elif result.captures:
            capture = result.captures[-1]
            self.compare_a = self.engine.timeline.get(result.start_id) or capture
            self.compare_b = capture
            self._show_comparison(self.compare_a, capture)
        if result.captures:
            self._set_disassembly(
                {
                    "registers": result.captures[-1].registers,
                    "disassembly": {"raw": "Replay capture saved; click Rewind to inspect it."},
                }
            )
        self._set_text(self.replay_report, self._format_replay_report(result))
        self._log(result.summary(), level="info" if result.matched else "warn")
        if result.first_divergence is not None:
            self._log(
                f"first divergence: {result.first_divergence.describe()}", level="warn"
            )
        self._refresh_timeline()

    def _format_replay_report(self, result: ReplayResult) -> str:
        lines = [
            result.summary(),
            "",
            f"mode: {result.mode}  steps: {result.steps}  approximate: {result.approximate}",
            f"note: {result.note}",
            "",
        ]
        if not result.checks:
            lines.append("no target checkpoint comparison was requested")
            if result.captures:
                lines.append(f"captured: {result.captures[-1].id}")
            return "\n".join(lines)
        for check in result.checks:
            lines.append(
                f"{check.reference.id} vs {check.replay.id}: {check.report.summary()}"
            )
            for divergence in check.report.divergences:
                lines.append(f"  - {divergence.describe()}")
            lines.append("")
        return "\n".join(lines)

    # -- views ---------------------------------------------------------

    def _show_comparison(
        self,
        before: Checkpoint | None,
        after: Checkpoint | None,
        *,
        live: dict[str, Any] | None = None,
    ) -> None:
        self.compare_a = before
        self.compare_b = after
        self._show_image(self.before_panel, self.before_meta, before)
        self._show_image(self.after_panel, self.after_meta, after)
        if before is not None and after is not None:
            report = diff_checkpoints(before, after)
            self.screen_summary.configure(text=report.summary())
            self._set_text(
                self.register_text, self._format_register_view(before, after, live)
            )
            self._set_text(self.memory_text, self._format_memory_view(before, after))
        else:
            self.screen_summary.configure(text="No two checkpoints selected.")
            self._set_text(self.register_text, "No checkpoint selected.")
            self._set_text(self.memory_text, "No checkpoint selected.")
        if live is not None:
            self._set_disassembly(live)

    def _show_image(self, panel: ImagePanel, meta: Any, checkpoint: Checkpoint | None) -> None:
        if checkpoint is None or not checkpoint.screenshot_path:
            panel.show_text("No screenshot")
            meta.configure(text="-")
            return
        path = Path(checkpoint.screenshot_path)
        photo = self._load_photo(path)
        if photo is None:
            panel.show_text(f"No image\n{path.name}")
            meta.configure(text=f"{checkpoint.id}  missing screenshot")
            return
        panel.show_photo(photo)
        meta.configure(
            text=(
                f"{checkpoint.id}  vbl={checkpoint.vbl}  "
                f"frame={checkpoint.frame_sha256[:16]}"
            )
        )

    def _load_photo(self, path: Path) -> Any:
        try:
            photo = tk.PhotoImage(file=str(path))
        except Exception:
            return None
        width = max(1, int(photo.width()))
        height = max(1, int(photo.height()))
        factor = max(1, math.ceil(width / 470), math.ceil(height / 330))
        if factor > 1:
            with contextlib.suppress(Exception):
                photo = photo.subsample(factor, factor)
        return photo

    def _format_register_view(
        self,
        before: Checkpoint,
        after: Checkpoint,
        live: dict[str, Any] | None = None,
    ) -> str:
        report = diff_checkpoints(before, after)
        lines = [
            f"A: {before.id}   B: {after.id}",
            report.summary(),
            "",
            f"{'reg':>4}  {'A':>18}  {'B':>18}",
            "-" * 48,
        ]
        names = sorted((set(before.registers) | set(after.registers)) | {"pc"})
        for name in names:
            left = before.registers.get(name, "-")
            right = after.registers.get(name, "-")
            marker = "  DIFF" if left != right else ""
            lines.append(f"{name:>4}  {left:>18}  {right:>18}{marker}")
        if live is not None:
            lines.extend(["", "Live registers after load/step:", "-" * 48])
            live_regs = live.get("registers") or {}
            for name in sorted(live_regs):
                lines.append(f"{name:>4}  {live_regs[name]}")
        return "\n".join(lines)

    def _format_memory_view(self, before: Checkpoint, after: Checkpoint) -> str:
        report = diff_checkpoints(before, after)
        lines = [
            f"A: {before.id}   B: {after.id}",
            report.summary(),
            "",
        ]
        addresses = sorted(
            set(before.memory_by_address()) | set(after.memory_by_address())
        )
        before_map = before.memory_by_address()
        after_map = after.memory_by_address()
        for address in addresses:
            left = before_map.get(address)
            right = after_map.get(address)
            lines.append(f"region 0x{address:x}")
            lines.append(
                f"  A len={left.length if left else '-'} sha={left.sha256[:16] if left else '-'}"
            )
            lines.append(
                f"  B len={right.length if right else '-'} sha={right.sha256[:16] if right else '-'}"
            )
        ranges = memory_diff_ranges(before, after)
        lines.extend(["", f"changed ranges: {len(ranges)}", "-" * 72])
        if not ranges:
            lines.append("no changed bytes in the captured regions")
        for item in ranges:
            lines.append(
                f"0x{item.address:08x}  {item.length:4d} bytes  "
                f"A={item.before_hex or '-'}  B={item.after_hex or '-'}"
            )
        if len(ranges) >= 64:
            lines.append("(range list truncated at 64)")
        return "\n".join(lines)

    def _set_disassembly(self, result: dict[str, Any]) -> None:
        registers = result.get("registers") or {}
        disassembly = result.get("disassembly") or {}
        lines: list[str] = []
        if registers:
            lines.append(
                f"pc={registers.get('pc', '-')} sr={registers.get('sr', '-')} "
                f"a7={registers.get('a7', '-')}"
            )
            lines.append("")
        if isinstance(disassembly.get("lines"), list):
            lines.extend(str(item) for item in disassembly["lines"])
        elif disassembly.get("raw"):
            lines.append(str(disassembly["raw"]))
        elif disassembly.get("error"):
            lines.append(f"disassembly error: {disassembly['error']}")
        else:
            lines.append("no disassembly")
        self._set_text(self.disassembly_text, "\n".join(lines))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", help="base state directory (timeline lives below it)")
    parser.add_argument("--session-id", help="checkpoint session directory name")
    parser.add_argument("--machine", default="st", choices=["st", "ste", "tt", "falcon"])
    parser.add_argument("--tos", help="TOS/EmuTOS image")
    parser.add_argument("--prg", help="PRG/TOS to load after connecting")
    parser.add_argument("--sample", action="store_true", help="build and load the bundled sample")
    parser.add_argument("--retention", type=int, default=24, help="checkpoints to keep (0 = unlimited)")
    parser.add_argument("--memory", default="0x70000:64", help="ADDRESS:LENGTH[,ADDRESS:LENGTH...]")
    parser.add_argument("--no-connect", action="store_true", help="build the UI without starting MCP")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = None if args.no_connect else MCPClient()
    engine = None
    if client is not None:
        engine = CheckpointEngine(
            client,
            state_base=args.state_root,
            session_id=args.session_id,
            machine=args.machine,
            tos=args.tos,
            memory_regions=args.memory,
            retention=args.retention,
        )
    root = tk.Tk()
    app = TimeTravelApp(
        root,
        engine,
        auto_connect=not args.no_connect,
        pending_prg=args.prg,
        pending_sample=args.sample,
    )
    try:
        root.mainloop()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
