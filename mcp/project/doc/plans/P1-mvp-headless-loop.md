# P1 — MVP: headless compile/debug loop

Status: partial — MVP headless loop implemented and live-verified; CI acceptance and residual stock-Hatari limits remain
Owner: phase implementer (MVP loop)
Last updated: 2026-10-27
Roadmap: P1.1–P1.29, gate in roadmap §5 Phase 1 and §6 MVP acceptance

## Goal

Deliver workflow W1 end to end with only the MCP server and a stock Hatari
build: start a headless ST/STE, expose a host directory as a GEMDOS drive,
autostart a program with symbols, capture console/NatFeats output and an exit
code, stop on a breakpoint, inspect registers/memory/disassembly, step, save and
load state, take screenshots, inject keyboard/mouse input, and compile a C
program with AHCC through the proven CLI mechanism. Add an opt-in, best-effort
AHCC GUI mode without breaking the CLI path or the one-child rule.

## Dependencies

* P0 complete: package, supervisor, socket/stdin channels, state machine,
  demuxer/sentinel, profiles, fixtures, error model, first tools, CI.
* P0.3a decision: the chosen readline/stdin strategy. Every execution-control
  tool depends on it.
* P0 pager guard: the private config must disable paging; otherwise P1 memory/
  disassembly tools are unsafe.
* Local Hatari 2.6.1-devel with `--harddrive`, `--gemdos-*`, `--auto`,
  `--natfeats`, `--debug-except`, `--symload`, `--parse`, and the debugger
  commands used below.
* Local AHCC 6.0 at
  `mcp/project/tools/ahcc/ahcc-68000/AHCCST.BIN/` (`AHCCST_P.TTP` and
  `AHCCST.PRG`) plus `build-install/bin/hatari-prg-args`.
* EmuTOS for CI, optional user TOS for accuracy; optional read-only corpus
  trees (Okami-Shell, FreeMiNT, GodLib) discovered by path and skipped when
  absent.

## Deliverables and file ownership

All server paths are under `mcp/project/src/hatari_mcp/` unless noted. P1 extends
P0 modules rather than forking them.

| Path | Purpose |
| --- | --- |
| `mcp/project/src/hatari_mcp/tools/lifecycle.py` | Add `emu_reset`, `emu_reboot`, `emu_quit_debugger`; extend start args. |
| `mcp/project/src/hatari_mcp/tools/config.py` | `emu_set_option`, `emu_get_config`, `emu_save_config`, `emu_set_path`, `emu_device`, `emu_machine`, `emu_memory_size`. |
| `mcp/project/src/hatari_mcp/config.py` | Restart-required option table and machine/memory validation. |
| `mcp/project/src/hatari_mcp/workspace.py` | Workspace model, GEMDOS roots, drive mapping, artifact directories. |
| `mcp/project/src/hatari_mcp/guestpath.py` | Host↔guest path conversion, case/separator rules, symlink checks. |
| `mcp/project/src/hatari_mcp/run.py` | Autostart assembly, parse-script generation, run-to-exit state, exit ladder. |
| `mcp/project/src/hatari_mcp/natfeats.py` | `NF_STDERR`, `NF_EXIT`, `NatFeats:` line parsing, program exit tracking. |
| `mcp/project/src/hatari_mcp/exception.py` | `--debug-except` mask, exception line parsing, event payloads. |
| `mcp/project/src/hatari_mcp/debug/parse.py` | Shared debugger output parsers and quote/format helpers. |
| `mcp/project/src/hatari_mcp/debug/registers.py` | `cpureg` parse/set builders and field names. |
| `mcp/project/src/hatari_mcp/debug/disasm.py` | Range/count disassembly parsing with symbol annotation. |
| `mcp/project/src/hatari_mcp/debug/symbols.py` | `symbols` load/list/free, PRG base handling, `.sym` parsing. |
| `mcp/project/src/hatari_mcp/debug/breakpoints.py` | Breakpoint/watchpoint set/list/delete, hit and reason parsing. |
| `mcp/project/src/hatari_mcp/debug/memory.py` | `memdump`/`savebin`/`loadbin`/`memwrite`/`find` codecs and chunking. |
| `mcp/project/src/hatari_mcp/debug/evaluate.py` | Expression result parsing. |
| `mcp/project/src/hatari_mcp/tools/run.py` | `emu_wait_for_exit` (new; update tools reference). |
| `mcp/project/src/hatari_mcp/tools/debugger.py` | `dbg_symbols_*`, registers, disassemble, evaluate, status. |
| `mcp/project/src/hatari_mcp/tools/breakpoints.py` | Breakpoint/watchpoint tools and wait API. |
| `mcp/project/src/hatari_mcp/tools/execution.py` | Step, step-over, continue, run-until. |
| `mcp/project/src/hatari_mcp/tools/memory.py` | Memory read/write/find/save/load. |
| `mcp/project/src/hatari_mcp/tools/state.py` | State save/load and annotations. |
| `mcp/project/src/hatari_mcp/tools/screen.py` | `screen_screenshot`, `screen_info`. |
| `mcp/project/src/hatari_mcp/tools/input.py` | Keyboard, text, mouse, joystick. |
| `mcp/project/src/hatari_mcp/tools/ahcc.py` | `ahcc_compile`, `ahcc_gui_start`, `ahcc_gui_status`. |
| `mcp/project/src/hatari_mcp/keymap.py` | ST scancode table, key names, shift/modifier expansion. |
| `mcp/project/src/hatari_mcp/st_scancodes.py` | Generated/constant scancode data with provenance note. |
| `mcp/project/src/hatari_mcp/screen/png.py` | PNG/BMP header parsing and dimensions without a heavy dependency. |
| `mcp/project/src/hatari_mcp/safety.py` | Path confinement, allowlist mode, destructive gating, limits. |
| `mcp/project/src/hatari_mcp/limits.py` | Hard limits shared by tools. |
| `mcp/project/src/hatari_mcp/ahcc/models.py` | `AhccRequest`, `AhccResult`, diagnostics. |
| `mcp/project/src/hatari_mcp/ahcc/project.py` | PRJ generation and staging, preserving `ahcc-cli.py` behavior. |
| `mcp/project/src/hatari_mcp/ahcc/runner.py` | Ephemeral headless Hatari/AHCC runner, socket monitor, timeout, cleanup. |
| `mcp/project/src/hatari_mcp/ahcc/diagnostics.py` | AHCC output classification and structured diagnostics. |
| `mcp/project/src/hatari_mcp/ahcc/cli.py` | Shared command-line `main()` for the standalone script and tests. |
| `mcp/project/scripts/ahcc-cli.py` | Thin wrapper that imports `hatari_mcp.ahcc.cli`; no duplicated compile logic. |
| `mcp/project/tests/unit/` | Parsers, path mapping, input tables, AHCC staging/diagnostics. |
| `mcp/project/tests/integration/` | MVP lifecycle, debugger, memory, screen, input, AHCC. |
| `mcp/project/tests/corpus/` | Recorded debugger/NatFeats/AHCC streams and optional corpus smoke. |
| `mcp/project/doc/tools-reference.md` | Update schemas and new `emu_wait_for_exit`/start args. |
| `mcp/project/doc/architecture.md` | Add the AHCC ephemeral process and GUI mode-switch decision. |

