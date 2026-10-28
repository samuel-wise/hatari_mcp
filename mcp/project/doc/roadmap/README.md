# Hatari MCP roadmap

Status: living document; implementation snapshot added 2026-10-27.
Last updated: 2026-10-27.
Companion docs: `../architecture.md`, `../tools-reference.md`,
`../adr/0001-language-and-transport.md`, `../plans/README.md`.

This roadmap started from a deliberately small MVP — enough to run the
"compile on the host, run headless, debug, screenshot" loop — and builds up to
device coverage, deterministic graphical testing, CI automation, and eventually
an upstream protocol. The server now registers **102 tools** across lifecycle,
logs, debugger, memory/state, screen/frame, input/scripting, media/devices,
build/AHCC, protocol reporting and scenario automation.

Ordering rule: **every phase must be usable on its own**, and must not require a
later phase to be useful. Items marked `[P]` need an upstream Hatari patch
(see `../../patches/`); `[R]` is research/spike work with an unknown outcome.

Size tags: `S` < 1 day, `M` 1–3 days, `L` 1–2 weeks, `XL` multi-week/research.

### Implementation snapshot (2026-10-27)

* P0–P6 are substantially implemented: supervisor, dual socket/stdin channel,
  stderr demuxer, screenshots/frame analysis, input scripting, floppy/media
  handling, AHCC CLI/GUI, `run_prg`, scenario validate/run/replay, corpus
  discovery, artifact bundles and JUnit reports. Watchpoints remain
  unsupported, and frame-locked input/determinism is still absent.
* P7 protocol scaffolding exists in `src/hatari_mcp/protocol/`, but no
  HELLO/HELLO_ACK handshake is wired to the supervisor. `emu_capabilities`
  therefore reports legacy/banner-only and strict framed mode is unavailable.
* P8 harnesses live in the non-shipping, `HATARI_MCP_LABS=1`-gated `labs/`
  package. Their verdicts are mostly INCONCLUSIVE or narrow, not shipped
  capabilities.
* The checkboxes below are historical planning records for phases that are
  complete; consult `../plans/` for per-phase work logs and remaining items.

---

## 1. Primary workflows

### W1 — Headless compile/debug loop (the reason this project exists)

The agent already compiles Atari binaries on the host. The MCP should turn the
manual emulator dance into a small, reliable loop:

1. start a headless machine with a host directory exposed as a GEMDOS drive;
2. autostart a `.prg`/`.ttp`, with symbols loaded;
3. collect the program's console/stderr and, ideally, its exit code;
4. stop on a symbol or address, inspect registers/memory/disassembly, step;
5. capture screenshots when the program draws;
6. restart quickly for the next build.

The MCP server now exposes this loop end to end: `emu_start` maps one host
directory and autostarts, `run_prg` waits for NatFeats exit or timeout, and the
debugger tools handle symbols, breakpoints, registers, memory and stepping.

AHCC 6.0 is available locally in two forms: the command-line project manager
`AHCCST_P.TTP` (wrapped by `scripts/ahcc-cli.py`) and the GEM GUI
`AHCCST.PRG`. The MCP supports both: `ahcc_compile` runs the CLI path in an
isolated headless instance, and `ahcc_gui_start`/`ahcc_gui_status` cover the GUI
path (which needs a real display). Neither path requires modifying Hatari.

### W2 — Graphical regression testing

Boot a program, drive it, wait for a stable frame, capture a PNG, compare it
against a baseline, and attach the artifacts to a test report. This needs
deterministic frame timing more than raw speed.

### W3 — Interactive automation

Scripted keyboard/mouse/joystick input for demos, installers, games, and UI
tests. Feasibility differs per device (see §3): keyboard and mouse are mostly
there, joystick is indirect, analog devices are not.

### W4 — Debugger-assisted analysis

Watchpoints, conditional breakpoints, stack traces, profiling, tracing, crash
triage, and state diffs. Valuable for the agent and for humans using the same
server.

### W5 — Batch/CI and long-term research

Run a matrix of machines/configurations in parallel, replay scenarios, fuzz,
and measure coverage. This is where the architecture is stress-tested.

---

## 2. What Hatari gives us today

This is the baseline the roadmap builds on. No feature below should be
re-planned as if it did not exist.

