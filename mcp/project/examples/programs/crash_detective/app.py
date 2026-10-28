#!/usr/bin/env python3
"""Tkinter crash detective for Atari ST disks and PRG programs.

Run from the repository root::

    mcp/.venv/bin/python mcp/project/examples/programs/crash_detective/app.py

Use ``--no-scan`` for a fast/headless window smoke and ``--autoclose-ms`` to
exit automatically (used by the test suite).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

PROGRAMS = Path(__file__).resolve().parents[1]
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import Catalog, Config, paths  # noqa: E402
from common.catalog import DiskEntry  # noqa: E402
from common.game_runner import normalise_launch_path  # noqa: E402
from common.ui import (  # noqa: E402
    ImagePanel,
    ScrollableList,
    ShutdownHooks,
    StatusLog,
    TaskRunner,
    show_error,
    show_info,
)

from crash_detective import analysis  # noqa: E402
from crash_detective.controller import CrashDetective, resolve_state_dir  # noqa: E402
from crash_detective.discovery import PrgTarget, catalog_roots, filter_entries, prg_target  # noqa: E402


class CrashDetectiveApp:
    def __init__(
        self,
        root: Any,
        *,
        state_dir: str | Path | None = None,
        games: list[str] | None = None,
        machine: str = "st",
        tos: str | Path | None = None,
        timeout: float = 45.0,
        no_scan: bool = False,
        autoclose_ms: int | None = None,
    ):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.state_dir = resolve_state_dir(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = Config(self.state_dir / "config.json")
        self.controller = CrashDetective(
            state_dir=self.state_dir,
            config=self.config,
            machine=machine,
            tos=tos,
            timeout=timeout,
        )
        self.runner = TaskRunner(root)
        self.hooks = ShutdownHooks()
        self.hooks.add(self._shutdown)
        self.hooks.bind_window(root)
        self._closed = False
        self._run_active = False
        self._monitor_stop = threading.Event()
        self._monitor_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._last_run: dict[str, Any] | None = None
        self._report_text = ""
        self._selected_entry: DiskEntry | None = None
        self._catalog_ids: tuple[str, ...] = ()
        self._photo: Any = None
        self._diagnosis: dict[str, dict[str, Any]] = {}
        self._after_ids: list[str] = []
        self._last_monitor_state: str | None = None

        self.root.title("Hatari Crash Detective")
        self.root.geometry("1320x860")
        self.root.minsize(980, 680)
        self.machine_var = tk.StringVar(value=machine)
        self.tos_var = tk.StringVar(value=str(tos or paths.find_tos("emutos") or ""))
        self.timeout_var = tk.StringVar(value=str(int(timeout)))
        self.debug_exc_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar(value="")
        self.prg_var = tk.StringVar(value="")
        self.autorun_var = tk.StringVar(value="")
        self.catalog_count_var = tk.StringVar(value="Catalog not started")
        self.selected_var = tk.StringVar(value="Select a disk image")
        self.status_var = tk.StringVar(value="Idle — select a game or PRG and press Launch")
        self.bundle_var = tk.StringVar(value="")
        self._build_ui()

        roots = catalog_roots(
            [*self.config.game_dirs, *(games or [])]
        )
        self.catalog = Catalog(roots, state_dir=self.state_dir, metadata_dir=self.state_dir / "metadata")
        self.no_scan = bool(no_scan)
        if not self.no_scan:
            self.catalog.start_scan()
            self._log(f"Scanning {len(roots)} corpus root(s) in the background", level="info")
        else:
            self.catalog_count_var.set("Catalog scan disabled (--no-scan)")

        self._after_ids.append(self.root.after(300, self._refresh_catalog))
        self._after_ids.append(self.root.after(250, self._drain_monitor))
        if autoclose_ms is not None and autoclose_ms > 0:
            self._after_ids.append(self.root.after(int(autoclose_ms), self.close))

    # -- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk
        root = self.root

        toolbar = ttk.Frame(root, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Hatari Crash Detective", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Label(toolbar, text="   Machine").pack(side="left")
        ttk.Combobox(
            toolbar,
            textvariable=self.machine_var,
            values=("st", "ste", "tt", "falcon"),
            width=8,
            state="readonly",
        ).pack(side="left", padx=(4, 12))
        ttk.Label(toolbar, text="TOS").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.tos_var, width=42).pack(side="left", padx=(4, 4))
        ttk.Button(toolbar, text="Browse…", command=self._browse_tos).pack(side="left", padx=(0, 12))
        ttk.Label(toolbar, text="Timeout").pack(side="left")
        ttk.Spinbox(toolbar, from_=10, to=180, textvariable=self.timeout_var, width=6).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(
            toolbar,
            text="Break on guest exceptions",
            variable=self.debug_exc_var,
        ).pack(side="left")

        paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=3)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent: Any) -> None:
        tk = self.tk
        ttk = self.ttk
        ttk.Label(parent, text="Source", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.source_tabs = ttk.Notebook(parent)
        self.source_tabs.pack(fill="both", expand=True, pady=(4, 6))

        corpus = ttk.Frame(self.source_tabs, padding=4)
        prg = ttk.Frame(self.source_tabs, padding=4)
        self.source_tabs.add(corpus, text="TOSEC corpus")
        self.source_tabs.add(prg, text="PRG file")

        search_row = ttk.Frame(corpus)
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Search").pack(side="left")
        search = ttk.Entry(search_row, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._refresh_catalog())

        self.game_list = ScrollableList(corpus, height=22)
        self.game_list.pack(fill="both", expand=True, pady=(4, 4))
        self.game_list.bind_select(self._on_game_select)
        ttk.Label(corpus, textvariable=self.catalog_count_var, foreground="#555").pack(anchor="w")
        ttk.Label(corpus, textvariable=self.selected_var, wraplength=340, justify="left").pack(anchor="w", pady=(2, 0))

        ttk.Label(corpus, text="Autorun path (disk only)").pack(anchor="w", pady=(6, 0))
        ttk.Entry(corpus, textvariable=self.autorun_var).pack(fill="x", pady=(2, 0))
        ttk.Label(
            corpus,
            text=(
                "Optional guest program, e.g. RUNME.PRG or FOLDER/RUNME.TOS.\n"
                "Relative paths run from A:; use --auto on the boot disk."
            ),
            foreground="#555",
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Label(prg, text="PRG path").pack(anchor="w")
        prg_row = ttk.Frame(prg)
        prg_row.pack(fill="x", pady=(4, 4))
        ttk.Entry(prg_row, textvariable=self.prg_var).pack(side="left", fill="x", expand=True)
        ttk.Button(prg_row, text="Browse…", command=self._browse_prg).pack(side="left", padx=(4, 0))
        ttk.Label(
            prg,
            text=(
                "PRGs are staged in a space-free GEMDOS workspace by DebugSession.\n"
                "Enable 'Break on guest exceptions' to stop at CPU faults."
            ),
            foreground="#555",
            wraplength=340,
            justify="left",
        ).pack(anchor="w")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(4, 0))
        self.launch_button = ttk.Button(actions, text="Launch", command=self._launch)
        self.launch_button.pack(side="left", fill="x", expand=True)
        self.abort_button = ttk.Button(actions, text="Abort", command=self._abort, state="disabled")
        self.abort_button.pack(side="left", padx=(6, 0))

    def _build_right(self, parent: Any) -> None:
        tk = self.tk
        ttk = self.ttk

        status_frame = ttk.Frame(parent)
        status_frame.pack(fill="x")
        ttk.Label(status_frame, textvariable=self.status_var, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        ttk.Label(status_frame, textvariable=self.bundle_var, foreground="#555", wraplength=900, justify="left").pack(anchor="w")

        middle = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        middle.pack(fill="both", expand=True, pady=(6, 6))
        log_frame = ttk.Frame(middle)
        preview_frame = ttk.Frame(middle)
        middle.add(log_frame, weight=2)
        middle.add(preview_frame, weight=1)
        ttk.Label(log_frame, text="Live status / log").pack(anchor="w")
        self.log = StatusLog(log_frame, height=12)
        self.log.pack(fill="both", expand=True, pady=(2, 0))
        ttk.Label(preview_frame, text="Screenshot").pack(anchor="w")
        self.image_panel = ImagePanel(preview_frame, width=360, height=260)
        self.image_panel.pack(fill="both", expand=True, pady=(2, 0))
        self.image_panel.show_text("No run yet")

        lower = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        lower.pack(fill="both", expand=True)
        diagnosis_frame = ttk.Frame(lower)
        evidence_frame = ttk.Frame(lower)
        lower.add(diagnosis_frame, weight=1)
        lower.add(evidence_frame, weight=2)

        ttk.Label(diagnosis_frame, text="Diagnosis (hypotheses, not confirmed)").pack(anchor="w")
        columns = ("confidence", "hypothesis", "evidence")
        self.diagnosis_tree = ttk.Treeview(
            diagnosis_frame,
            columns=columns,
            show="headings",
            height=9,
            selectmode="browse",
        )
        self.diagnosis_tree.heading("confidence", text="Conf.")
        self.diagnosis_tree.heading("hypothesis", text="Hypothesis")
        self.diagnosis_tree.heading("evidence", text="Lead evidence")
        self.diagnosis_tree.column("confidence", width=62, anchor="center", stretch=False)
        self.diagnosis_tree.column("hypothesis", width=220, anchor="w")
        self.diagnosis_tree.column("evidence", width=300, anchor="w")
        tree_scroll = ttk.Scrollbar(diagnosis_frame, orient="vertical", command=self.diagnosis_tree.yview)
        self.diagnosis_tree.configure(yscrollcommand=tree_scroll.set)
        self.diagnosis_tree.pack(side="left", fill="both", expand=True, pady=(2, 0))
        tree_scroll.pack(side="right", fill="y", pady=(2, 0))
        self.diagnosis_tree.bind("<<TreeviewSelect>>", self._on_diagnosis_select)

        ttk.Label(evidence_frame, text="Evidence / report").pack(anchor="w")
        self.evidence_text = tk.Text(evidence_frame, wrap="word", height=9, state="disabled")
        evidence_scroll = ttk.Scrollbar(evidence_frame, orient="vertical", command=self.evidence_text.yview)
        self.evidence_text.configure(yscrollcommand=evidence_scroll.set)
        self.evidence_text.pack(side="left", fill="both", expand=True, pady=(2, 0))
        evidence_scroll.pack(side="right", fill="y", pady=(2, 0))

        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=(4, 0))
        self.mark_success_button = ttk.Button(
            buttons, text="Mark run success", command=lambda: self._mark("success"), state="disabled"
        )
        self.mark_success_button.pack(side="left")
        self.mark_failure_button = ttk.Button(
            buttons, text="Mark run failure", command=lambda: self._mark("failure"), state="disabled"
        )
        self.mark_failure_button.pack(side="left", padx=(6, 0))
        self.copy_button = ttk.Button(buttons, text="Copy report", command=self._copy_report, state="disabled")
        self.copy_button.pack(side="left", padx=(18, 0))
        self.open_button = ttk.Button(buttons, text="Open bundle", command=self._open_bundle, state="disabled")
        self.open_button.pack(side="left", padx=(6, 0))

    # -- catalog ---------------------------------------------------------

    def _refresh_catalog(self) -> None:
        if self._closed:
            return
        try:
            entries = filter_entries(
                self.catalog.snapshot(), self.search_var.get(), limit=500, sort="title"
            )
            identifiers = tuple(
                str(item.id if hasattr(item, "id") else item.get("id", "")) for item in entries
            )
            if identifiers != self._catalog_ids:
                self._catalog_ids = identifiers
                self.game_list.set_items(
                    entries,
                    label=lambda item: f"{item.display_name}  [{item.filename}]",
                )
            scanning = self.catalog.scanning
            state = "scanning" if scanning else "ready"
            self.catalog_count_var.set(
                f"{len(entries)} shown / {len(self.catalog)} indexed ({state})"
            )
        except BaseException as exc:  # noqa: BLE001 - keep the UI alive
            self.catalog_count_var.set(f"catalog error: {type(exc).__name__}: {exc}")
        finally:
            if not self._closed:
                self._after_ids.append(self.root.after(800, self._refresh_catalog))

    def _on_game_select(self, _index: int, entry: DiskEntry) -> None:
        self._selected_entry = entry
        self.selected_var.set(
            f"{entry.display_name}\n{entry.relative}\n"
            f"publisher={entry.publisher or '?'}  genre={entry.genre or '?'}  size={entry.size} bytes"
        )
        saved = self.config.get_game(entry.id).get("autorun_path") or ""
        self.autorun_var.set(str(saved))

    def _browse_tos(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select a TOS or EmuTOS image",
            filetypes=[("ROM images", "*.img *.rom *.bin"), ("All files", "*")],
        )
        if selected:
            self.tos_var.set(selected)

    def _browse_prg(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select an Atari ST PRG",
            filetypes=[("Atari ST programs", "*.prg *.PRG"), ("All files", "*")],
        )
        if selected:
            self.prg_var.set(selected)
            self.source_tabs.select(1)

    # -- launch / abort --------------------------------------------------

    def _launch(self) -> None:
        if self._run_active:
            self._log("A run is already active", level="warn")
            return
        disk: DiskEntry | None = None
        prg: PrgTarget | None = None
        try:
            if self.source_tabs.index("current") == 0:
                disk = self._selected_entry
                if disk is None:
                    show_info(self.root, "Select a game", "Choose a disk image from the catalog first.")
                    return
            else:
                raw = self.prg_var.get().strip()
                if not raw:
                    show_info(self.root, "Select a PRG", "Choose a .prg file first.")
                    return
                prg = prg_target(raw)
        except BaseException as exc:  # noqa: BLE001 - user input boundary
            show_error(self.root, "Invalid selection", str(exc))
            return

        machine = self.machine_var.get().strip() or "st"
        tos = self.tos_var.get().strip() or None
        try:
            timeout = max(5.0, float(self.timeout_var.get()))
        except ValueError:
            show_error(self.root, "Invalid timeout", "Timeout must be a number of seconds.")
            return
        debug_exceptions = bool(self.debug_exc_var.get())
        autorun_path: str | None = None
        if disk is not None:
            try:
                autorun_path = normalise_launch_path(self.autorun_var.get())
            except ValueError as exc:
                show_error(self.root, "Invalid autorun path", str(exc))
                return
            self.config.update_game(disk.id, {"autorun_path": autorun_path})
        self._run_active = True
        self._last_run = None
        self._last_monitor_state = None
        self._diagnosis = {}
        self._report_text = ""
        self._set_evidence("")
        for item in self.diagnosis_tree.get_children():
            self.diagnosis_tree.delete(item)
        self.image_panel.show_text("Launching…")
        self.launch_button.configure(state="disabled")
        self.abort_button.configure(state="normal")
        self.mark_success_button.configure(state="disabled")
        self.mark_failure_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.bundle_var.set("")
        self.status_var.set("Launching and collecting evidence…")
        self.log.clear()
        name = disk.display_name if disk is not None else prg.title
        self._log(f"Launching {name} on {machine}")
        if autorun_path:
            self._log(f"Autorun path: {autorun_path}", level="info")
        self._monitor_stop.clear()
        self.runner.submit(self._monitor_loop, name="crash-detective-monitor")
        if disk is not None:
            self.runner.submit(
                lambda: self.controller.launch_disk(
                    disk,
                    machine=machine,
                    tos=tos,
                    timeout=timeout,
                    debug_exceptions=debug_exceptions,
                    autorun_path=autorun_path,
                ),
                on_success=self._on_run_success,
                on_error=self._on_run_error,
                name="crash-detective-disk",
            )
        else:
            assert prg is not None
            self.runner.submit(
                lambda: self.controller.launch_prg(
                    prg,
                    machine=machine,
                    tos=tos,
                    timeout=timeout,
                    debug_exceptions=debug_exceptions,
                ),
                on_success=self._on_run_success,
                on_error=self._on_run_error,
                name="crash-detective-prg",
            )

    def _abort(self) -> None:
        self._log("Abort requested; stopping the emulator", level="warn")
        self._monitor_stop.set()
        self.abort_button.configure(state="disabled")
        self.runner.submit(self.controller.abort, name="crash-detective-abort")

    def _monitor_loop(self) -> None:
        previous: str | None = None
        while not self._monitor_stop.wait(0.5):
            status = self.controller.status()
            state = str(status.get("state") or "unknown")
            self._monitor_queue.put({"state": state, "status": status})
            if state != previous:
                previous = state
        return None

    def _drain_monitor(self) -> None:
        if self._closed:
            return
        changed: str | None = None
        last_status: dict[str, Any] | None = None
        while True:
            try:
                event = self._monitor_queue.get_nowait()
            except queue.Empty:
                break
            last_status = event.get("status")
            changed = event.get("state")
        if last_status is not None and self._run_active:
            state = str(last_status.get("state") or "unknown")
            self.status_var.set(f"Running — emulator state: {state}")
            if state != self._last_monitor_state:
                self._last_monitor_state = state
                for line in self._status_lines(last_status):
                    self._log(line, level="info")
        self._after_ids.append(self.root.after(250, self._drain_monitor))

    @staticmethod
    def _status_lines(status: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        if status.get("exception"):
            lines.append(f"exception: {status.get('exception')}")
        if status.get("exit_reason"):
            lines.append(f"exit reason: {status.get('exit_reason')} returncode={status.get('returncode')}")
        if status.get("last_error"):
            lines.append(f"last error: {status.get('last_error')}")
        return lines

    def _on_run_success(self, payload: dict[str, Any]) -> None:
        self._run_active = False
        self._monitor_stop.set()
        self.launch_button.configure(state="normal")
        self.abort_button.configure(state="disabled")
        self._last_run = payload
        record = payload.get("record") or {}
        classification = payload.get("classification") or {}
        result = record.get("result") or {}
        booted = classification.get("booted")
        if booted:
            prefix = "BOOTED"
        elif booted is False:
            prefix = "FAILED"
        else:
            prefix = "UNDETERMINED"
        self.status_var.set(
            f"{prefix} — {classification.get('category')} ({float(classification.get('confidence') or 0) * 100:.0f}%) — {result.get('message') or ''}"
        )
        self.bundle_var.set(f"Bundle: {payload.get('bundle') or record.get('bundle') or ''}")
        self._log(f"Classification: {classification.get('summary')}", level="info" if booted else "warn")
        for signal in classification.get("signals", []):
            self._log(f"signal {signal.get('code')}: {signal.get('detail')}", level="info")
        hypotheses = payload.get("hypotheses") or []
        for hypothesis in hypotheses:
            self._log(
                f"hypothesis {hypothesis.get('confidence')}: {hypothesis.get('title')}",
                level="warn" if booted is False else "info",
            )
        self._render_hypotheses(hypotheses)
        self._render_evidence(payload)
        self._render_screenshot(record, payload)
        self.mark_success_button.configure(state="normal")
        self.mark_failure_button.configure(state="normal")
        self.copy_button.configure(state="normal")
        self.open_button.configure(state="normal")

    def _on_run_error(self, exc: BaseException) -> None:
        self._run_active = False
        self._monitor_stop.set()
        self.launch_button.configure(state="normal")
        self.abort_button.configure(state="disabled")
        self.status_var.set(f"Run failed before evidence collection: {type(exc).__name__}: {exc}")
        self._log(f"{type(exc).__name__}: {exc}", level="error")
        show_error(self.root, "Crash detective error", f"{type(exc).__name__}: {exc}")

    # -- rendering -------------------------------------------------------

    def _render_hypotheses(self, hypotheses: list[dict[str, Any]]) -> None:
        self._diagnosis = {}
        for item in self.diagnosis_tree.get_children():
            self.diagnosis_tree.delete(item)
        for hypothesis in hypotheses:
            evidence = hypothesis.get("evidence") or []
            lead = evidence[0] if evidence else ""
            identifier = self.diagnosis_tree.insert(
                "",
                "end",
                values=(
                    f"{float(hypothesis.get('confidence') or 0) * 100:.0f}%",
                    hypothesis.get("title") or hypothesis.get("id"),
                    lead,
                ),
            )
            self._diagnosis[str(identifier)] = hypothesis

    def _render_evidence(self, payload: dict[str, Any]) -> None:
        bundle = Path(str(payload.get("bundle") or ""))
        report_path = bundle / "report.md"
        text = ""
        if report_path.is_file():
            with contextlib.suppress(OSError):
                text = report_path.read_text(encoding="utf-8")
        if not text:
            text = analysis.build_report(
                payload.get("record") or {},
                payload.get("classification") or {},
                payload.get("hypotheses") or [],
            )
        self._report_text = text
        self._set_evidence(text)

    def _set_evidence(self, text: str) -> None:
        try:
            self.evidence_text.configure(state="normal")
            self.evidence_text.delete("1.0", "end")
            self.evidence_text.insert("1.0", text)
            self.evidence_text.configure(state="disabled")
        except Exception:
            return

    def _render_screenshot(self, record: dict[str, Any], payload: dict[str, Any]) -> None:
        candidates: list[Path] = []
        bundle = Path(str(payload.get("bundle") or ""))
        if bundle.is_dir():
            candidates.extend(sorted((bundle / "screenshots").glob("*"), reverse=True))
        for frame in reversed(record.get("frames") or []):
            if isinstance(frame, dict) and frame.get("path"):
                candidates.append(Path(str(frame["path"])))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            photo = self._photo_from_bmp(candidate)
            if photo is not None:
                self._photo = photo
                self.image_panel.show_photo(photo)
                return
        self.image_panel.show_text("No screenshot captured")

    def _photo_from_bmp(self, path: Path) -> Any:
        try:
            data = analysis.bmp_to_ppm_bytes(path)
        except (OSError, ValueError):
            return None
        handle = tempfile.NamedTemporaryFile(suffix=".ppm", delete=False, dir=str(self.state_dir))
        name = handle.name
        try:
            handle.write(data)
            handle.close()
            return self.tk.PhotoImage(file=name, master=self.root)
        except Exception:
            return None
        finally:
            with contextlib.suppress(OSError):
                Path(name).unlink()

    def _on_diagnosis_select(self, _event: Any) -> None:
        selection = self.diagnosis_tree.selection()
        if not selection:
            return
        hypothesis = self._diagnosis.get(str(selection[0]))
        if not hypothesis:
            return
        lines = [
            f"{hypothesis.get('title')} — confidence {hypothesis.get('confidence')}",
            "",
            str(hypothesis.get("explanation") or ""),
            "",
            "Evidence:",
        ]
        lines.extend(f"- {item}" for item in hypothesis.get("evidence") or [])
        lines += ["", "This is a hypothesis, not a confirmed root cause."]
        self._set_evidence("\n".join(lines))

    # -- actions ---------------------------------------------------------

    def _mark(self, outcome: str) -> None:
        if not self._last_run:
            show_info(self.root, "No run", "Launch a game or PRG before marking an outcome.")
            return
        record = self._last_run.get("record") or {}
        game = record.get("game") or {}
        classification = self._last_run.get("classification") or {}
        result = record.get("result") or {}
        try:
            entry = analysis.record_outcome(
                self.state_dir,
                str(record.get("run_id") or ""),
                str(game.get("id") or ""),
                outcome,
                title=str(game.get("title") or game.get("filename") or ""),
                note=f"detected={classification.get('category')} message={result.get('message')}",
                bundle=self._last_run.get("bundle"),
                detected=str(classification.get("category") or ""),
            )
        except BaseException as exc:  # noqa: BLE001
            show_error(self.root, "Could not save outcome", str(exc))
            return
        self._log(f"User marked this run as {outcome} (saved for {entry['run_id']})", level="info")
        self.status_var.set(
            f"User outcome saved: {outcome} — detected {classification.get('category')} "
            f"({float(classification.get('confidence') or 0) * 100:.0f}%)"
        )

    def _copy_report(self) -> None:
        if not self._report_text:
            show_info(self.root, "No report", "Run something first.")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._report_text)
            self._log("Report copied to clipboard", level="info")
        except Exception as exc:
            show_error(self.root, "Clipboard error", str(exc))

    def _open_bundle(self) -> None:
        if not self._last_run:
            show_info(self.root, "No bundle", "Run something first.")
            return
        bundle = Path(str(self._last_run.get("bundle") or ""))
        if not bundle.is_dir():
            show_error(self.root, "Bundle missing", str(bundle))
            return
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(bundle)])
            elif os.name == "nt":
                os.startfile(str(bundle))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(
                    ["xdg-open", str(bundle)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._log(f"Opened bundle {bundle}", level="info")
        except BaseException as exc:  # noqa: BLE001
            show_error(self.root, "Could not open bundle", str(exc))

    # -- shutdown --------------------------------------------------------

    def _log(self, message: str, *, level: str = "info") -> None:
        try:
            self.log.log(message, level=level)
        except Exception:
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._monitor_stop.set()
        self.hooks.run()
        with contextlib.suppress(Exception):
            self.root.destroy()

    def _shutdown(self) -> None:
        self._monitor_stop.set()
        with contextlib.suppress(Exception):
            self.catalog.stop()
        self.runner.close(wait=0.2)
        try:
            self.controller.close()
        except BaseException:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hatari Crash Detective")
    parser.add_argument("--state-dir", help="application state directory (default: examples state/crash_detective)")
    parser.add_argument("--games", action="append", default=[], help="extra game corpus root (repeatable)")
    parser.add_argument("--machine", default="st", choices=["st", "ste", "tt", "falcon"])
    parser.add_argument("--tos", help="TOS/EmuTOS image path")
    parser.add_argument("--timeout", type=float, default=45.0, help="boot timeout in seconds")
    parser.add_argument("--no-scan", action="store_true", help="do not scan the catalog (tests/headless)")
    parser.add_argument("--autoclose-ms", type=int, help="destroy the window after N milliseconds")
    args = parser.parse_args(argv)

    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"FAIL: cannot open Tk display: {exc}", file=sys.stderr)
        return 2
    app = CrashDetectiveApp(
        root,
        state_dir=args.state_dir,
        games=args.games,
        machine=args.machine,
        tos=args.tos,
        timeout=args.timeout,
        no_scan=args.no_scan,
        autoclose_ms=args.autoclose_ms,
    )
    root.mainloop()
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