## Design / constraints

### 1. Lifecycle and configuration

* `emu_reset(kind)`: stock debugger `reset` supports `soft` and `hard` only.
  Map `warm` -> `soft` and `cold` -> `hard`; return the native kind used. Do not
  claim four native reset modes. `reset` leaves the debugger; do not queue a
  sentinel.
* `emu_reboot(args?)`: stop + start. State is not preserved unless the caller
  saves it first; return `state_preserved: false`.
* `emu_machine`/`emu_memory_size`/TOS changes are spawn-time in P1. If called
  while running, return `restart_required: true` with the exact `eu_start`
  arguments; do not silently mutate and claim success.
* `emu_set_option(options)`: only runtime-safe options are accepted. Maintain a
  small allowlist/denylist derived from `Change_ApplyCommandline` behavior and
  reject known restart-required options (`--machine`, `--memsize`, `--tos`,
  disk/ACSI/IDE attachment, CPU level) with `restart_required`. Unknown options
  are sent but marked `applied: "unknown"`; errors are inferred from stderr.
* `emu_set_path(name, path)`: only names supported by `control.c`
  (`memauto`, `memsave`, `midiin/out`, `printout`, `soundout`, `rs232in/out`,
  `scca*`, `sccalan*`, `sccb*`). Validate against the configured roots.
* `emu_device(device, action)`: only `printer`, `rs232`, `scca`, `sccalan`,
  `sccb`, `midi`.
* `emu_get_config`: merged launch config plus selected supervisor state; never
  dump secrets or host paths outside configured roots.
* `emu_save_config(path)`: explicit destructive write; path-confined; never
  writes the user's default config unless asked.

### 2. GEMDOS workspace and autostart

A workspace is one host directory, mapped to a guest drive (default `C:`), with
optional read-only additional roots. P1 supports one write root; P3 expands to a
multi-root model.

* `emu_start(workspace=..., gemdos_drive="C", gemdos_case=..., gemdos_time=...,
  gemdos_conv=..., write_protect=..., autostart=..., autostart_args=[...],
  symload=..., natfeats=True, auto_exit_detection=True)`.
* Pass `--harddrive <workspace>`, `--gemdos-drive <letter>`, and the selected
  `--gemdos-case/--gemdos-time/--gemdos-conv` and `--protect-hd` options.