| Mechanism | Capability | Limitation |
| --- | --- | --- |
| `--control-socket <path>` | Persistent two-way Unix socket; newline commands (`src/control.c`) | No response framing; only window-size replies. |
| `--cmd-fifo <path>` | One-way command pipe | No replies, no connection state. |
| `hatari-debug <cmd>` | Full debugger via `DebugUI_ParseLine()` | Output on stderr, unframed. |
| `hatari-event <ev>` | Keys, mouse buttons, relative mouse motion | No joystick/analog events. |
| `hatari-option <args>` | Runtime option changes (`Change_ApplyCommandline`) | Some options require reset. |
| `hatari-shortcut <name>` | screenshot, recanim, recsound, savemem, resets | No path control for screenshots. |
| `--parse <file>` | Startup debugger script | Startup only. |
| `--run-vbls <n>` | Run a fixed number of frames and exit | No mid-run interaction. |
| `--auto <atari-path>` | Autostart a program | Launch only. |
| `--harddrive <dir>` | GEMDOS host-directory drive | Guest drive letters/case mapping. |
| `--natfeats on` | Guest can emit stderr, exit with a code, invoke the debugger/commands | Test program must cooperate. |
| debugger `screenshot` | PNG/BMP/NEO/XIMG capture | Single frame; needs rendering enabled. |
| debugger `statesave/stateload` | Full machine snapshot | Large; no incremental state. |
| `--avirecord` + `recanim` | AVI capture of the screen | Codec/fps read when recording starts; change requires toggling. |
| `recsound` shortcut | YM/WAV recording to the configured capture file | Output format comes from that filename's extension; no per-recording path argument. |

Known environment constraints:

* Debugger output and logs share **stderr**; TOS console output is on stdout.
  The demuxer must keep them apart.
* While the debugger is at its prompt, Hatari blocks in
  `DebugUI_GetCommand()` reading **stdin**; the control socket is not polled.
  Breakpoint inspection and single-stepping therefore need a second channel
  (stdin pipe) or an upstream patch. Readline may also write its prompt to
  stdout, so a non-TTY-safe reader is required.
* `--disable-video` skips rendering, so screenshots stop working. Headless
  operation uses `SDL_VIDEODRIVER=dummy` instead.
* As of 2026-10-27 the framed protocol is not wired: even if a startup banner
  advertises it, the server reports `banner-only` and uses the legacy adapter.
* The control socket is Unix-only (`HAVE_UNIX_DOMAIN_SOCKETS`).
* There is no guest exit code unless the program uses NatFeats or a known
  termination trap.
* Hatari's symbol tables carry addresses, not source line numbers.

### 2.1 Local toolchain and example corpus

These host resources are inputs to development and testing, not files to copy
into the repository. Tests must discover them by path and skip cleanly when a
tree is absent.

| Resource | Path | Use |
| --- | --- | --- |
| Hatari | `build-install/bin/hatari` | Default emulator binary; `hatari-prg-args` sits beside it. |
| AHCC CLI | `mcp/project/tools/ahcc/ahcc-68000/AHCCST.BIN/AHCCST_P.TTP` | Project-manager compile/assemble/link path used by `ahcc-cli.py`. |
| AHCC GUI | `.../AHCCST.BIN/AHCCST.PRG` | GEM IDE path for GUI automation and dialog reproduction. |
| TOS/EmuTOS | `~/Projects/Hatari/tos/{emutos,tos1x,tos3x}` | EmuTOS for CI/demos; real TOS for accuracy. |
| Okami-Shell | `~/Projects/Hatari/Okami-Shell` | GEM shell, resource files, TTP tools, screenshots. |
| FreeMiNT | `~/Projects/Hatari/freemint/<subproject>` | Large C/C++ corpus: cflib, gemlib, libcmini, mintlib, xg, etc. |
| GodLib | `~/Projects/Hatari/godlib/{godlib,godlib.spl}` | Game/demo library with input, screen, and asset code. |

The corpus is read-only. Do not copy third-party source into `mcp/`; use it to
drive integration and scenario tests. Preserve each project's license and build
requirements in test notes.

---

## 3. Feasibility: input and observation

### 3.1 Input

| Device | Available today | Gaps | Plan |
| --- | --- | --- | --- |
| Keyboard, single keys | `input_key` accepts named keys, one printable ASCII character (including shifted punctuation) or an ST scancode 0–255 | No Unicode or layout translation in the event path | P1 scancode/key-name mapping is implemented. |
| Keyboard, text | `input_text(text, delay_ms=10)` expands a string into keypress/down/up events with a per-key delay | No Unicode or layout translation | P1 `input_text` is implemented. |
| Mouse buttons | `leftdown/leftup/rightdown/rightup/doubleclick` | No middle button (ST mice lack one) | P1. |
| Mouse motion | `mousemove <dx> <dy>` relative | No absolute positioning; corner-pin trick is fragile | P1 basic, P2 absolute helper. |
| Mouse wheel | None | ST hardware has no wheel | Not applicable. |
| Joystick | Keyboard-emulation mode (`--joystick <port>`) driven by arrow/fire scancodes | No direct axes/buttons; keyboard mode steals keys from the guest | P2 indirect; P7 direct injection `[P]`. |
| STe paddles / analog / lightpen | Not exposed | No event or control API | P8 research `[P]`. |
| Host gamepad / controller | SDL-only (`JoyUI_ReadJoystick`) | Not reachable over the socket | P7 `[P]` or virtual controller. |
| MIDI / RS232 / SCC | File or device streams at launch (`--midi-in`, `--rs232-in`, `--scc-*`) | No per-event injection or readback API; not deterministic if real devices | P5 file-backed streams; P7 socket-backed. |
| Printer | Output to file/device | No capture API beyond the file | P5. |
| Input timing | Events applied on the next emulation loop iteration | No timestamps or frame-locked delivery | P2 scheduling; P7 frame-locked queue `[P]`. |

