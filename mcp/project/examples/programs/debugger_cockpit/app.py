"""Tkinter debugger cockpit for the Hatari MCP server."""

from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable

from common.game_runner import TERMINAL_STATES
from common.ui import (
    ImagePanel,
    ShutdownHooks,
    StatusLog,
    TaskRunner,
    ask_yes_no,
    show_error,
    show_info,
)

from . import addresses, commands
from .controller import CockpitController, default_state_dir



class TextPane:
    """Read-only monospaced text pane with scrollbars."""

    def __init__(self, parent: Any, *, height: int = 12, wrap: str = "none"):
        self.frame = ttk.Frame(parent)
        self.text = tk.Text(
            self.frame,
            height=height,
            wrap=wrap,
            state="disabled",
            font="TkFixedFont",
            undo=False,
        )
        self.y_scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.x_scroll = ttk.Scrollbar(self.frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.y_scroll.set, xscrollcommand=self.x_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.y_scroll.grid(row=0, column=1, sticky="ns")
        self.x_scroll.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def set_text(self, text: str) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.text.configure(state="disabled")
            self.text.see("1.0")
        except Exception:
            return

    def append(self, text: str) -> None:
        try:
            self.text.configure(state="normal")
            self.text.insert("end", text)
            self.text.see("end")
            self.text.configure(state="disabled")
        except Exception:
            return

    def get(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except Exception:
            return ""


class CockpitApp:
    def __init__(
        self,
        root: tk.Tk,
        *,
        state_dir: str | Path | None = None,
        auto_poll: bool = True,
        controller: CockpitController | None = None,
    ):
        self.root = root
        self.auto_poll = auto_poll
        self.poll_ms = 1000
        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.state_dir = (
            Path(state_dir).expanduser().resolve() if state_dir else default_state_dir()
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.controller = controller or CockpitController(
            state_dir=self.state_dir, event_sink=self._enqueue_event
        )
        self.runner = TaskRunner(root)
        self.hooks = ShutdownHooks()
        self.hooks.add(self._shutdown_resources)
        self._closing = False
        self._busy = 0
        self._last_state = "unknown"
        self._last_exception_signature: str | None = None
        self._auto_triage_on_stop = False
        self._poll_job: str | None = None
        self._event_job: str | None = None
        self._regs: dict[str, Any] = {}
        self._screenshot_photo: Any = None

        self.connection_var = tk.StringVar(value="MCP disconnected")
        self.state_var = tk.StringVar(value="state: unknown")
        self.prompt_var = tk.StringVar(value="prompt: no")
        self.pc_var = tk.StringVar(value="PC: $--------")
        self.exception_var = tk.StringVar(value="exception: none")
        self.busy_var = tk.StringVar(value="idle")
        self.program_var = tk.StringVar(value="program: (none)")
        self.bundle_var = tk.StringVar(value="bundle: (none)")

        self.address_var = tk.StringVar()
        self.length_var = tk.StringVar(value="64")
        self.reg_name_var = tk.StringVar(value="d0")
        self.reg_value_var = tk.StringVar(value="0")
        self.bp_address_var = tk.StringVar()
        self.bp_type_var = tk.StringVar(value="exec")
        self.bp_condition_var = tk.StringVar()
        self.bp_once_var = tk.BooleanVar(value=True)
        self.bp_delete_var = tk.StringVar()
        self.mem_address_var = tk.StringVar()
        self.mem_length_var = tk.StringVar(value="64")
        self.mem_data_var = tk.StringVar()
        self.command_var = tk.StringVar()

        root.title("Hatari Debugger Cockpit")
        root.geometry("1440x920")
        root.minsize(1000, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_menu()
        self._build_status()
        self._build_toolbar()
        self._build_panes()
        self._build_bottom()
        self._bind_shortcuts()
        self._log("Debugger cockpit ready. Start/Attach EmuTOS or load a PRG.")
        self._log(
            "Raw console is limited to read-only debugger verbs; every MCP call is logged here."
        )
        self._drain_events()
        if self.auto_poll:
            self._schedule_poll()

    # -- construction --------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Load PRG/TOS...", command=self._cmd_load_prg)
        file_menu.add_command(label="Load bundled sample", command=self._cmd_load_sample)
        file_menu.add_command(label="Build sample with AHCC", command=self._cmd_build_sample)
        file_menu.add_separator()
        file_menu.add_command(label="Save state...", command=self._cmd_save_state)
        file_menu.add_command(label="Load state...", command=self._cmd_load_state)
        file_menu.add_command(label="Screenshot", command=self._cmd_screenshot)
        file_menu.add_separator()
        file_menu.add_command(label="Capture triage bundle", command=self._cmd_capture_triage)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.close)
        menubar.add_cascade(label="File", menu=file_menu)

        emu_menu = tk.Menu(menubar, tearoff=False)
        emu_menu.add_command(label="Start / Attach EmuTOS", command=self._cmd_start_attach)
        emu_menu.add_command(label="Enter debugger", command=self._cmd_enter_debugger)
        emu_menu.add_command(label="Stop emulator", command=self._cmd_stop)
        menubar.add_cascade(label="Emulator", menu=emu_menu)

        debug_menu = tk.Menu(menubar, tearoff=False)
        debug_menu.add_command(label="Continue            F5", command=self._cmd_continue)
        debug_menu.add_command(label="Step                F10", command=self._cmd_step)
        debug_menu.add_command(label="Step over           F11", command=self._cmd_step_over)
        debug_menu.add_command(label="Set breakpoint      Ctrl+B", command=self._cmd_set_breakpoint)
        debug_menu.add_command(label="Run to address", command=self._cmd_run_to)
        debug_menu.add_command(label="Refresh panes", command=self._cmd_refresh)
        menubar.add_cascade(label="Debug", menu=debug_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Keyboard shortcuts", command=self._show_shortcuts)
        help_menu.add_command(label="Safe console commands", command=self._show_console_help)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_status(self) -> None:
        frame = ttk.Frame(self.root, padding=(6, 4))
        frame.grid(row=0, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self.connection_var, relief="sunken", padding=3).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(frame, textvariable=self.state_var, relief="sunken", padding=3).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        ttk.Label(frame, textvariable=self.prompt_var, relief="sunken", padding=3).grid(
            row=0, column=2, sticky="w", padx=(6, 0)
        )
        ttk.Label(frame, textvariable=self.pc_var, relief="sunken", padding=3).grid(
            row=0, column=3, sticky="w", padx=(6, 0)
        )
        ttk.Label(frame, textvariable=self.exception_var, relief="sunken", padding=3).grid(
            row=0, column=4, sticky="w", padx=(6, 0)
        )
        ttk.Label(frame, textvariable=self.program_var, relief="sunken", padding=3).grid(
            row=0, column=5, sticky="w", padx=(6, 0)
        )
        ttk.Label(frame, textvariable=self.busy_var, relief="sunken", padding=3).grid(
            row=0, column=6, sticky="e"
        )
        frame.columnconfigure(5, weight=1)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(6, 2))
        bar.grid(row=1, column=0, sticky="ew")
        for label, command in (
            ("Start / Attach EmuTOS", self._cmd_start_attach),
            ("Load PRG...", self._cmd_load_prg),
            ("Load sample", self._cmd_load_sample),
            ("Build sample", self._cmd_build_sample),
            ("Enter debugger", self._cmd_enter_debugger),
            ("Stop", self._cmd_stop),
        ):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        for label, command in (
            ("Save state", self._cmd_save_state),
            ("Load state", self._cmd_load_state),
            ("Screenshot", self._cmd_screenshot),
            ("Capture triage", self._cmd_capture_triage),
        ):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=2)

        bar2 = ttk.Frame(self.root, padding=(6, 2))
        bar2.grid(row=2, column=0, sticky="ew")
        ttk.Label(bar2, text="Address").pack(side="left")
        ttk.Entry(bar2, textvariable=self.address_var, width=12).pack(side="left", padx=(3, 6))
        for label, command in (
            ("Set BP", self._cmd_set_breakpoint),
            ("List BP", self._cmd_list_breakpoints),
            ("Delete BP", self._cmd_delete_breakpoint),
            ("Run to", self._cmd_run_to),
        ):
            ttk.Button(bar2, text=label, command=command).pack(side="left", padx=2)
        ttk.Separator(bar2, orient="vertical").pack(side="left", fill="y", padx=6)
        for label, command in (
            ("Continue F5", self._cmd_continue),
            ("Step F10", self._cmd_step),
            ("Step over F11", self._cmd_step_over),
            ("Refresh", self._cmd_refresh),
        ):
            ttk.Button(bar2, text=label, command=command).pack(side="left", padx=2)

    def _build_panes(self) -> None:
        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.grid(row=3, column=0, sticky="nsew", padx=6, pady=4)
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        left = ttk.Frame(main)
        center = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=2)
        main.add(center, weight=3)
        main.add(right, weight=3)
        self._build_registers(left)
        self._build_center(center)
        self._build_right(right)

    def _build_registers(self, parent: Any) -> None:
        frame = ttk.LabelFrame(parent, text="Registers")
        frame.pack(fill="both", expand=True, padx=(0, 4))
        columns = ("reg", "value")
        self.register_tree = ttk.Treeview(
            frame, columns=columns, show="headings", height=12, selectmode="browse"
        )
        self.register_tree.heading("reg", text="Register")
        self.register_tree.heading("value", text="Value")
        self.register_tree.column("reg", width=90, anchor="w", stretch=False)
        self.register_tree.column("value", width=130, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.register_tree.yview)
        self.register_tree.configure(yscrollcommand=scroll.set)
        self.register_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.register_tree.bind("<<TreeviewSelect>>", self._on_register_select)

        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(controls, text="Reg").pack(side="left")
        ttk.Entry(controls, textvariable=self.reg_name_var, width=6).pack(side="left", padx=2)
        ttk.Label(controls, text="Value").pack(side="left")
        ttk.Entry(controls, textvariable=self.reg_value_var, width=12).pack(side="left", padx=2)
        ttk.Button(controls, text="Set", command=self._cmd_write_register).pack(side="left", padx=2)
        ttk.Button(controls, text="Refresh", command=self._cmd_refresh).pack(side="left", padx=2)

    def _build_center(self, parent: Any) -> None:
        panes = ttk.Panedwindow(parent, orient="vertical")
        panes.pack(fill="both", expand=True)

        dis_frame = ttk.LabelFrame(panes, text="Disassembly around PC")
        self.disasm_pane = TextPane(dis_frame, height=12)
        self.disasm_pane.frame.pack(fill="both", expand=True)
        panes.add(dis_frame, weight=3)

        stack_frame = ttk.LabelFrame(panes, text="Stack / Backtrace (best effort)")
        self.stack_pane = TextPane(stack_frame, height=10)
        self.stack_pane.frame.pack(fill="both", expand=True)
        panes.add(stack_frame, weight=2)

    def _build_right(self, parent: Any) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True, padx=(4, 0))

        bp_tab = ttk.Frame(notebook, padding=4)
        notebook.add(bp_tab, text="Breakpoints")
        self._build_breakpoints(bp_tab)

        mem_tab = ttk.Frame(notebook, padding=4)
        notebook.add(mem_tab, text="Memory")
        self._build_memory(mem_tab)

        triage_tab = ttk.Frame(notebook, padding=4)
        notebook.add(triage_tab, text="Triage")
        self._build_triage(triage_tab)

        screen_tab = ttk.Frame(notebook, padding=4)
        notebook.add(screen_tab, text="Screen")
        self.image_panel = ImagePanel(screen_tab)
        self.image_panel.frame.pack(fill="both", expand=True)
        ttk.Button(screen_tab, text="Capture screenshot", command=self._cmd_screenshot).pack(
            pady=(4, 0)
        )

    def _build_breakpoints(self, parent: Any) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="Address").pack(side="left")
        ttk.Entry(row, textvariable=self.bp_address_var, width=12).pack(side="left", padx=2)
        ttk.Label(row, text="Type").pack(side="left", padx=(6, 0))
        ttk.Combobox(
            row,
            textvariable=self.bp_type_var,
            values=("exec", "mem", "reg"),
            width=6,
            state="readonly",
        ).pack(side="left", padx=2)
        ttk.Checkbutton(row, text="once", variable=self.bp_once_var).pack(side="left", padx=4)

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(3, 0))
        ttk.Label(row2, text="Condition").pack(side="left")
        ttk.Entry(row2, textvariable=self.bp_condition_var).pack(
            side="left", fill="x", expand=True, padx=2
        )

        row3 = ttk.Frame(parent)
        row3.pack(fill="x", pady=(3, 0))
        ttk.Button(row3, text="Set", command=self._cmd_set_breakpoint).pack(side="left", padx=2)
        ttk.Button(row3, text="List", command=self._cmd_list_breakpoints).pack(side="left", padx=2)
        ttk.Label(row3, text="Delete #").pack(side="left", padx=(8, 0))
        ttk.Entry(row3, textvariable=self.bp_delete_var, width=5).pack(side="left", padx=2)
        ttk.Button(row3, text="Delete", command=self._cmd_delete_breakpoint).pack(side="left", padx=2)
        ttk.Button(row3, text="Delete all", command=lambda: self._cmd_delete_breakpoint(all_items=True)).pack(
            side="left", padx=2
        )

        columns = ("id", "text")
        self.bp_tree = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        self.bp_tree.heading("id", text="#")
        self.bp_tree.heading("text", text="Breakpoint")
        self.bp_tree.column("id", width=40, anchor="center", stretch=False)
        self.bp_tree.column("text", width=420, anchor="w")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.bp_tree.yview)
        self.bp_tree.configure(yscrollcommand=scroll.set)
        self.bp_tree.pack(side="left", fill="both", expand=True, pady=(4, 0))
        scroll.pack(side="right", fill="y", pady=(4, 0))

    def _build_memory(self, parent: Any) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="Address").pack(side="left")
        ttk.Entry(row, textvariable=self.mem_address_var, width=12).pack(side="left", padx=2)
        ttk.Label(row, text="Length").pack(side="left", padx=(6, 0))
        ttk.Entry(row, textvariable=self.mem_length_var, width=7).pack(side="left", padx=2)
        ttk.Button(row, text="Read", command=self._cmd_read_memory).pack(side="left", padx=2)

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(3, 0))
        ttk.Label(row2, text="Hex data").pack(side="left")
        ttk.Entry(row2, textvariable=self.mem_data_var).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(row2, text="Write", command=self._cmd_write_memory).pack(side="left", padx=2)

        self.mem_pane = TextPane(parent, height=18)
        self.mem_pane.frame.pack(fill="both", expand=True, pady=(4, 0))

    def _build_triage(self, parent: Any) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Button(row, text="Capture triage bundle", command=self._cmd_capture_triage).pack(
            side="left", padx=2
        )
        ttk.Label(row, textvariable=self.bundle_var).pack(side="left", padx=6)
        self.triage_pane = TextPane(parent, height=18, wrap="word")
        self.triage_pane.frame.pack(fill="both", expand=True, pady=(4, 0))

    def _build_bottom(self) -> None:
        frame = ttk.Frame(self.root, padding=(6, 2))
        frame.grid(row=4, column=0, sticky="nsew")
        self.root.rowconfigure(4, weight=0)

        notebook = ttk.Notebook(frame, height=10)
        notebook.grid(row=0, column=0, columnspan=4, sticky="nsew")
        self.mcp_log = StatusLog(notebook, height=8)
        notebook.add(self.mcp_log.frame, text="MCP calls / app log")
        self.console_pane = TextPane(notebook, height=8)
        notebook.add(self.console_pane.frame, text="Guest console")
        frame.columnconfigure(0, weight=1)

        row = ttk.Frame(frame)
        row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(3, 0))
        ttk.Label(row, text="dbg_command").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.command_var)
        entry.pack(side="left", fill="x", expand=True, padx=3)
        entry.bind("<Return>", lambda _event: self._cmd_console_command())
        ttk.Button(row, text="Run safe command", command=self._cmd_console_command).pack(side="left", padx=2)
        ttk.Button(row, text="Refresh console", command=self._cmd_refresh_console).pack(side="left", padx=2)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<F5>", lambda _event: self._cmd_continue())
        self.root.bind("<F10>", lambda _event: self._cmd_step())
        self.root.bind("<F11>", lambda _event: self._cmd_step_over())
        self.root.bind("<Control-b>", lambda _event: self._cmd_set_breakpoint())

    # -- logging and status -------------------------------------------

    def _log(self, message: str, level: str = "info") -> None:
        try:
            self.mcp_log.log(message, level=level)
        except Exception:
            return

    def _enqueue_event(self, kind: str, text: str) -> None:
        self.event_queue.put((kind, text))

    def _drain_events(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, text = self.event_queue.get_nowait()
            except queue.Empty:
                break
            level = {"error": "error", "warn": "warn", "result": "result"}.get(kind, "info")
            self._log(f"{kind.upper()}: {text}", level=level)
        try:
            self._event_job = self.root.after(80, self._drain_events)
        except Exception:
            self._event_job = None

    def _set_busy(self, label: str | None) -> None:
        self.busy_var.set(label or "idle")

    def _schedule_poll(self) -> None:
        if not self.auto_poll or self._closing:
            return
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
        self._poll_job = self.root.after(self.poll_ms, self._poll_status)

    def _submit(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        show_dialog: bool = True,
    ) -> bool:
        if self._closing:
            return False
        if self._busy:
            self._log(f"Busy ({self.busy_var.get()}); ignoring '{label}'.", level="warn")
            return False
        self._busy = 1
        self._set_busy(f"busy: {label}")

        def done(result: Any) -> None:
            self._busy = 0
            self._set_busy(None)
            if on_success is not None:
                on_success(result)
            self._schedule_poll()

        def failed(exc: BaseException) -> None:
            self._busy = 0
            self._set_busy(None)
            self._log(f"{label} failed: {exc}", level="error")
            if on_error is not None:
                on_error(exc)
            elif show_dialog:
                show_error(self.root, f"{label} failed", str(exc))
            self._schedule_poll()

        try:
            self.runner.submit(fn, on_success=done, on_error=failed, name=f"cockpit-{label}")
        except RuntimeError as exc:
            self._busy = 0
            self._set_busy(None)
            self._log(f"Could not start {label}: {exc}", level="error")
            return False
        return True

    def _poll_status(self) -> None:
        self._poll_job = None
        if self._closing or not self.auto_poll:
            return
        if self._busy or not self.controller.connected:
            self._schedule_poll()
            return

        def work() -> dict[str, Any]:
            return self.controller.status()

        def done(status: dict[str, Any]) -> None:
            previous = self._last_state
            self._apply_status(status)
            state = str(status.get("state"))
            exception = status.get("exception")
            signature = None
            if exception:
                signature = str(exception)
            if status.get("at_prompt") and previous != "at_prompt":
                if exception:
                    reason = "exception: " + (
                        str(exception.get("type") if isinstance(exception, dict) else exception)
                    )
                elif self._auto_triage_on_stop:
                    reason = "stopped at debugger prompt after continue/run-to"
                else:
                    reason = None
                self._auto_triage_on_stop = False
                self._refresh_all(auto_reason=reason)
            elif exception and signature != self._last_exception_signature:
                self._last_exception_signature = signature
                self._refresh_all(auto_reason="exception: " + str(exception))
            elif previous not in TERMINAL_STATES and state in TERMINAL_STATES:
                self._refresh_all()

        def failed(exc: BaseException) -> None:
            self._log(f"status poll failed: {exc}", level="warn")

        self._submit("status", work, on_success=done, on_error=failed, show_dialog=False)

    def _apply_status(self, status: dict[str, Any]) -> None:
        state = str(status.get("state", "unknown"))
        self._last_state = state
        self.connection_var.set("MCP connected" if self.controller.connected else "MCP disconnected")
        self.state_var.set(f"state: {state}")
        self.prompt_var.set(f"prompt: {'yes' if status.get('at_prompt') else 'no'}")
        exception = status.get("exception")
        if exception:
            text = exception.get("line") if isinstance(exception, dict) else str(exception)
            self.exception_var.set(f"exception: {exception.get('type') if isinstance(exception, dict) else text}")
        else:
            self.exception_var.set("exception: none")
        if status.get("program"):
            self.program_var.set(f"program: {status.get('program')}")
        if not self.controller.connected:
            self.connection_var.set("MCP disconnected")

    def _apply_context(self, context: dict[str, Any]) -> None:
        status = context.get("status") or {}
        if status:
            self._apply_status(status)
        registers = context.get("registers") or {}
        if registers:
            self._regs = registers
            self._render_registers(registers)
            pc = registers.get("pc")
            if isinstance(pc, int):
                self.pc_var.set(f"PC: {addresses.format_address(pc)}")
        self.disasm_pane.set_text(
            "\n".join(str(line) for line in context.get("disassembly") or [])
            or "(no disassembly: the emulator is not stopped at the debugger prompt)"
        )
        self.stack_pane.set_text(self._format_stack(context))
        console = context.get("console_tail") or []
        if console:
            self.console_pane.set_text("\n".join(str(line) for line in console[-200:]))
        for error in context.get("errors") or []:
            self._log(f"evidence warning: {error}", level="warn")

    def _format_stack(self, context: dict[str, Any]) -> str:
        lines: list[str] = []
        stack = context.get("stack")
        if isinstance(stack, dict):
            address = stack.get("address")
            words = stack.get("words") or []
            if isinstance(address, int):
                lines.append(f"Stack words at {addresses.format_address(address)}")
                lines.append(addresses.format_word_dump(words, base_address=address))
            else:
                lines.append("Stack words")
                lines.append(addresses.format_word_dump(words))
        else:
            lines.append("(no stack dump)")
        backtrace = context.get("backtrace")
        frames = backtrace.get("frames") if isinstance(backtrace, dict) else None
        lines.append("")
        if frames:
            lines.append("A6 frame-pointer backtrace (best effort)")
            for frame in frames:
                pc = frame.get("pc")
                text = frame.get("text") or frame.get("symbol") or ""
                if isinstance(pc, int):
                    lines.append(
                        f"#{frame.get('index', '?')}  {addresses.format_address(pc)}  {text}"
                    )
                else:
                    lines.append(f"#{frame.get('index', '?')}  {text}")
        else:
            lines.append("(no A6 frame-pointer frames recovered)")
        return "\n".join(lines)

    def _render_registers(self, registers: dict[str, Any]) -> None:
        order = [f"d{i}" for i in range(8)] + [f"a{i}" for i in range(8)] + [
            "pc",
            "sr",
            "usp",
            "isp",
        ]
        seen: set[str] = set()
        for name in order:
            if name not in registers:
                continue
            seen.add(name)
            value = registers[name]
            text = (
                addresses.format_value(value, width=4)
                if name == "sr"
                else addresses.format_value(value)
            )
            if self.register_tree.exists(name):
                self.register_tree.item(name, values=(name.upper(), text))
            else:
                self.register_tree.insert("", "end", iid=name, values=(name.upper(), text))
        for name in sorted(registers):
            if name in seen:
                continue
            if self.register_tree.exists(name):
                self.register_tree.item(name, values=(name.upper(), addresses.format_value(registers[name])))
            else:
                self.register_tree.insert(
                    "", "end", iid=name, values=(name.upper(), addresses.format_value(registers[name]))
                )
        pc = registers.get("pc")
        if isinstance(pc, int):
            self.pc_var.set(f"PC: {addresses.format_address(pc)}")

    def _on_register_select(self, _event: Any) -> None:
        selection = self.register_tree.selection()
        if not selection:
            return
        name = selection[0]
        self.reg_name_var.set(name.upper())
        value = self._regs.get(name)
        if isinstance(value, int):
            self.reg_value_var.set(addresses.format_value(value))
        elif value is not None:
            self.reg_value_var.set(str(value))

    def _show_triage(self, bundle: dict[str, Any]) -> None:
        report = str(bundle.get("report") or bundle.get("diagnosis") or "")
        path = str(bundle.get("path") or "")
        self.triage_pane.set_text(report)
        self.bundle_var.set(f"bundle: {path}" if path else "bundle: (none)")
        if path:
            self._log(f"Triage bundle written: {path}", level="result")

    # -- generic task wrappers ----------------------------------------

    def _operation(
        self,
        label: str,
        fn: Callable[[CockpitController], Any],
        *,
        refresh: bool = True,
        triage_on_stop: bool = False,
        triage_on_exception: bool = True,
        on_done: Callable[[Any], None] | None = None,
        show_dialog: bool = True,
    ) -> None:
        def work() -> dict[str, Any]:
            result = fn(self.controller)
            status: dict[str, Any] | None = None
            context: dict[str, Any] | None = None
            bundle: dict[str, Any] | None = None
            try:
                status = self.controller.status()
            except BaseException as exc:
                self.controller.emit("error", f"status after {label} failed: {exc}")
            if status is not None:
                stopped = bool(status.get("at_prompt"))
                exception = status.get("exception")
                if refresh and (stopped or exception):
                    context = self.controller.read_context()
                if stopped and (
                    triage_on_stop or (triage_on_exception and bool(exception))
                ):
                    reason = (
                        "exception: "
                        + str(exception.get("type") if isinstance(exception, dict) else exception)
                        if exception
                        else "stopped at debugger prompt"
                    )
                    bundle = self.controller.capture_triage(reason)
            return {
                "result": result,
                "status": status,
                "context": context,
                "bundle": bundle,
            }

        def done(payload: dict[str, Any]) -> None:
            status = payload.get("status")
            if status:
                self._apply_status(status)
            context = payload.get("context")
            if context:
                self._apply_context(context)
            bundle = payload.get("bundle")
            if bundle:
                self._show_triage(bundle)
            if on_done is not None:
                on_done(payload.get("result"))
            if status and (
                status.get("at_prompt") or str(status.get("state")) in TERMINAL_STATES
            ):
                self._auto_triage_on_stop = False

        self._submit(label, work, on_success=done, show_dialog=show_dialog)

    def _refresh_all(self, *, auto_reason: str | None = None) -> None:
        if self._busy:
            self._schedule_poll()
            return

        def work() -> dict[str, Any]:
            context = self.controller.read_context()
            bundle = self.controller.capture_triage(auto_reason) if auto_reason else None
            return {"context": context, "bundle": bundle}

        def done(payload: dict[str, Any]) -> None:
            self._apply_context(payload["context"])
            if payload.get("bundle"):
                self._show_triage(payload["bundle"])

        self._submit("refresh", work, on_success=done, show_dialog=False)

    # -- file / emulator commands -------------------------------------

    def _cmd_start_attach(self) -> None:
        self._auto_triage_on_stop = False
        self._operation("start/attach", lambda controller: controller.attach_or_start())

    def _cmd_load_prg(self) -> None:
        initial = Path(__file__).resolve().with_name("sample")
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Load PRG or TOS",
            initialdir=str(initial),
            filetypes=(("Atari programs", "*.prg *.PRG *.tos *.TOS *.ttp *.TTP"), ("All files", "*")),
        )
        if not chosen:
            return
        self._load_prg_path(Path(chosen))

    def _load_prg_path(self, path: Path) -> None:
        self._auto_triage_on_stop = False
        self.program_var.set(f"program: {path}")

        def done(_result: Any) -> None:
            self._log(f"Loaded {path}")
            self._refresh_all()

        self._operation(
            "load PRG", lambda controller: controller.load_prg(path), on_done=done
        )

    def _cmd_load_sample(self) -> None:
        output = self.controller.sample_output
        if not output.is_file():
            show_info(
                self.root,
                "Sample not built",
                "The bundled sample is not built yet. Building it with the MCP AHCC tool now.",
            )
            self._cmd_build_sample()
            return
        self._load_prg_path(output)

    def _cmd_build_sample(self) -> None:
        def done(result: Any) -> None:
            output = result.get("output") if isinstance(result, dict) else None
            fallback = result.get("fallback") if isinstance(result, dict) else None
            if output:
                self._log(f"Sample built: {output}")
                self.program_var.set(f"program: {output}")
            if fallback:
                self._log(f"Build used the local ahcc-cli.py fallback: {fallback}", level="warn")
            show_info(self.root, "Sample build", f"Built sample target:\n{output or '(unknown)'}")

        self._operation(
            "build sample",
            lambda controller: controller.build_sample(),
            refresh=False,
            on_done=done,
        )

    def _cmd_enter_debugger(self) -> None:
        self._auto_triage_on_stop = False
        self._operation(
            "enter debugger",
            lambda controller: controller.enter_debugger(),
            triage_on_stop=False,
        )

    def _cmd_stop(self) -> None:
        self._auto_triage_on_stop = False
        self._operation("stop emulator", lambda controller: controller.stop_emulator())

    # -- execution commands -------------------------------------------

    def _cmd_continue(self) -> None:
        self._auto_triage_on_stop = True
        self._operation(
            "continue",
            lambda controller: controller.continue_run(),
            refresh=False,
            triage_on_stop=False,
        )

    def _cmd_step(self) -> None:
        self._operation("step", lambda controller: controller.step())

    def _cmd_step_over(self) -> None:
        self._operation("step over", lambda controller: controller.step_over())

    def _cmd_run_to(self) -> None:
        try:
            target = addresses.parse_address(self.address_var.get())
        except addresses.AddressError as exc:
            show_error(self.root, "Run to", str(exc))
            return
        self._auto_triage_on_stop = True
        self._operation(
            "run to",
            lambda controller: controller.run_to(target),
            triage_on_stop=True,
        )

    # -- breakpoints ---------------------------------------------------

    def _breakpoint_address(self) -> int | None:
        text = self.bp_address_var.get().strip() or self.address_var.get().strip()
        if text:
            try:
                return addresses.parse_address(text)
            except addresses.AddressError as exc:
                show_error(self.root, "Breakpoint address", str(exc))
                return None
        pc = self._regs.get("pc")
        if isinstance(pc, int):
            return pc
        show_error(self.root, "Breakpoint address", "Enter an address or read registers first.")
        return None

    def _cmd_set_breakpoint(self) -> None:
        address = self._breakpoint_address()
        if address is None:
            return
        condition = self.bp_condition_var.get().strip() or None
        type_name = self.bp_type_var.get().strip() or "exec"
        once = bool(self.bp_once_var.get())
        if type_name != "exec" and not condition:
            show_error(
                self.root,
                "Breakpoint condition",
                "mem/reg breakpoints need an explicit Hatari condition.",
            )
            return
        self.bp_address_var.set(addresses.format_address(address))
        self.address_var.set(addresses.format_address(address))
        self._operation(
            "set breakpoint",
            lambda controller: controller.set_breakpoint(
                address,
                type=type_name,
                condition=condition,
                once=once,
                quiet=False,
            ),
            on_done=lambda _result: self._refresh_breakpoints(),
        )

    def _refresh_breakpoints(self) -> None:
        def work() -> dict[str, Any]:
            return self.controller.list_breakpoints()

        def done(result: dict[str, Any]) -> None:
            self._update_breakpoints(result.get("entries") or [])
            raw = str(result.get("raw") or "").strip()
            if raw:
                self._log("Breakpoints:\n" + raw)

        self._submit("list breakpoints", work, on_success=done, show_dialog=False)

    def _cmd_list_breakpoints(self) -> None:
        self._refresh_breakpoints()

    def _cmd_delete_breakpoint(self, *, all_items: bool = False) -> None:
        if all_items:
            if not ask_yes_no(self.root, "Delete breakpoints", "Delete all breakpoints?"):
                return
            index = 0
        else:
            text = self.bp_delete_var.get().strip()
            if not text:
                selection = self.bp_tree.selection()
                if selection:
                    value = self.bp_tree.item(selection[0], "values")
                    text = str(value[0]) if value else ""
            if not text:
                show_error(self.root, "Delete breakpoint", "Enter a breakpoint number or select one.")
                return
            try:
                index = int(text, 10)
            except ValueError:
                show_error(self.root, "Delete breakpoint", f"Not a breakpoint number: {text!r}")
                return
        self._operation(
            "delete breakpoint",
            lambda controller: controller.delete_breakpoint(index),
            on_done=lambda _result: self._refresh_breakpoints(),
        )

    def _update_breakpoints(self, entries: list[dict[str, Any]]) -> None:
        for item in self.bp_tree.get_children():
            self.bp_tree.delete(item)
        for entry in entries:
            identifier = entry.get("id")
            self.bp_tree.insert(
                "",
                "end",
                values=("" if identifier is None else identifier, entry.get("text", "")),
            )

    # -- memory and registers -----------------------------------------

    def _cmd_read_memory(self) -> None:
        try:
            address = addresses.parse_address(self.mem_address_var.get())
        except addresses.AddressError as exc:
            show_error(self.root, "Memory address", str(exc))
            return
        try:
            length = addresses.parse_count(self.mem_length_var.get(), maximum=65536)
        except addresses.AddressError as exc:
            show_error(self.root, "Memory length", str(exc))
            return
        self._read_memory(address, length)

    def _read_memory(self, address: int, length: int) -> None:
        def done(result: dict[str, Any]) -> None:
            try:
                payload = bytes.fromhex(str(result.get("hex") or ""))
            except ValueError as exc:
                self._log(f"memory result was not valid hex: {exc}", level="error")
                return
            self.mem_pane.set_text(addresses.format_hex_dump(payload, address))
            self._log(f"Read {len(payload)} bytes at {addresses.format_address(address)}")

        self._operation(
            "read memory",
            lambda controller: controller.read_memory(address, length),
            refresh=False,
            on_done=done,
        )

    def _cmd_write_memory(self) -> None:
        try:
            address = addresses.parse_address(self.mem_address_var.get())
        except addresses.AddressError as exc:
            show_error(self.root, "Memory address", str(exc))
            return
        try:
            data = addresses.normalize_hex_data(self.mem_data_var.get())
        except addresses.AddressError as exc:
            show_error(self.root, "Memory data", str(exc))
            return
        length = len(data) // 2

        def work() -> dict[str, Any]:
            self.controller.write_memory(address, data)
            return self.controller.read_memory(address, max(1, length))

        def done(result: dict[str, Any]) -> None:
            payload = bytes.fromhex(str(result.get("hex") or ""))
            self.mem_pane.set_text(addresses.format_hex_dump(payload, address))
            self._log(
                f"Wrote {length} byte(s) at {addresses.format_address(address)}; read back {len(payload)}"
            )

        self._operation("write memory", work, refresh=False, on_done=done)

    def _cmd_write_register(self) -> None:
        name = self.reg_name_var.get().strip()
        if not name:
            show_error(self.root, "Register", "Enter a register name (d0-d7/a0-a7/pc/sr).")
            return
        try:
            value = addresses.parse_value(self.reg_value_var.get())
        except addresses.AddressError as exc:
            show_error(self.root, "Register value", str(exc))
            return
        self._operation(
            "write register",
            lambda controller: controller.write_register(name, value),
            on_done=lambda _result: self._refresh_all(),
        )

    # -- state, screenshot, console -----------------------------------

    def _cmd_save_state(self) -> None:
        default = self.state_dir / "snapshots" / "cockpit-state.snap"
        default.parent.mkdir(parents=True, exist_ok=True)
        chosen = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save emulator state",
            initialdir=str(default.parent),
            initialfile=default.name,
            defaultextension=".snap",
            filetypes=(("Hatari state", "*.snap"), ("All files", "*")),
        )
        if not chosen:
            return
        self._operation(
            "save state",
            lambda controller: controller.save_state(chosen),
            refresh=False,
            on_done=lambda _result: self._log(f"State saved: {chosen}"),
        )

    def _cmd_load_state(self) -> None:
        initial = self.state_dir / "snapshots"
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Load emulator state",
            initialdir=str(initial),
            filetypes=(("Hatari state", "*.snap"), ("All files", "*")),
        )
        if not chosen:
            return
        self._operation(
            "load state",
            lambda controller: controller.load_state(chosen),
            on_done=lambda _result: self._log(f"State loaded: {chosen}"),
        )

    def _cmd_screenshot(self) -> None:
        target = self.state_dir / "screenshots" / f"screen-{self._timestamp()}.png"

        def done(result: dict[str, Any]) -> None:
            path = Path(str(result.get("path") or target))
            self._show_screenshot(path)
            self._log(f"Screenshot: {path}")

        self._operation(
            "screenshot",
            lambda controller: controller.screenshot(target, format="png"),
            refresh=False,
            on_done=done,
        )

    def _show_screenshot(self, path: Path) -> None:
        try:
            photo = tk.PhotoImage(file=str(path))
            max_width = 640
            if photo.width() > max_width:
                factor = max(1, (photo.width() + max_width - 1) // max_width)
                photo = photo.subsample(factor, factor)
            self._screenshot_photo = photo
            self.image_panel.show_photo(photo)
        except Exception as exc:
            self.image_panel.show_text(f"Screenshot saved:\n{path}\n(preview unavailable: {exc})")

    def _cmd_refresh_console(self) -> None:
        def work() -> list[str]:
            return self.controller.console_tail(200)

        def done(lines: list[str]) -> None:
            self.console_pane.set_text(
                "\n".join(lines[-200:]) if lines else "(guest console has no output yet)"
            )

        self._submit("refresh console", work, on_success=done, show_dialog=False)

    def _cmd_console_command(self) -> None:
        text = self.command_var.get()
        route = commands.route_console_command(text)
        if route.action == "local_help":
            self._log(commands.CONSOLE_HELP)
            self.console_pane.append("\n" + commands.CONSOLE_HELP + "\n")
            return
        if not route.allowed:
            self._log(f"Refused raw command: {route.reason}", level="warn")
            show_error(self.root, "Unsafe console command", route.reason)
            return
        self._log("WARNING: " + route.warning, level="warn")
        self.console_pane.append(f"\n> {route.raw}\n")

        def done(result: dict[str, Any]) -> None:
            raw = str(result.get("raw") or "")
            if raw:
                self.console_pane.append(raw + "\n")
            self.command_var.set("")

        self._operation(
            "dbg_command",
            lambda controller: controller.raw_command(route.raw),
            on_done=done,
        )

    # -- triage --------------------------------------------------------

    def _cmd_capture_triage(self) -> None:
        def done(bundle: dict[str, Any]) -> None:
            self._show_triage(bundle)

        self._operation(
            "capture triage",
            lambda controller: controller.capture_triage("manual capture"),
            refresh=False,
            on_done=done,
        )

    # -- misc ----------------------------------------------------------

    def _timestamp(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")

    def _cmd_refresh(self) -> None:
        self._refresh_all()

    def _show_shortcuts(self) -> None:
        show_info(
            self.root,
            "Keyboard shortcuts",
            "F5  continue\nF10 step\nF11 step over\nCtrl+B set breakpoint\n\n"
            "Refresh on stop is automatic. Evidence bundles go under:\n"
            f"{self.state_dir / 'crash-bundles'}",
        )

    def _show_console_help(self) -> None:
        show_info(self.root, "Safe console commands", commands.CONSOLE_HELP)

    def _shutdown_resources(self) -> None:
        try:
            self.runner.close(wait=1.0)
        except BaseException:
            pass
        try:
            self.controller.close()
        except BaseException:
            pass

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for job in (self._poll_job, self._event_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._poll_job = None
        self._event_job = None
        self.hooks.run()
        try:
            self.root.destroy()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Hatari MCP debugger cockpit")
    parser.add_argument("--state-dir", help="override the cockpit state directory")
    parser.add_argument("--no-poll", action="store_true", help="disable background status polling")
    args = parser.parse_args(argv)
    root = tk.Tk()
    CockpitApp(root, state_dir=args.state_dir, auto_poll=not args.no_poll)
    root.mainloop()
    return 0