* Guest paths are `<DRIVE>:\<UPPER-SUBDIR>\<UPPER-FILE>`. Convert `/` to `\`,
  apply `--gemdos-case` semantics, and reject names that cannot round-trip.
  Resolve symlinks and require the real path to remain under the workspace; a
  symlink escape is `path_not_allowed`.
* **Autostart:** `--auto` accepts an Atari path, not an arbitrary host path.
  Accept `autostart` as a workspace-relative or absolute host path, verify it is
  inside the workspace, then either:
  1. pass `--auto C:\SUBDIR\FILE.PRG`; or
  2. if the program is at the workspace root and the drive is C, pass
     `--auto FILE.PRG` (Hatari prepends `C:\`).
  Do not pass a host path to `--auto`. A file outside the workspace is
  `guest_path_unmapped` with a hint to copy it into the workspace or add a root
  in P3.
* For program arguments, use the proven `hatari-prg-args` pattern only if needed;
  P1 autostart does not need basepage arguments. P3.2 owns the one-call
  `run_prg` with arguments.
* GEMDOS HD symbols autoload with `--symload exec` by default for an autostarted
  program; `--symload debugger` is an option. Do not claim symbols exist if the
  binary is stripped; return `symbols_loaded: false`.
* Program files, headers, outputs, symbols, screenshots, states, and memory
  dumps all resolve through `safety.py`. Generated artifacts go under
  `<workspace>/.hatari-mcp/{artifacts,state,tmp}` by default.

### 3. Run loop, NatFeats, exceptions

* Enable `--natfeats on`. Parse `NatFeats: exit(N)` on stderr and prefer the
  child process return code when it matches; expose `program_exit_code`,
  `exit_reason` (`natfeats`/`pterm`/`timeout`/`signal`/`unknown`), and
  `crashed`.
* `NF_STDERR` writes raw bytes to stderr without a `NatFeats:` prefix and
  without guaranteeing a newline. Buffer partial data, flush on newline or a
  short idle timeout, and mark such entries `kind=natfeats_stderr` with a
  documented caveat: raw guest output can be indistinguishable from Hatari log
  text. Never silently drop it.
* TOS console: launch with `--conout 2`; read stdout through the demuxer; flatten
  VT-52 control sequences to text; keep a ring buffer with `since` cursors.
  stdout may also contain the readline prompt; the P0.3a decision controls how
  it is separated.
* `emu_wait_for_exit(timeout_ms)` waits for one of:
  1. NatFeats `NF_EXIT` / child exit (preferred);
  2. Pterm/Pterm0 trap (optional auto-exit detection);
  3. timeout.
  Return `{exit_code, exit_reason, output, console, duration_ms, crashed,
  snapshot?}`. P1 does not attempt the full P3.3 ladder; Pterm detection is a
  minimal, optional parse-script path.
* Pterm trap: when `auto_exit_detection=true` and a program was autostarted,
  generate a private `--parse prg:<file>` script similar to `hatari-prg-args`:
  set `b GemdosOpcode = 0x00 :quiet :trace :file <quit-script>` and the same
  for `0x4C`; the quit script runs `quit`. The exit code is unknown (None) and
  `exit_reason="pterm"`. This is opt-in because it changes debugger state.
* Exceptions: launch with an explicit mask (default `bus,address,illegal,
  zerodiv,chk,trapv,privilege,trace,linea,linef`; do not use `all`, which
  includes `autostart` and DSP behavior). Parse lines such as
  `Bus error exception at 0x...!`; transition to `at_prompt`, record
  `exception_type`, `pc`, and emit an `exception` event. `dbg_breakpoint_wait`
  and an exception wait API both observe this event.
* `emu_status` includes `program_exit_code`, `exit_reason`, `exception`,
  `symbols_loaded`, and `at_prompt` state.

### 4. Symbols, registers, disassembly, evaluation

* `dbg_symbols_load(path, name?, prg?, offset?)`:
  * accept ELF/DRI/GST/a.out and nm/gst2ascii `.sym` files supported by
    Hatari's `symbols` command;
  * for a running GEMDOS HD program, `symbols prg` is the primary path; for an
    external file use `symbols "<path>" [TEXT offset]`;
  * `prg`/`offset` sets the text base for ASCII symbol files;
  * return `{loaded, file, count?, symbols?}` and `raw`.
  * `dbg_symbols_list(filter?, kind?)` uses `symbols code|data|name [find]`;
    cap results and return a `truncated` flag.
  * Symbol addresses are addresses, not source lines. State this in the tool
    description.
* `dbg_registers_get(cpu="m68k")`: require `at_prompt` or `remote_paused` for a
  coherent snapshot; if `running` and `allow_running=true`, pause/read/resume
  around the command. Parse `cpureg` into `d0..d7`, `a0..a7`, `pc`, `sr`,
  `usp`, `isp`, plus raw lines. DSP parsing is P4.
* `dbg_registers_set(registers)`: `cpureg <reg>=<value>`; destructive; validate
  register names and widths; chunk multiple registers if the command approaches
  the limit.
* `dbg_disassemble(address, count=10, symbols=True)`: Hatari's `disasm` takes a
  start/end range, not a count. Compute a conservative range
  (`address + max(16*count, 32)`), parse instructions, and truncate to `count`.
  Use `PConSymbol`/loaded symbols for annotation when available. Cap count
  (default 10, max 200) and output.
* `dbg_evaluate(expression)`: `evaluate <expr>`; return `raw`, `value`,
  `hex`, `dec`, and `bin` when parseable. Reject newlines and commands that
  would mutate state; `evaluate` is read-only.

### 5. Breakpoints and execution control

* Breakpoints are Hatari conditional breakpoints: `breakpoint <conditions>
  [:once] [:quiet] [:<count>] [:<print ...>]`. There is no enable/disable; delete
  and re-add. IDs are 1-based positions and shift after deletion.
* `dbg_breakpoint_set(address, type="exec"|"mem"|"reg", condition?, once?,
  count?, print?, cpu?)`:
  * `exec` -> `pc = <addr>` (or `pc = <symbol>`);
  * `mem` -> `(<addr>).b|w|l = ...` or a supplied condition;
  * `reg` -> `<reg> = ...`.
  * Always append `:once` when requested. Return the parsed numeric ID from the
    "condition breakpoint N ... added" output, and re-list to reconcile.
* `dbg_breakpoint_list`: parse the numbered list into
  `{id, condition, hits?, options}`; keep raw.
* `dbg_breakpoint_delete(id|"all")`: `breakpoint <id>` or `breakpoint all`;
  re-list.
* `dbg_watchpoint_set(address, width, mode)`: implemented as a memory
  breakpoint condition; document that stock Hatari has no separate watchpoint
  object and that read watchpoints may be approximated.
* `dbg_breakpoint_wait(timeout_ms)`: wait on the event queue, never on a socket
  reply. Resolve PC/reason when stopped; return `{reason, id?, pc, symbol?,
  exception?}`.
* `dbg_step(count=1)`: only when `at_prompt`. Send `step` on stdin, then an
  `echo` sentinel; repeat up to `count` with a cap. If the state is
  `remote_paused` or `running`, return `not_stopped` with a hint to set a
  breakpoint first. Never send `step` over the socket.
* `dbg_step_over`: same, using `next`.
* `dbg_continue`: if `at_prompt`, send `cont` on stdin with no sentinel and
  transition to `running`; if `remote_paused`, send `hatari-cont`; if already
  running, return idempotent success.
* `dbg_run_until(address, timeout_ms)`: require `at_prompt` or `remote_paused`;
  set a temporary `pc = <address> :once` breakpoint, continue, wait for the
  breakpoint/timeout, remove the temporary breakpoint on timeout, and return
  stop reason.
* `dbg_status`: state, PC, SR, stop reason, last breakpoint/exception, whether
  the socket/prompt is responsive. Use `cpureg` at the prompt; use
  `hatari-debug info`/`cpureg` via socket while running.
* Terminal commands (`cont`, `quit`, `reset`) never get a sentinel; update state
  optimistically and verify via events/status.

### 6. Memory and state

* `memory_read(address, length, format="hex"|"base64", space="physical"|
  "logical")`: cap 32 KiB per call. Prefer `savebin "<tmp>" <addr> <len>` and
  read the host file, because it avoids hex/format ambiguities and paging; fall
  back to `memdump` only if `savebin` is unavailable. Return
  `{address, length, data|hex|base64, sha256, truncated}`.
  * `savebin` writes from the emulator process; use a private, writable temp
    path under the runtime directory, quote it, and delete it after reading.
  * For logical addresses use `memdump s|u`; `savebin` is physical.
* `memory_write(address, data, format)`: `memwrite b|w|l <addr> <values>` in
  chunks that keep the command under 3.5 KiB; destructive. Verify by reading
  back a sample and return `verified`.
* `memory_find(address, length, pattern, type)`: `find b|c|w|l ...`; parse
  matches and the "N matches." line; cap.
* `memory_save_binary(address, length, path)`: `savebin`; path-confined; return
  size and SHA256.
* `memory_load_binary(address, path)`: `loadbin`; path-confined read; destructive.
* `memory_fill`: optional P1 convenience implemented as chunked `memwrite`.
* `state_save(path?)`/`state_load(path?)`: pause or require `at_prompt`/
  `remote_paused`; `statesave`/`stateload`; path-confined; enforce a maximum
  size before/after; warn that snapshots are large and version-sensitive. Do not
  claim atomicity while running.
* `state_annotate(text)`: server-side bookmark only; never changes emulator
  state.

### 7. Screen and input

* `screen_screenshot(path?, format="png")`: use the debugger
  `screenshot "<path>"` command, never the UI shortcut and never
  `--disable-video`. Default path under the artifact directory; PNG is default;
  BMP/NEO/XIMG optional. Return path, bytes, width, height, format, SHA256.
  Region/crop capture is P2.5, not P1.
  * At `at_prompt`, send the command on stdin; while running/paused, send via
    socket. If the command would page, fail.
  * Parse PNG/BMP headers locally for dimensions; do not require Pillow in P1.
* `screen_info`: `hatari-debug info`; parse resolution, monitor, color mode,
  refresh, borders. Return raw on unknown formats.
* `input_key(key, down=true, shift=false)`: accept a single alphanumeric
  character or an ST scancode (0-255) or a name from `keymap.py`. Non-
  alphanumeric characters require a mapped scancode; uppercase/characters that
  need shift produce an explicit shift down/up sequence. Use
  `hatari-event keydown|keyup <char|scancode>`.
* `input_type_text(text, delay_ms=0)`: expand each character to keydown/keyup
  with Shift where needed, preserving newline/Tab/Backspace/Escape mappings.
  Send events in small batches; sleep server-side between keys; never use a
  non-existent `hatari-event text`.
* `input_mouse_move(dx, dy)`: `hatari-event mousemove <dx> <dy>` relative, in
  emulated pixels. `input_mouse_button(button, down)`: left/right down/up.
  `doubleclick` where appropriate. No middle button (ST mice lack one).
* `input_joystick(port, x, y, fire)`: stock Hatari has no direct joystick event.
  Document and expose the keyboard-emulation path (`--joystick <port>`):
  arrow-key scancodes for axes and the right-Ctrl scancode for fire (the
  default Hatari keyboard mapping). Warn that keyboard mode steals keys from the
  guest and that direct axes/buttons are P2/P7 `[P]`. Do not claim analog
  support.
* Input timing is host-timed in P1, not frame-locked; state this in every input
  result (`frame_locked: false`). P2 adds frame scheduling.

### 8. Safety and limits

* `safety.py` resolves every path:
  * read roots: workspace, fixture root, configured TOS roots, AHCC toolchain
    root, user-supplied read-only roots;
  * write roots: workspace, state/artifact/temp roots;
  * resolve symlinks; reject `..`, NUL, control characters, and paths outside
    roots with `path_not_allowed`;
  * `HATARI_MCP_ALLOW_OUTSIDE=1` is an explicit opt-out and is reported in the
    result.
* Destructive tools: `memory_write`, `memory_fill`, `memory_load_binary`,
  `state_load`, `emu_reset`, `emu_reboot`, `emu_stop`, `dbg_registers_set`,
  `dbg_breakpoint_delete`, `emu_save_config`, `ahcc_compile` (writes output),
  `ahcc_gui_start` (restarts). Mark `destructive_hint` and, in allowlist mode,
  require an explicit tool allowlist. `dbg_command` remains the privileged
  escape hatch and is never enabled implicitly by allowlist mode.
* Hard limits: memory read 32 KiB, memory write 4 KiB per call, disassembly 200
  instructions, symbol list 500 entries, log/console 1000 lines or 256 KiB,
  command 3.5 KiB, screenshot path length, state file 512 MiB default cap,
  input text 4 KiB, run/exit timeout 30 min maximum.
* No host shell, no `os.system`, no `shell=True`, no arbitrary command execution
  through AHCC. The debugger itself is arbitrary code execution; document it.
* Tool results include `warnings: []` for destructive actions, non-deterministic
  input, stripped symbols, headless audio/video caveats, and path opt-outs.

### 9. AHCC CLI shared module (P1.28)

`scripts/ahcc-cli.py` is the proven mechanism and the source of truth for
behavior. Do not reimplement it in the tool. Move the logic into
`src/hatari_mcp/ahcc/` and keep the script as a thin wrapper.

Shared API:

```python
def compile_project(request: AhccRequest) -> AhccResult: ...
```

`AhccRequest` fields: source, output, tos, ahcc_dir, hatari_bin, include_dirs,
defines, cflags, libs, startup, timeout_s, keep, verbose, extra_hatari_args.

Preserve the verified mechanics:

* validate source is a `.c` file, AHCC dir contains `AHCCST_P.TTP`, Hatari and
  `hatari-prg-args` exist, TOS exists;
* create a private temp work dir; copy the AHCC directory; copy the source to
  `SOURCE.C`; copy sibling `*.h` into `LOCALINC`; copy each include dir to
  `INC1..N`;
* build `MCPOUT.PRJ` in latin-1 with:
  `MCPOUT.TOS`, `=`, startup (`lib\ahcstart.o`), `SOURCE.C`, libraries,
  `.C [-Iinclude -Isinclude -ILOCALINC -IINCn -D... <cflags>]`, blank line;
* create an `AF_UNIX` listener, spawn `hatari-prg-args -q --control-socket
  <sock> --tos <tos> --conout 2 --fast-boot on --fast-forward on
  [extra args] -- AHCCST_P.TTP MCPOUT.PRJ` with `cwd=work`,
  `SDL_VIDEODRIVER=dummy`, `PATH` including the Hatari directory,
  `start_new_session=True`, stdout/stderr merged (the script's current
  behavior);
* accept the socket with timeout; read lines; on the first "press any key"
  (case-insensitive) send `hatari-event keypress x`;
* enforce the overall timeout; terminate the process group on timeout;
* detect failure with `(?im)^\s*(?:error|fatal)\b`; require `MCPOUT.TOS`;
* copy the result to the requested output atomically (temp file + rename);
* always clean the temp dir unless `keep=true`.

Tool behavior:

* `ahcc_compile(source, output?, tos?, include_dirs?, defines?, cflags?,
  libs?, startup?, timeout_s?, keep?)`:
  * resolve source/output under the workspace (output may be absent -> source
    sibling with `.TOS`, but only inside the workspace);
  * stage and compile in a temporary directory, then copy the result into the
    workspace;
  * return `{ok, output, size, duration_ms, exit_code, diagnostics: [{severity,
    message, line?, raw}], stdout, stderr, timed_out}`;
  * parse diagnostics best-effort and always include `raw`; AHCC output is not
    a structured compiler protocol.
* Single translation unit only in P1. Multi-file projects and PRJ passthrough
  are P3.13. Do not pretend `include_dirs` means linked modules.
* The standalone script must call the shared `main()` and keep its CLI
  compatibility. It may insert `mcp/project/src` on `sys.path` when the package
  is not installed; it must not carry a second copy of the staging logic.
* The AHCC ephemeral process is independent of the main supervisor. It does not
  count as a second long-lived emulator; it is bounded by the compile timeout
  and killed on return. Document this exception in `architecture.md`.

### 10. AHCC GUI mode (P1.29)

The roadmap asks for `ahcc_gui_start`/`ahcc_gui_status` through the same
screenshot/input tools. The v1 architecture says one long-lived Hatari child per
server. These conflict if implemented as a second primary emulator.

Decision for P1: implement GUI mode as a **mode switch of the primary
supervisor**, not a second long-lived child:

* `ahcc_gui_start(replace=false, display=false, tos?, workspace?, timeout_s?)`:
  * if no emulator is running: start one with a GEMDOS workspace containing the
    AHCC directory (or a symlinked read-only mount if supported), autostart
    `AHCCST.PRG`, and mark `mode="ahcc_gui"`;
  * if an emulator is running: refuse with `already_running` unless
    `replace=true`; with `replace=true`, stop it (state is lost), then start GUI
    mode;
  * never kill an emulator with unsaved state without the explicit flag.
* `ahcc_gui_status`: mode, pid, state, boot/readiness heuristic, AHCC version if
  visible, screenshot hash, last input, and whether the CLI path is available.
* Readiness is best-effort: wait for the socket, then poll a screenshot hash and
  console/log markers until stable or timeout. Do not claim a GEM desktop is
  ready just because the process is alive.
* The GUI is driven through `screen_screenshot` and `input_*`; no new input path.
* The CLI compile path is always available and independent; a GUI failure must
  never break `ahcc_compile`.
* If a second concurrent GUI instance becomes a hard requirement, it is a P6
  session-manager change, not a P1 exception to the one-child rule.
* AHCC GUI timing, dialogs, and anchors are fragile; return
  `best_effort: true` and never block the server waiting for a human.

### 11. Tool result conventions

Every successful tool returns a dict with `ok: true` plus typed fields. Failures
return a structured envelope with `ok: false`, `error.code`, `error.message`,
`error.hint`, optional `detail`, and `raw` where available, and are marked as an
MCP tool error (`is_error=True`). `raw` text is retained for best-effort parsers.
Include `warnings` and `frame_locked: false` where applicable. Destructive
results include `before`/`after` only when cheap and safe.

## Task checklist

### Lifecycle and configuration

- [ ] P1.1 Add `emu_reset` with warm->soft/cold->hard mapping and
      `emu_reboot`; test state transitions and no-sentinel handling.
- [ ] P1.2 Add typed `emu_machine`, `emu_memory_size`, TOS selection, and
      restart-required detection; never fabricate a live reconfigure.
- [ ] P1.3 Add workspace/GEMDOS configuration: `--harddrive`,
      `--gemdos-drive`, case/time/conversion, write protection.
- [ ] P1.4 Add autostart path mapping and validation; reject host paths passed
      to `--auto`; test subdirectories and drive letters.
- [ ] P1.5 Add headless defaults (`SDL_VIDEODRIVER=dummy`, `--sound off`,
      `--confirm-quit off`, no dialogs); `display=true` opt-in.
- [ ] P1.6 Add determinism basics (`--fast-boot on`, `--timer-d on`,
      `--fastfloppy on`, `--rtc-year`), document remaining non-determinism.
- [ ] P1.6a Add the private config paging-disable guard and test a paging
      command.

### Program run loop

- [ ] P1.7 Add `--natfeats on`, `NF_STDERR` buffering/classification, `NF_EXIT`
      parsing, child-returncode reconciliation, and `program_exit_code`.
- [ ] P1.8 Add `--conout 2` console capture and `since` cursors; flatten VT-52.
- [ ] P1.9 Add `emu_wait_for_exit` and the minimal Pterm trap path; return
      `{exit_code, exit_reason, output, console, duration, crashed}`.
- [ ] P1.10 Add explicit `--debug-except` mask, exception parsing, event, and
      `emu_status.exception`.

### Symbols and code

- [ ] P1.11 Implement `dbg_symbols_load`/`dbg_symbols_list` with PRG, file, and
      ASCII symbol support; test missing/stripped symbols.
- [ ] P1.12 Implement `dbg_registers_get/set` with coherent snapshot rules.
- [ ] P1.13 Implement `dbg_disassemble` with range-based count emulation and
      symbol annotation.
- [ ] P1.14 Implement `dbg_evaluate` parsing.

### Execution control

- [ ] P1.15 Implement breakpoint set/list/delete and watchpoint aliases with
      `once`/condition/count support; no enable/disable claim.
- [ ] P1.16 Implement `dbg_step`, `dbg_step_over`, `dbg_continue`,
      `dbg_run_until`, `dbg_status`; route stdin only at the prompt.
- [ ] P1.17 Implement `dbg_breakpoint_wait` and exception wait with timeouts;
      never await a socket reply that cannot arrive while stopped.

### Memory and state

- [ ] P1.18 Implement `memory_read` with 32 KiB cap, `savebin` preference,
      chunking, and base64/hex.
- [ ] P1.19 Implement `memory_write`, `memory_find`, `memory_save_binary`,
      `memory_load_binary`; destructive annotations and read-back verification.
- [ ] P1.20 Implement `state_save`/`state_load` with confinement, size checks,
      and stopped-state requirements.

### Observation and input

- [ ] P1.21 Implement `screen_screenshot` (PNG default, BMP/NEO/XIMG optional)
      and `screen_info`; never use `--disable-video`.
- [ ] P1.22 Implement `input_key` and `input_type_text` with the ST scancode
      table, shift handling, and inter-key delay.
- [ ] P1.23 Implement `input_mouse_move`, buttons, and double-click.
- [ ] P1.24 Implement `input_joystick` over keyboard emulation with an explicit
      warning and no direct-event claim.

### Safety

- [ ] P1.25 Implement path confinement for ROMs, images, snapshots,
      screenshots, symbols, and memory dumps; opt-out flag.
- [ ] P1.26 Add destructive annotations and allowlist mode.
- [ ] P1.27 Add hard limits, output caps, and timeouts; test each.

### AHCC

- [ ] P1.28 Extract `ahcc-cli.py` into `hatari_mcp.ahcc`; keep the script as a
      wrapper; add `ahcc_compile` with structured diagnostics and workspace
      output.
- [ ] P1.28a Add unit tests that compare the shared PRJ/staging output with the
      script's historical behavior.
- [ ] P1.28b Add integration tests for success, compiler error, missing
      toolchain, missing TOS, timeout, and output copy.
- [ ] P1.29 Add `ahcc_gui_start`/`ahcc_gui_status` as a primary-supervisor mode
      switch; document the one-child decision and best-effort limits.

## Acceptance criteria

The MVP gate is roadmap §6, plus:

1. Start an ST/STE profile headless with EmuTOS or user TOS; expose a workspace
   as a GEMDOS drive; autostart a `.prg`/`.ttp` from it.
2. Load symbols and resolve symbol names in registers/disassembly/breakpoint
   results; report when symbols are absent.
3. Capture TOS console output and NatFeats stderr; report a NatFeats exit code
   or a clean timeout with `exit_reason`.
4. Set a breakpoint by symbol/address, wait for the hit, and read registers,
   disassembly, and memory.
5. Step, step over, continue, and report PC and stop reason; no call sends
   `step` over the socket.
6. Read/write memory, save/load a state, and save/load a memory region with path
   confinement.
7. Capture a PNG screenshot while stopped and while running, headless, with
   rendering enabled.
8. Inject keyboard text and mouse movement/click; verify the guest reacts in an
   integration test.
9. Reset/pause/resume/stop cleanly; no orphan process or socket.
10. Every failure returns a typed envelope with a hint; no hanging calls,
    including a deliberately paging debugger command.
11. Unit and integration tests pass in CI without a proprietary ROM.
12. `ahcc_compile` compiles a small C program, returns diagnostics, copies the
    `.TOS` into the workspace, and never duplicates the script's logic.
13. `ahcc_gui_start` either starts/restarts the primary emulator deliberately
    or refuses; the CLI path remains unaffected.

## Test plan

### Unit tests

* `tests/unit/test_guestpath.py`: host↔guest mapping, drive letters, case,
  separators, symlink escape, root/subdir cases.
* `tests/unit/test_natfeats.py`: `NF_STDERR` partial lines, `NF_EXIT`, child
  returncode reconciliation, ambiguity with logs.
* `tests/unit/test_exception.py`: every exception line, mask parsing, event.
* `tests/unit/test_breakpoints.py`: set/list/delete parsing, ID shifts, `once`,
  conditions, terminal commands, no-sentinel rule.
* `tests/unit/test_registers.py`: `cpureg` dump/set parsing, widths, unknown
  registers.
* `tests/unit/test_disasm.py`: range construction, instruction parsing,
  truncation, symbol annotation.
* `tests/unit/test_memory.py`: hex/base64, 32 KiB cap, chunking, quote helper,
  find parsing, savebin/loadbin.
* `tests/unit/test_symbols.py`: `.sym`/ELF/DRI load command building, PRG base,
  list filters, truncation.
* `tests/unit/test_keymap.py`: ASCII/scancode/name mapping, shift expansion,
  newline/tab/backspace/escape, text batching.
* `tests/unit/test_safety.py`: read/write roots, opt-out, destructive
  annotations, allowlist mode.
* `tests/unit/test_ahcc_project.py`: PRJ text, latin-1, include copies,
  STARTUP/libs/cflags, output path; golden comparison with the script's
  historical behavior.
* `tests/unit/test_ahcc_diagnostics.py`: error/fatal lines, diagnostics
  extraction, raw retention.
* `tests/unit/test_result_envelopes.py`: success/destructive/warning shapes.

### Integration tests (real Hatari + EmuTOS)

`tests/integration/`; all skip cleanly without `HATARI_BIN`, `HATARI_TOS`, or
AHCC, and fail on corrupt fixtures.

* `test_boot_gemdos_autostart.py`: workspace drive, `--auto`, desktop, console.
* `test_natfeats_exit.py`: a small cooperative test program prints via
  `NF_STDERR`, exits with a known code; assert code/output/reason.
* `test_pterm_exit.py`: a non-NatFeats program; assert Pterm detection and
  `exit_code=None`.
* `test_breakpoint_debug.py`: symbol breakpoint, wait, registers, disassembly,
  memory, step/next/continue, status.
* `test_exception.py`: a deliberately invalid instruction; assert exception
  event, state, and crash metadata.
* `test_memory_state.py`: read/write/find/save/load and state save/load; path
  confinement refusals.
* `test_screenshot.py`: stopped and running PNG; dimensions; `screen_info`.
* `test_input.py`: a small GEM/TOS-aware test program or AHCC-compiled program
  that opens a menu/prints after keyboard and mouse input; assert observable
  console/screen change.
* `test_joystick_keyboard.py`: `--joystick 1`, arrow/right-Ctrl events, observable
  guest state; skip if no test program.
* `test_ahcc_compile.py`: hello-world C -> `.TOS`; error case; timeout case
  with a fake slow toolchain; missing toolchain.
* `test_ahcc_gui.py`: `ahcc_gui_start` replaces/refuses correctly; screenshot
  and input path reachable; CLI still works after GUI failure.

### Corpus tests

* Replay recorded streams for: GEMDOS boot, symbol load, `cpureg`, `disasm`,
  `memdump`, breakpoint hit, exception, NatFeats stderr/exit, pager prompt, and
  AHCC success/failure.
* Optional local corpus smoke (skip if the tree or toolchain is absent):
  * compile one small FreeMiNT/libcmini or GodLib C file with `ahcc_compile`
    and run the result headless; record console/exit/screenshot;
  * compile one Okami-Shell TTP source only if its include graph and license
    permit; otherwise record the skip reason.
* Corpus artifacts store only text/metadata, never third-party source or ROMs.
* Each corpus scenario declares its external root, required ROM/toolchain,
  deterministic inputs, expected artifacts, and skip reason.

### Protocol/E2E

* Drive the server through MCP stdio: start, autostart, screenshot, breakpoint,
  step, memory read, stop; assert result envelopes and events.
* Verify a client-visible `is_error=True` failure for a destructive op in
  allowlist mode.
* Verify no stdout pollution from child processes or AHCC.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Debugger state detection is heuristic | Commands sent on wrong channel can hang | Probe + explicit state; fail with `debugger_state_unknown`; test breakpoint/exception paths |
| Debugger pager reads stdin | Deadlock, socket stops | P0 config disabling paging; `pager_blocked`; caps |
| `--auto` host/guest path confusion | Autostart fails | Map to guest path; tests for root/subdir/drive letters |
| GEMDOS case/name mapping is lossy | File not found in guest | Document, reject non-round-trippable names, test |
| NatFeats requires cooperative program | No exit code for normal programs | Pterm fallback with `exit_code=None`; P3.3 full ladder |
| `NF_STDERR` raw output is ambiguous | Misclassification | Buffer + kind + caveat; never drop |
| `cont`/`quit`/`reset` have no sentinel | Stale sentinel contaminates next command | Never queue; transition and verify via events |
| `step` must be at prompt | Running/remote-paused step unsupported | `not_stopped` hint; tests |
| Disassembly count is not native | Over/under-fetch | Conservative range + truncate; cap |
| Memory dump formatting/paging | Parse failures/hangs | Prefer `savebin`; private config; raw fallback |
| State save while running is inconsistent | Corrupt snapshots | Require stopped/paused; size checks |
| AHCC CLI copies whole toolchain per compile | Slow, disk-heavy | Temp dir cleanup; document; optimize later |
| AHCC diagnostics are not structured | Partial parse | Raw always returned; best-effort severity |
| AHCC compile timeout leaves children | Orphans | Process group kill; cleanup test |
| AHCC GUI is a mode switch | State loss on replace | Explicit `replace=true`; never automatic |
| AHCC GUI timing/dialogs | Flaky automation | Best-effort; screenshot anchors; no infinite waits |
| Path confinement vs user TOS | False rejection | Separate configured read-only roots; explicit opt-out |
| Input is host-timed | Non-deterministic tests | `frame_locked:false`; P2 scheduling |
| Joystick keyboard mode steals keys | Guest input corruption | Explicit warning; no direct-event claim |
| No proprietary ROM in CI | Accuracy gap | EmuTOS CI; optional user TOS locally |

## Work log

### Done

* 2026-10-27, planning subagent: authored this plan and mapped every P1.1–P1.29
  item, the MVP acceptance criteria, and the AHCC CLI/GUI split.
* 2026-10-27, planning subagent: re-read `scripts/ahcc-cli.py` and extracted its
  exact staging/PRJ/socket/timeout behavior into the shared-module design rather
  than proposing a second implementation.
* 2026-10-27, planning subagent: verified stock debugger command names, reset
  kinds, breakpoint conditions/options, screenshot, symbols, memory, and
  `_vbl`/`VBL` behavior in the Hatari sources.
* 2026-10-27, implementation subagent (machine/debug/memory slice): created
  `tools/machine.py` with `emu_set_option`, `emu_set_path`, `emu_device`, and
  `emu_shortcut`; allowlists come from `Control_SetPath`,
  `Control_DeviceAction`, and `Shortcut_Invoke` in the local Hatari source.
* 2026-10-27, implementation subagent: created `tools/debug_extra.py` with
  `dbg_registers_set` (`cpureg REG=value`), `dbg_breakpoint_wait`,
  `dbg_memory_find`, `dbg_memory_save`, and `dbg_exception_get`; parsers and
  validators are covered by unit tests.
* 2026-10-27, implementation subagent: created `tools/memory_extra.py`;
  `dbg_watchpoint_set` and `dbg_watchpoint_clear` return `supported: false`
  with the exact stock-Hatari limitation instead of faking polling.
* 2026-10-27, implementation subagent: verified the stock syntax in
  `src/debug/debugcpu.c` (`DebugCpu_Register`, `DebugCpu_MemFind`,
  `DebugCpu_SaveBin`) and `src/debug/breakcond.c`. Runtime probes against
  EmuTOS 1.92 confirmed `cpureg a0=$1234`, `find $0-$100 60`, `savebin`, and a
  `:once` breakpoint at the debugger prompt.
* 2026-10-27, implementation subagent: discovered that `find` returns
  `DEBUGGER_CMDCONT`, so a socket-routed `hatari-debug find` never processes
  the echo sentinel. `dbg_memory_find` therefore requires `at_prompt` and uses
  stdin. Remote-paused debugger commands were also unreliable after the first
  command, so register set and breakpoint wait require the prompt.
* 2026-10-27, implementation subagent: `py_compile` passed for all created
  files and `pytest tests/unit/test_machine_debug_extra.py -q` passed
  (107 tests).

* 2026-10-27, reconciliation: the MVP headless loop is implemented and
  live-verified end to end: start/stop, breakpoint → `at_prompt` → stdin
  register set/step → resume → clean stop; SIGSTOP pause/resume; AHCC CLI
  compilation; and the real scenario boot-capture-stop passes. All modules are
  registered in `server.py`; the server exposes 102 tools.

### Remaining / next steps

* Acceptance item 11 (CI without a proprietary ROM) still has no MCP CI job;
  it depends on the missing P0.9 CI workflow.
* No stock watchpoint capability exists: P1 aliases remain explicit
  `unsupported` results, with real/range watchpoints blocked on a Hatari
  patch (tracked as P4.1).
* AHCC GUI is best-effort only (primary-supervisor mode switch; no second
  concurrent instance), so it cannot be part of a strict CI gate.
* `dbg_memory_find` remains prompt-only because stock `find` returns
  `DEBUGGER_CMDCONT`; running-search support needs a supervisor streaming API.
* P1 does not cover multi-file AHCC projects, full program arguments, or
  `run_prg` Pterm auto-exit; those remain P3 blockers.

### Next

1. Add the common CI workflow and a fixture-gated real-EmuTOS smoke run.
2. Keep the verified MVP path regression-tested; do not broaden watchpoint or
   absolute-mouse claims without the corresponding patch.
3. Hand residual run/exit work to P3 and watchpoint/source work to P4.

### Adversarial review (2026-10-27 implementation pass)

The Task tool is not available in this session, so an independent reviewer
could not be spawned. A recorded self-review was performed instead.

| Finding | Severity | Adjustment |
| --- | --- | --- |
| `find` returns `DEBUGGER_CMDCONT`; the socket helper waits for an echo sentinel that Hatari will not process after that return code. | High | `dbg_memory_find` requires `at_prompt` and sends `find` on stdin. |
| A remote-paused `cpureg` probe worked once and then timed out on later commands; `hatari-cont` after a socket breakpoint also failed to resume reliably. | High | Register set and breakpoint wait require `at_prompt`; remote-paused mode is rejected with a hint. |
| `dbg_breakpoint_wait` cleanup runs after a timeout while emulation may be running; a hit can race with deletion. | Medium | Cleanup is best-effort, returns `breakpoint_removed`, and emits a warning when deletion fails. |
| `dbg_memory_save` can return a short file if Hatari writes fewer bytes than requested. | Medium | Returns `size_matches` and a warning; it still reports the actual size and hash. |
| `dbg_registers_set` can target 68020+ registers on a 68000 machine. | Medium | Names are validated, but Hatari's rejection is surfaced as `unsupported_register` with a hint. |
| `app.set_option` does not inspect Hatari stderr, so unknown or restart-required options can look successful. | Medium | Recorded as a core limitation; the tool returns Hatari's fire-and-forget result without a false `applied` claim. |
| `emu_set_path` forwards a path without the planned root confinement. | Medium | Recorded as a core change; the tool validates characters and the documented path names only. |
| Watchpoint polling would report false negatives and race the CPU. | High | No polling fallback; both watchpoint tools return `supported: false` with limitation and hint. |

## Adversarial review

The requested independent review via the Task tool with `subagent_type:
"general"` could not be performed: the Task tool is not exposed in this session
(only shell, file, search, web, and Firecrawl tools are available). This is a
recorded process gap. A structured adversarial self-review was performed against
the roadmap and the Hatari source; the following findings changed the plan.

| Finding | Severity | Adjustment |
| --- | --- | --- |
| `_vbl` is not a Hatari builtin symbol; the builtin is `VBL` and the TOS vector is at `$70`. | High | P1 uses `VBL`/`($70)` only for P2 frame work; P1 breakpoints use symbols/addresses and test symbol existence. |
| `step`/`next` over the socket set a step count and then block at the prompt, leaving a sentinel unread. | High | Step/next require `at_prompt` and are sent on stdin; `not_stopped` otherwise. |
| The debugger pager reads stdin from socket commands, deadlocking the emulator. | High | P0/P1 generated config disables paging; P1 tests a paging command; `pager_blocked`. |
| `--auto` does not accept host paths. | High | Workspace-relative path -> guest path mapping; reject unmapped files. |
| `cont`/`quit`/`reset` never reach a sentinel. | High | Terminal commands are special-cased; no sentinel queued. |
| `emu_reset` has only native soft/hard; cold/warm are aliases. | High | Explicit mapping; no false four-kind claim. |
| `dbg_disassemble(count)` is not a native Hatari argument. | Medium | Conservative address range + parse + truncate. |
| `dbg_registers_get` while running is racy. | Medium | Require stopped/remote-paused; optional pause/read/resume. |
| `state_save` while running is not atomic. | Medium | Require stopped/paused; size and path checks. |
| AHCC GUI as a second emulator conflicts with the one-child rule. | High | Mode switch of the primary supervisor; explicit replace; second instance deferred to P6. |
| AHCC compile diagnostics are unstructured text. | Medium | Always return raw + best-effort diagnostics. |
| AHCC CLI single-file staging cannot represent multi-file projects. | Medium | Explicit P1 limitation; multi-file is P3.13. |
| `NF_STDERR` is raw and may look like logs. | Medium | Buffered `natfeats_stderr` with a documented caveat. |
| Joystick fire is right-Ctrl, not a direct button; arrow keys are stolen from the guest. | Medium | Scancode mapping + warning; direct injection deferred. |
| `--debug-except all` includes `autostart` and DSP behavior. | Medium | Explicit exception mask, not `all`. |
| Path confinement can reject a legitimate user TOS. | Medium | Separate read-only configured roots and explicit opt-out. |
| `emu_wait_for_exit` is not in the tools reference. | Low | Add it and update the reference before implementation. |
| A Pterm trap exit code is unknown. | Low | Return `exit_code=None`, `exit_reason="pterm"`; P3.3 improves it. |

No roadmap item was dropped. P1.29 is narrowed to a best-effort mode switch, and
the GUI automation scenarios that require stable anchors remain in P2.15.