**Bottom line:** keyboard and mouse automation is feasible today; joystick only
indirectly; analog and real-time device input need patches.

### 3.2 Observation and control

| Capability | Today | Gap | Plan |
| --- | --- | --- | --- |
| Screenshot | Debugger `screenshot <path>`; shortcut uses configured dir | No frame selection; parse/serialize is text-only | P1 single frame; P2 frame-targeted. |
| Raw framebuffer | Server decodes 24/32-bit BMP frames; PNG capture is file-only | No direct framebuffer readback and no PNG pixel decode | P2 server-side BMP decode is implemented; P7 binary readback `[P]`. |
| Frame stepping | Not exposed | `--run-vbls` is per-process; VBL breakpoint is coarse | P2 `[P]`; P2 fallback with `_vbl` breakpoints. |
| Screen metadata | `hatari-debug info` | Text parsing | P1. |
| Video recording | AVI options + `recanim` | Runtime codec/file changes limited | P2/P5. |
| Audio recording | `recsound` shortcut | Format/control unclear per build | P5. |
| Console output | stdout; `--conout` | Interleaves with nothing else (separate pipe) | P1. |
| Logs/traces | stderr; `--log-file`, `--trace-file` | Shared channel with debugger output | P1 demuxer. |
| Program exit code | NatFeats `NF_EXIT`; stderr `NatFeats: exit(N)` | Requires cooperative program | P1; P3 adds Pterm-trap fallback. |
| Crash state | `--debug-except` enters debugger on exceptions | Must be enabled and parsed | P1. |

---

## 4. Roadmap at a glance

| Phase | Theme | Outcome / gate |
| --- | --- | --- |
| **0** | Foundation | Server launches Hatari, round-trips commands, streams logs, passes CI. |
| **1** | **MVP: headless debug loop** | Start/stop, autostart from a host dir, symbols, breakpoints, registers, memory, step, screenshot, basic keyboard/mouse, state save/load. |
| **2** | Graphical test rig | Deterministic frame capture, pixel/hash access, input scripting, AVI, replay basics. |
| **3** | Build/run integration | One-call run-prg loop, exit codes, crash bundles, optional build command. |
| **4** | Debugger depth | Watchpoints, DSP, profiling, tracing, backtraces, source mapping. |
| **5** | Devices and media | Floppy/HD/cartridge, serial/MIDI/printer, audio/video modes. |
| **6** | Automation and CI | Scenario format, parallel matrix, record/replay, fuzzing, coverage. |
| **7** | Protocol and upstream | Framed RPC, notifications, binary I/O, frame-locked input, Windows. |
| **8** | Stretch | Time travel, source-level debugging, LLM-native test synthesis, remote service. |

Dependency notes: Phase 2 needs Phase 1 screenshots/input; Phase 3 needs Phase
1 lifecycle and Phase 2 capture; Phase 6 needs Phases 2–3 and benefits from
Phase 7 determinism; Phase 8 is independent once Phase 7 lands.

---

## 5. Phases

### Phase 0 — Foundation (S/M each)

Goal: a boring, reliable process wrapper. No new emulator capability yet.

- [ ] P0.1 Repository skeleton: `pyproject.toml`, `src/hatari_mcp/`, console
      entry point, `ruff`, `pytest`, `uv` workflow.
- [ ] P0.2 Supervisor: launch the configured `hatari`, create a private socket
      directory (0700), accept the connection, capture stdout/stderr, own the
      stdin pipe, enforce startup timeout, graceful stop, socket cleanup.
- [ ] P0.3 Dual control channel: socket while running/paused, stdin while at
      the debugger prompt; state machine for `running|paused|at_prompt`,
      per-command lock, 4 KiB limit, reconnect/closed-socket handling.
- [ ] P0.3a Debugger-input spike: prove a stock, readline-enabled build accepts
      commands from a stdin pipe without polluting the TOS console; if not,
      ship the minimal `!isatty()` patch (preferred) or require a no-readline
      build.
- [ ] P0.4 stderr demuxer: line splitting, classification (debugger, log,
      trace, NatFeats, breakpoint/exception entry), ring buffers, sentinel
      correlation with `echo` on both channels.
- [ ] P0.5 Error and timeout model: `emulator_gone`, `timeout`,
      `missing_tos`, `bad_option`, `not_stopped`, `parse_failed`.
- [ ] P0.6 Profiles and precedence: load TOML, merge `HATARI_BIN`/env/profile/
      call arguments, validate paths.
- [ ] P0.7 Fixture harness: fetch EmuTOS with checksum, generate a blank
      floppy and a GEMDOS directory, skip tests cleanly when absent.
- [ ] P0.8 First tools: `emu_start`, `emu_stop`, `emu_pause`,
      `emu_resume`, `emu_status`, `log_read`, `console_read`,
      `dbg_command` (privileged escape hatch).
- [ ] P0.9 CI: lint, unit tests, one integration test with a real Hatari
      binary, no ROMs committed.
- [ ] P0.10 Tool descriptions and examples that a model can actually follow
      (including failure hints).

Gate: a script can start Hatari headless, pause/resume it, read logs, run
`hatari-debug info`, and stop it cleanly, with deterministic CI.

### Phase 1 — MVP: headless compile/debug loop

Goal: W1 end to end, plus enough input/screenshot to be useful for W2.

**Lifecycle and configuration**

- [ ] P1.1 `emu_reset(kind)` and `emu_reboot` (soft/hard/cold/warm).
- [ ] P1.2 Machine/memory/TOS configuration via typed arguments, with
      restart-required detection.
- [ ] P1.3 GEMDOS host directory: `--harddrive <dir>`, `--gemdos-drive`,
      case/time/conversion options, write protection.
- [ ] P1.4 Autostart: `--auto <atari-path>` with path mapping from a host
      path; validate the file exists inside the configured workspace.
- [ ] P1.5 Headless defaults: `SDL_VIDEODRIVER=dummy`, `--sound off`,
      `--confirm-quit off`, no dialogs; optional `display=true`.
- [ ] P1.6 Determinism basics: `--fast-boot on`, `--timer-d on`,
      `--fastfloppy on`, fixed `--rtc-year`, documented caveats.

**Program run loop**

- [ ] P1.7 NatFeats support: `--natfeats on`, surface `NF_STDERR` lines and
      `NF_EXIT` codes; expose `program_exit_code` in `emu_status` and events.
- [ ] P1.8 Console capture: TOS console stdout, `--conout`, ring buffer with
      `since` cursors.
- [ ] P1.9 Run-to-exit helper: wait for NatFeats exit, Pterm trap, or timeout;
      return `{exit_code, output, duration, crashed}`.
- [ ] P1.10 Exception surfacing: `--debug-except`, classify bus/address/
      illegal/privilege/trace and enter debugger; emit a notification.

**Symbols and code**

- [ ] P1.11 `dbg_symbols_load`: ELF, DRI/GST, a.out, and nm `.sym` files;
      `--symload exec` autoload; list/filter symbols.
- [ ] P1.12 `dbg_registers_get/set` for m68k (d0–d7, a0–a7, pc, sr, usp/isp).
- [ ] P1.13 `dbg_disassemble` with address/count and symbol annotation.
- [ ] P1.14 `dbg_evaluate` (registers, symbols, `_vbl`, expressions).

**Execution control**

- [ ] P1.15 `dbg_breakpoint_set/list/delete` for exec, memory, and
      register breakpoints; `once` and condition support. There is no
      enable/disable; delete and re-add instead.
- [ ] P1.16 `dbg_step`, `dbg_step_over`, `dbg_continue`, `dbg_run_until`,
      and `dbg_status` (running vs stopped, PC, reason); route via stdin at
      the prompt and via socket when running.
- [ ] P1.17 Breakpoint/exception wait API with timeout and event stream;
      never wait on a socket reply that cannot arrive while stopped.

**Memory and state**

- [ ] P1.18 `memory_read` (hex/ASCII/base64) with a 32 KiB cap, alignment
      handling, and chunking.
- [ ] P1.19 `memory_write`, `memory_find`, `memory_save_binary`,
      `memory_load_binary`; destructive annotations.
- [ ] P1.20 `state_save`/`state_load` with path confinement and size checks.

**Observation and input**

- [ ] P1.21 `screen_screenshot` (PNG by default, BMP/NEO/XIMG optional),
      `screen_info` (resolution, monitor, mode).
- [ ] P1.22 Keyboard: `input_key` (press/down/up), ST scancode table,
      `input_type_text` with optional inter-key delay.
- [ ] P1.23 Mouse: `input_mouse_move` (relative), buttons, double click.
- [ ] P1.24 Joystick: document and expose the keyboard-emulation path
      (`--joystick <port>`) as `input_joystick` with an explicit warning.

**Safety**

- [ ] P1.25 Path confinement for ROMs, images, snapshots, screenshots, symbol
      files; opt-out flag.
- [ ] P1.26 Destructive tool annotations and an optional allowlist mode.
- [ ] P1.27 Hard limits: output size, dump size, command length, runtime.

**AHCC toolchain**

- [ ] P1.28 `ahcc_compile`: expose the verified `scripts/ahcc-cli.py` flow as a
      tool (source, output, TOS, include/define/lib, timeout); copy the result
      into the workspace and return structured diagnostics. Keep the script as
      the standalone path and share one implementation.
- [ ] P1.29 `ahcc_gui_start` / `ahcc_gui_status`: launch `AHCCST.PRG` in a
      visible or virtual display, wait for the GEM desktop/IDE, and expose it
      through the same screenshot/input tools. GUI automation is best-effort
      and must never block the CLI path.

Gate: the MVP acceptance criteria in §6.

### Phase 2 — Graphical test rig

Goal: W2. Deterministic capture and scripted interaction, not just "a PNG".

- [ ] P2.1 Frame-targeted capture: pause at a VBL breakpoint, capture, resume;
      `run_frames(n)` built from a `_vbl` breakpoint or `--run-vbls`.
- [ ] P2.2 `wait_vbl`, `wait_frames`, `wait_for_screen_change` (hash polling
      as fallback).
- [ ] P2.3 Frame stepping and frame-locked commands `[P]`.
- [ ] P2.4 Raw pixel access: decode PNG server-side; return RGBA regions,
      per-pixel probes, checksums, and dimensions.
- [ ] P2.5 Region capture and cropping without writing a file.
- [ ] P2.6 Screenshot sequence/burst mode and artifact retention policy.
- [ ] P2.7 Baseline comparison helper: exact hash, per-pixel diff, tolerance,
      bounding box; return a diff image. (Keep image diff out of core if the
      client can do it; expose deterministic data either way.)
- [ ] P2.8 AVI recording: start/stop/status, codec/fps/file, `recanim`;
      audio recording via `recsound`.
- [ ] P2.9 Absolute mouse positioning helper (corner pin + delta), drag,
      click-at-coordinates, and a warning about guest scaling/borders.
- [ ] P2.10 Input scripting: timed sequences, key repeat, layout-aware
      mappings, scancode vs character modes.
- [ ] P2.11 Direct joystick axes/buttons `[P]`; keep keyboard emulation as the
      no-patch path.
- [ ] P2.12 Input recording/replay (server-side event log; replay requires
      frame-locked delivery to be exact).
- [ ] P2.13 Deterministic screenshot mode: fixed RTC, fixed seed, no host
      timing dependence where possible; document what remains non-deterministic.
- [ ] P2.14 Screen modes: color/mono, borders, overscan, ST/STE/TT/Falcon
      Videl modes; verify capture in each.
- [ ] P2.15 AHCC GUI scenarios: build a project through the IDE using
      keyboard/mouse (open project, compile, read dialogs, capture errors);
      define stable screen anchors and document where GUI timing is fragile.

Gate: a test can boot a program, send input, capture the same frame twice with
identical hashes, and produce a baseline diff.

### Phase 3 — Build/run integration

Goal: collapse the manual loop into a few calls. Compilation itself stays
outside unless explicitly enabled.

- [ ] P3.1 Workspace model: one or more host roots, guest drive mapping,
      read-only mounts, path translation both ways.
- [ ] P3.2 `run_prg`: restart with profile + GEMDOS dir + autostart + symbols +
      NatFeats; wait for exit/output/timeout; return a structured result.
- [ ] P3.3 Exit detection ladder: NatFeats `NF_EXIT` → Pterm/Pterm0 trap →
      console sentinel → timeout.
- [ ] P3.4 Crash bundle: on exception, capture registers, disassembly, stack
      window, screenshot, recent log, and optional state snapshot.
- [ ] P3.5 Symbol autoload for autostarted PRGs; address→symbol resolution for
      every result.
- [ ] P3.6 Optional build command: off by default; allowlisted command, no
      shell interpolation, working directory pinned; explicit security warning.
- [ ] P3.7 Fast restart strategy: reboot vs relaunch; measure and document
      which is faster for the workflow.
- [ ] P3.8 Disk/image builders: blank floppy, `zip2st`, `atari-hd-image`,
      createBlankImage; deterministic sizes and checksums.
- [ ] P3.9 Host↔guest file transfer via the GEMDOS dir, with case conversion
      and conflict reporting.
- [ ] P3.10 Machine matrix profiles (ST/STE/TT/Falcon) and per-profile
      expectations.
- [ ] P3.11 Source mapping `[R]`: map m68k-atari-mint ELF/DWARF line info to
      addresses; Hatari symbols alone have no lines.
- [ ] P3.12 External corpus harness: discover Okami-Shell, FreeMiNT, and GodLib
      trees; select buildable targets per project; record toolchain and ROM
      prerequisites; skip cleanly when a tree or toolchain is absent.
- [ ] P3.13 AHCC build matrix: compile representative C from each corpus with
      the CLI path, run the resulting PRG, and capture console/screenshot/exit
      status. Include at least one multi-file project and one GUI build.

Gate: one tool call runs a freshly built PRG to completion and returns exit
code, console output, and a screenshot; a failing run returns a crash bundle.

### Phase 4 — Debugger depth

Goal: W4. Close the gap with the interactive debugger.

- [ ] P4.1 Memory watchpoints (read/write/read-write) and range breakpoints.
- [ ] P4.2 Conditional breakpoints, hit counts, temporary breakpoints.
- [ ] P4.3 Breakpoint actions: print registers/memory, continue, run a script
      (guarded, no host command execution).
- [ ] P4.4 DSP equivalents: registers, memory, disassembly, breakpoints,
      step/next/continue (`dsp*` commands).
- [ ] P4.5 CPU state beyond GPRs: FPU, MMU, caches, SR/ISP/USP, 68020+ modes.
- [ ] P4.6 `dbg_backtrace`, `dbg_struct`, stack window, call-stack heuristics.
- [ ] P4.7 `dbg_history`, `dbg_profile_start/read/stop`, trace sets and
      trace-file management.
- [ ] P4.8 Symbol management: load/unload, name collisions, demangling, PRG
      base-address handling.
- [ ] P4.9 Memory checksums and state diffs between snapshots.
- [ ] P4.10 Non-interactive pager handling: disable paging, cap output,
      stream large results in chunks.
- [ ] P4.11 Exception policy: configurable mask, vector breakpoints,
      first-chance vs repeated reporting.
- [ ] P4.12 Crash classification heuristics: illegal instruction, bad jump,
      stack corruption, null deref; link to source symbol when available.
- [ ] P4.13 Source-level stepping `[R]`: needs DWARF mapping plus a
      line-table→PC cache; without it, only symbol-level debugging is honest.

Gate: the interactive debugger's common workflows are all reachable through
tools, with structured results.

### Phase 5 — Devices and media

Goal: cover the hardware surface that tests and demos need.

- [ ] P5.1 Floppies: insert/eject A/B, write protect, heads, fast floppy;
      ST/MSA and optional STX/IPF (build-dependent).
- [ ] P5.2 Hard disks: GEMDOS dir, ACSI, SCSI, IDE master/slave, byte swap,
      versions, write protect.
- [ ] P5.3 Cartridge insert/eject.
- [ ] P5.4 Printer output capture and reset.
- [ ] P5.5 RS232 in/out: file-backed streams, later socket-backed.
- [ ] P5.6 MIDI in/out: file-backed for determinism; host devices for
      interactivity.
- [ ] P5.7 SCC/LAN in/out streams.
- [ ] P5.8 Audio: enable/disable, frequency, buffer, sync, YM mixing, mic;
      WAV recording and AVI audio.
- [ ] P5.9 Video: monitor type, TOS resolution, borders, spec512, video
      timing, VDI modes, force-max, aspect, crop, zoom, frame skip.
- [ ] P5.10 Hardware options: CPU level/clock, compatible mode, data cache,
      cycle-exact, 24-bit addressing, FPU, MMU, blitter, DSP, RTC, timer-D.
- [ ] P5.11 Config persistence: read/write `.cfg`, `--saveconfig`, diff
      against running state.
- [ ] P5.12 Media hot-swap timing and validation; never silently continue with
      a missing image.

Gate: a graphical demo requiring a floppy/HD and audio can be launched,
captured, and validated headlessly.

### Phase 6 — Automation and CI

Goal: W5. Make the server a dependable test runner.

Implementation note: the scenario engine, strict tool allowlist, artifact
bundles, corpus discovery, replay (approximate fidelity) and parallel case
execution are implemented. Items below remain the original design record.

- [ ] P6.1 Scenario format (JSON/YAML): start, input, waits, assertions on
      screen/memory/console/exit code, teardown.
- [ ] P6.2 Scenario runner with per-step timeouts and clear failure locations.
- [ ] P6.3 Artifact bundle: screenshots, diffs, AVI, logs, state, config,
      Hatari version, profile.
- [ ] P6.4 Parallel instances with resource caps (CPU, RAM, disk, open files)
      and isolated sockets/workspaces.
- [ ] P6.5 Machine/config matrix execution and aggregated reports.
- [ ] P6.6 JUnit/JSON output and CI annotations; stable exit codes.
- [ ] P6.7 Docker/CI image: Hatari build, EmuTOS, toolchain, Python runtime.
- [ ] P6.8 Deterministic mode: fixed RTC/seed, frame-locked input `[P]`,
      cycle-exact option, documented tolerances.
- [ ] P6.9 Record/replay: state + input + trace; replay verification.
- [ ] P6.10 Fuzzing harness: random memory/IO/input within a sandbox; crash
      corpus and deduplication.
- [ ] P6.11 Coverage `[R]/[P]`: basic-block or instruction coverage via
      breakpoints/profile, or emulator instrumentation.
- [ ] P6.12 Attach mode: drive a Hatari instance started by someone else.
- [ ] P6.13 Session manager: list/wait/cleanup orphaned emulator processes.
- [ ] P6.14 Corpus scenarios: smoke scenarios for Okami-Shell, selected
      FreeMiNT components, and GodLib; each declares its external root,
      required ROM/toolchain, deterministic inputs, and expected artifacts.

Gate: a CI job can run a scenario matrix in parallel and publish artifacts and
a pass/fail report without leaking processes or sockets.

### Phase 7 — Protocol and upstream hardening

Goal: remove the sentinel/parse hacks and make the integration first-class.

Implementation note: framing/codec/banner primitives exist, but P7.1's
handshake is not wired and P7.2–P7.5 are not implemented. `emu_capabilities`
tells the truth about this.

- [ ] P7.1 Framed control protocol `[P]`: request id, `OK`/`ERR`, end marker,
      protocol version handshake; poll it from the debugger loop so one
      channel works in every state.
- [ ] P7.2 Notifications `[P]`: debugger entry, breakpoint hit, exception,
      NatFeats exit, screen size change.
- [ ] P7.3 Binary framebuffer/RAM readback `[P]`: no hex/PNG round-trips.
- [ ] P7.4 Direct input injection `[P]`: absolute mouse, joystick axes/buttons,
      host gamepads, timestamped/frame-locked event queue.
- [ ] P7.5 Frame stepping and deterministic clock `[P]`.
- [ ] P7.6 Windows control channel `[P]`: named pipes or in-process commands.
- [ ] P7.7 Capability negotiation and graceful fallback for unpatched Hatari.
- [ ] P7.8 Crash-safe supervisor: heartbeat, automatic restart, state
      preservation, orphan cleanup.
- [ ] P7.9 Upstream process: patch series, tests, review, release notes,
      compatibility matrix across Hatari versions/build flags.
- [ ] P7.10 Optional HTTP/SSE transport and authentication (stdio remains the
      default; never expose the debugger unauthenticated).

Gate: the server runs against stock Hatari and against the patched build with
the same tool behavior, preferring the framed path when available.

### Phase 8 — Stretch and research

Only pursue when a concrete user need appears. These are not commitments.

Implementation note: the P8 lab harnesses are complete but non-shipping. Most
verdicts are INCONCLUSIVE or narrow GO; no P8 item is a committed feature.

- [ ] P8.1 Time-travel debugging: periodic snapshots + replay to a step.
- [ ] P8.2 Full source-level debugging: breakpoints by file:line, variables,
      watch expressions, source view (DWARF).
- [ ] P8.3 Coverage and test-gap analysis integrated into scenario reports.
- [ ] P8.4 Network/SCC-backed device tests and packet capture.
- [ ] P8.5 Remote MCP service with sandboxing and multi-user isolation.
- [ ] P8.6 Live framebuffer streaming (WebSocket/resource subscription).
- [ ] P8.7 Attach to a running UI instance and drive it in "pair mode".
- [ ] P8.8 Host input recording (gamepad/keyboard) for demo capture.
- [ ] P8.9 LLM-native test tooling: test synthesis from a goal, failure
      explanation, delta-debugging/minimization, crash clustering.
- [ ] P8.10 Performance benchmarking and regression detection for emulator
      configurations.
- [ ] P8.11 Real-hardware bridge (hatari vs. real Atari comparison) — likely
      out of scope.
- [ ] P8.12 Analog devices: paddles, lightpen, STe analog joysticks `[P]`.

---

## 6. MVP acceptance criteria

The MVP is done when all of the following are true, using only the MCP server
and a stock Hatari build:

1. Start an ST/STE profile headless with a user-supplied or EmuTOS ROM.
2. Expose a host directory as a GEMDOS drive and autostart a `.prg` from it.
3. Load symbols for that PRG and resolve symbol names in results.
4. Capture TOS console output and NatFeats stderr; report the NatFeats exit
   code or a clean timeout.
5. Set a breakpoint by symbol/address, wait for the hit, and read registers,
   disassembly, and memory.
6. Step, step over, continue, and report `pc` and stop reason.
7. Read and write guest memory, save/load a state, and save/load a memory
   region.
8. Capture a PNG screenshot while the machine is stopped or running, in
   headless mode.
9. Inject keyboard text and mouse movement/click events; verify the guest
   reacts (e.g. a menu opens).
10. Reset/pause/resume/stop cleanly; no orphan processes or sockets remain.
11. Every failure returns a typed error with a useful hint; no hanging calls.
12. Unit + integration tests pass in CI without a proprietary TOS ROM.

Anything beyond this list belongs to Phase 2+.

---

## 7. Cross-cutting workstreams

These run in parallel with every phase:

- **Security**: stdio-only by default; private socket; path confinement;
  destructive-tool gating; no arbitrary host shell; explicit allowlist mode;
  document the debugger's arbitrary-code-execution equivalence.
- **Model ergonomics**: short tool names, precise descriptions, examples for
  each workflow, actionable error hints, stable result schemas, a
  `hatari://docs/*` resource set.
- **Compatibility**: Hatari versions, build flags (SDL2/SDL3, libpng, capslib,
  PortMidi, DSP), Linux/macOS/BSD; Windows only after Phase 7.
- **Observability**: structured server logs, request ids, timing per tool,
  bounded buffers, optional metrics.
- **Testing**: unit (parsers/builders), integration (real Hatari + fixtures),
  protocol/E2E, recorded-stream regressions; no proprietary ROMs in CI.
- **Performance**: event-driven waits, no busy polling, chunked large reads,
  avoid PNG round-trips when binary readback lands.
- **Reliability**: timeouts everywhere, idempotent lifecycle calls, crash
  recovery, cleanup on signal/exit, no leaked child processes.
- **Licensing**: GPL-2.0-or-later for `mcp/` and patches; dependency license
  checks (see `../licensing.md`).
- **Docs**: keep `architecture.md` and `tools-reference.md` in sync with tools;
  every phase produces a plan in `../plans/` and, when a decision changes, an
  ADR in `../adr/`.

---

## 8. Explicit non-goals

- Reimplementing or forking emulation logic.
- Shipping Atari TOS ROMs or commercial software.
- Replacing the Hatari UI for human users.
- Arbitrary host shell execution as a feature.
- Network-exposed control by default.
- Multi-user hosting in v1.
- Supporting every exotic host platform before the Unix path is solid.

---

## 9. Open questions

1. **ROM policy**: require a user-supplied TOS for all real runs, or support
   EmuTOS as the default for tests and demos? (Assumed: EmuTOS for CI,
   user TOS for accuracy.)
2. **Joystick**: is keyboard-emulation good enough, or is a direct injection
   patch worth upstream effort in Phase 2 instead of Phase 7?
3. **Build execution**: should the server ever compile code, or is that always
   the client's shell? (Assumed: opt-in, off by default, Phase 3.)
4. **Screenshot comparison**: server-side baseline diff vs. returning raw
   pixels/hashes for the client to compare? (Assumed: expose both; keep the
   core deterministic and cheap.)
5. **Determinism**: how much host-timing variance remains after frame-locked
   input? Need a measured answer in Phase 2/6.
6. **Multi-instance model**: one server per emulator (current assumption) vs.
   a session manager inside one server (Phase 6).
7. **Upstream appetite**: will Hatari accept a framed protocol, binary readback,
   and input injection? If not, patches must stay small and optional.
8. **Windows**: is it required by real users? It is a Phase 7 item unless
   demand appears earlier.
9. **Language**: Python per ADR 0001. Rust-over-socket remains compatible with
   this roadmap if the decision is revisited; FFI/linking is not.
10. **Debugger prompt**: is a stdin pipe sufficient for stock readline builds,
    or do we ship the `!isatty()`/socket-polling patch as a hard requirement?
    Phase 0 spike decides; the answer affects every step/breakpoint tool.

---

## 10. How this roadmap is maintained

- The per-phase implementation plans are indexed in `../plans/README.md`.
  Each plan links back to the item ids below and records work done, remaining
  work, verification, and review findings.
- Items move to `../plans/` when work starts; the plan links back to the item
  id and states the exit criteria.
- Decisions that change the architecture get an ADR; this file does not carry
  rationale for them.
- When a phase ships, replace the checkbox with a short "shipped in <version>"
  note and keep the item for history.
- Re-order only by moving whole items, never by silently dropping them; if an
  item is no longer wanted, mark it `Dropped` with a reason.
