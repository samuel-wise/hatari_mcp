# Architecture

Status: current implementation snapshot (2026-10-27). The server registers
**102 tools** from 17 registration modules. Tool-level details live in
`tools-reference.md`; this page describes how the server talks to Hatari
and where the current limits are.

Related: `adr/0001-language-and-transport.md`,
`adr/0002-framed-control-protocol.md`, `tools-reference.md`,
`plans/README.md`.

## 1. Goal and scope

Give an MCP client (an AI agent) first-class control of one running Hatari
instance:

* lifecycle — launch, stop, pause/resume, reset, quit, wait for exit;
* configuration — runtime options, paths, devices, shortcuts;
* logs — classified stderr and TOS console stdout;
* input — keyboard, mouse, indirect joystick, timed sequences and replay;
* media — floppies at runtime; HDs/cartridges as restart-required plans;
* observation — screenshots, frame/pixel/hash/diff analysis, video info;
* state — save/load snapshots, exact memory read/write/find/dump/diff;
* debugging — CPU/DSP registers, breakpoints, stepping, disassembly,
  expression evaluation, symbols, profiling, tracing, backtraces;
* build/run — AHCC CLI compile, AHCC GUI launch, `run_prg`, workspace info;
* automation — scenario validation/run/replay and read-only corpus discovery.

Out of scope: replacing the Hatari UI, writing a new emulator, Windows support
(the control socket is Unix-only), distributing ROMs, and multi-instance
orchestration inside one server process.

## 2. What Hatari gives us

The design reuses existing, documented emulator mechanisms. No private globals
and no GDB stub are required.

### 2.1 Control socket (primary channel)

`--control-socket <path>` (Unix only, implemented in `src/control.c`).

* The MCP server creates an `AF_UNIX`/`SOCK_STREAM` socket and listens.
* Hatari connects to it; Hatari is the client.
* Commands are newline-delimited UTF-8, executed by `Control_ProcessBuffer()`.
* The socket is polled from `Control_CheckUpdates()` during the emulation loop,
  so execution is asynchronous relative to the caller.
* Writes are kept small (well under `MAX_DEBUG_CMD_LEN`, 4096 bytes).

Supported control commands used by the server:

| Command | Effect |
| --- | --- |
| `hatari-debug <debugger command>` | `DebugUI_ParseLine()` — debugger surface |
| `hatari-event <event>` | keyboard/mouse injection; joystick only indirectly |
| `hatari-option <cli options>` | `Change_ApplyCommandline()` at runtime |
| `hatari-enable/disable/toggle <device>` | device toggles |
| `hatari-path <config name> <path>` | runtime path changes |
| `hatari-shortcut <name>` | invoke a UI shortcut action |
| `hatari-embed-info` | ask for embedded window size messages |

**Critical limitation:** the socket is bidirectional but carries no command
responses. The only bytes Hatari writes back are window-size messages after
`hatari-embed-info` (and the echo sentinel used by this server is debugger
output on stderr). Debugger output is not sent to the socket.

### 2.2 Debugger channels

`hatari-debug <command>` calls `DebugUI_ParseLine()`. Debugger output goes to
`debugOutput`, which is **stderr** by default. `--parse <file>` runs a command
script at startup through the same path.

While the debugger is at its prompt, the emulation loop is blocked in
`DebugUI_GetCommand()` reading **stdin**, and `Control_CheckUpdates()` is not
called. Socket commands sent after a breakpoint or exception are queued but not
processed until the debugger exits. The supervisor therefore has two command
channels:

* the control socket while the CPU is running;
* Hatari's stdin pipe while the debugger is at its prompt (breakpoint,
  exception, `--parse`, manual entry).

`step`/`next` return to the prompt and stay on stdin; `cont`, `quit`, and
`reset` leave the debugger and switch back to the socket. Pagers are disabled
by the generated config; a pager prompt is reported as `pager_blocked`.

### 2.3 Pause and resume are signals, not debugger commands

`emu_pause` sends `SIGSTOP` to the Hatari child. While it is stopped, neither
the debugger nor the control socket is serviced, so debugger/control commands
are unavailable until `emu_resume`. `emu_resume` sends `SIGCONT` and then waits
for a control-socket echo acknowledgement before reporting `running`. At the
debugger prompt, `emu_resume` instead sends `cont` on stdin. Pausing is only
valid from `running`, not from `at_prompt`.

### 2.4 Determinism and headless operation

* `SDL_VIDEODRIVER=dummy` runs SDL without a window while still emulating and
  rendering the framebuffer, so debugger `screenshot` keeps working.
* Do **not** use `--disable-video` when screenshots or framebuffer reads are
  needed; it skips rendering.
* `--sound off`, `--confirm-quit off`, `--configfile <path>` avoid dialogs and
  host-state leakage in the default profiles.
* `--run-vbls <n>` exits after `n` vertical blanks; `--benchmark` disables
  interactive timing. These are launch-time only.
* NatFeats is **off by default**. `nf_stderr` lets guest code write diagnostic
  text that looks like server output, so `emu_start(natfeats=true)` is an
  explicit opt-in used by `run_prg` for cooperative programs that need
  `NF_EXIT`. Pterm/Pterm0 auto-exit is not enabled by default.

### 2.5 Protocol capability status

`src/hatari_mcp/protocol/` contains framing, codec, banner and HELLO/HELLO_ACK
primitives. The supervisor only reads an advisory startup banner; it does **not**
wire a framed handshake. Consequently:

* `emu_capabilities` reports `legacy` (no banner) or `banner-only` (banner seen
  but no completed handshake), never native framed mode;
* `framed`, `framed_prompt`, `input_abs`, `input_frame`, `clock_virtual` and
  `windows_pipe` are reported unsupported;
* `notify`, `binary`, `fb_native`, `input_joy`, `frame_step` and
  `headless_render` are reported as documented fallbacks;
* requesting `framed`/`require-framed` does not silently downgrade; strict mode
  is unavailable on stock Hatari.

This is deliberate: capability reporting must be truthful rather than claiming a
protocol that is not connected.

## 3. Components

```
┌──────────────────┐   MCP (stdio, JSON-RPC)   ┌──────────────────────────┐
│  MCP client      │ ◄───────────────────────► │  hatari-mcp server       │
│  (AI agent)      │                           │  (Python, src/)          │
└──────────────────┘                           │                          │
                                               │  1. process supervisor   │
                                               │  2. dual command channel │
                                               │  3. stream demuxer       │
                                               │  4. 102 tool handlers    │
                                               │  5. scenario / AHCC svc  │
                                               └──────────┬───────────────┘
                                                          │ spawn + AF_UNIX
                                                          │ socket + stdio pipes
                                               ┌──────────▼───────────────┐
                                               │  hatari (child process)  │
                                               │  --control-socket ...    │
                                               │  stdin / stdout / stderr │
                                               └──────────────────────────┘
```

### 3.1 Process supervisor

`Supervisor` owns exactly one Hatari child per server session.

* Resolves the binary/profile, merges defaults, and spawns with
  `SDL_VIDEODRIVER=dummy` unless `display=true`.
* Creates the private control socket before launch, then accepts Hatari's
  connection.
* Captures stdout/stderr separately (stderr carries debugger/log output;
  stdout carries TOS console output) and owns the stdin pipe used by the
  debugger prompt.
* Tracks `starting`, `running`, `remote_paused`, `at_prompt`, `exited`,
  `crashed`, and unresponsive states; exposes `status()` with pid, exit code,
  program exit code/reason, socket, last error/exception, and ring cursors.
* Terminates gracefully (debugger `quit` → `SIGTERM` → `SIGKILL`) and removes
  the socket/runtime directory.

### 3.2 Command routing

`dbg_command(channel="auto")` and execution helpers select the channel from the
state:

| State | Channel | Typical commands |
| --- | --- | --- |
| `running` | control socket | `hatari-debug`, `hatari-event`, `hatari-option`, `hatari-shortcut` |
| `remote_paused` | none (SIGSTOP) | unavailable; call `emu_resume` |
| `at_prompt` | stdin | debugger reads, `step`, `next`, `cont`, `quit`, `reset` |
| `exited` / `crashed` | none | lifecycle calls only |

A single async lock serializes commands because Hatari processes one command
buffer at a time and its debugger is not re-entrant. Commands that leave the
debugger (`cont`, `quit`, `reset`, DSP continue) deliberately do not queue a
sentinel.

### 3.3 Stream demuxer

A background reader per pipe:

* splits stderr into lines and classifies them (log, debugger, trace,
  breakpoint, exception, NatFeats, sentinel);
* records bounded ring buffers for `log_read`/`console_read` and late
  diagnostics;
* surfaces asynchronous events (breakpoint hit, exception, debugger entry,
  program exit) without blocking tool handlers.

## 4. Response correlation

Hatari does not frame debugger replies, so a reply is delimited with an echo
sentinel of the form `__HMCP_<32 hex>__` (a `uuid4().hex` value; heartbeat and
prompt-probe variants use `__HMCP_HB_` and `__HMCP_PROBE_`):

1. send the debugger command;
2. immediately send `echo <sentinel>` on stdin, or
   `hatari-debug echo <sentinel>` on the control socket;
3. collect stderr lines until the sentinel appears;
4. strip sentinel/echo noise and return the rest.

The same mechanism works on stdin for commands that stay at the prompt. Do
**not** queue a sentinel after `cont`, `quit`, `reset`, or DSP continue: those
leave the debugger and the stale line would be consumed on the next entry.

Known limitations:

* log/trace lines can interleave with debugger output; they are classified but
  cannot always be attributed;
* output produced asynchronously after the sentinel (breakpoint hits,
  exception dumps) is delivered as an event, not part of the reply;
* `cont`/`quit`/`reset` and commands that hit a breakpoint never reach the
  sentinel — handlers time out or use the event stream;
* very large dumps may contain a line identical to the sentinel;
* there are no protocol error codes: success is inferred from the text and the
  sentinel.

## 5. Scenario and batch path

`tools/automation.py` wraps `services/scenario.py`; `scenario_cli.py` exposes
the same engine from the command line (`validate`, `run`, `list-corpus`,
`repeat`, `replay`).

* Scenarios are JSON (YAML only when optional PyYAML is installed), schema v1.
  Validation rejects unknown keys, duplicate step ids, undeclared `${VAR}`
  references, unsafe Hatari options, path escapes, oversized matrices, and
  non-deterministic case ids.
* A run executes each case against its **own MCP stdio server/emulator**; this
  is a real integration path, not a mock. Steps may call only a conservative
  tool allowlist. `dbg_command`, `ahcc_*`, `scenario_*`, `emu_set_option`,
  `emu_set_path`, memory/state writers, trace/profile writers and recording
  shortcuts are denied.
* Artifacts are written atomically with a manifest and checksums, plus
  `report.json` and `junit.xml`. Teardown always runs; `timeout_ms` bounds the
  run and `jobs` (1..8) controls parallelism.
* External corpus trees (Okami-Shell, FreeMiNT, GodLib) are discovered and may
  be staged into a per-case `/tmp` workspace, but are never modified or copied
  into the repository. `run_prg` rejects protected corpus/repository roots as
  workspaces. Only workspaces under the system temp root (normally `/tmp`) are
  mounted writable; every other allowed workspace is mounted with
  `--protect-hd on`, regardless of OS permissions.
* Replay recreates a recorded `replay/` bundle and compares a final
  decoded-RGBA checkpoint. Fidelity is always labeled approximate: there is no
  frame-locked input or virtual clock without the unwired P7.4/P7.5 protocol
  work.

The P8 `labs/` package is separate from this path. It is non-shipping, outside
`src/`, gated by `HATARI_MCP_LABS=1`, and its verdicts are mostly
INCONCLUSIVE/narrow rather than production features.

## 6. Single-directory and AHCC constraints

* `emu_start` maps **at most one** host directory to one GEMDOS drive (`C`-`Z`)
  via `--harddrive`, with case/time/conversion and write-protect options.
  There is no multi-root workspace in one instance.
* `run_prg` uses the program's parent directory or an explicit `workspace` as
  `C:`; it rejects protected corpus/repository roots. Only temp-root workspaces
  (normally `/tmp`) are writable; all other allowed workspaces get
  `--protect-hd on`. `workspace_info.writable` reports this policy, not OS
  permissions. Autostart is a relative guest path.
* `ahcc_compile` compiles **one `.c` file** in a separate isolated headless
  Hatari instance by running `scripts/ahcc-cli.py` as a child process. It does
  not use the MCP supervisor and does not share its emulator state. Output is
  always restricted to `mcp/` or `/tmp`; `HATARI_MCP_BUILD_DIR` only selects
  the default directory, and that default must also be under those roots.
* `ahcc_gui_start` needs an X11/Wayland display, refuses to disturb an already
  running instance, and copies `AHCCST.BIN` into a temp workspace before
  autostarting `AHCCST.PRG`.

## 7. Security model

The debugger is equivalent to arbitrary code execution as the invoking user: it
can read/write guest memory, load files, and run host commands via
`setopt`/`parse`/`logfile`. Therefore:

* the transport is stdio by default; the server does not listen on TCP;
* the control socket lives in a private directory (`0700`) with a random name,
  is unlinked on exit, and is never world-writable;
* the server refuses to attach to an existing Hatari process;
* scenario and AHCC paths are strictly confined; `run_prg` rejects protected
  corpus/repository roots and mounts every other allowed workspace outside the
  temp root with `--protect-hd on`. Other path arguments are resolved but
  **not** globally sandboxed — the debugger escape hatch remains privileged;
* cartridge images are privileged guest code that runs before TOS;
* NatFeats is off by default so guests cannot forge diagnostic/log text;
* destructive tools (`quit`, reset, memory/state writes, media changes) carry
  tool annotations. The scenario step allowlist is the only server-side
  gating; a general server-side allowlist mode is not implemented (roadmap
  P1.26 remains unchecked).

## 8. Error handling and timeouts

* Every awaited command has a per-command timeout (short for debugger reads,
  10 s for state/media operations, 30 s for boot/waits, longer for AHCC and
  scenarios).
* Timeout → cancel the wait and return an MCP tool error with a hint that
  Hatari may be stopped at a breakpoint. Direct Python callers can read the
  collected lines from `HatariError.raw`; `guarded` does not forward `raw`,
  `detail` or `extra` to MCP clients.
* If the socket closes or Hatari exits, in-flight calls fail with
  `emulator_gone` and the supervisor transitions to `exited`/`crashed`.
* Parsing is best-effort: tools return `raw`/`lines` alongside structured
  fields when the format is unknown.

## 9. Testing strategy

* **Unit** — command builders, sentinel demuxer, output parsers, profile
  merging, frame decoding, scenario schema/artifacts/report/security.
* **Integration** — launch Hatari with an EmuTOS image and fixtures; exercise
  boot, screenshot, breakpoint, step, memory read/write, state save/load,
  input injection, and the real scenario runner.
* **Protocol/E2E** — drive `src/` through the MCP stdio protocol with a real
  client and assert tool results, capability modes and notifications.
* **Corpus** — discover external trees and run corpus scenarios without
  writing into them; skip cleanly when prerequisites are absent.
* **Labs** — guarded `tests/labs/` checks proving lab code stays non-shipping,
  gated, and outside the server tool set.

Fixture fetch/generation helpers under `scripts/` are planned and not present
yet; fixtures must currently be produced manually. ROMs are never committed.

## 10. Known limitations and fallbacks

* No native watchpoints; both watchpoint tools report `supported=false`.
* No framed protocol handshake wired; capability reporting is legacy/banner-only.
* No absolute mouse positioning; relative `mousemove` only.
* Joystick is keyboard-emulated; no direct axes/buttons.
* DSP tools require a Falcon build with DSP emulation.
* HD mount and cartridge insert/eject require a restart; the live session is not
  hot-swapped. There is no runtime cartridge eject.
* AVI/audio recording use blind toggles (`recanim`/`recsound`) with no state
  query. Start tools may stop an externally started recording; stop tools only
  act on recordings the server tracked and refuse when the state is unknown.
* `dbg_trace_start(file=...)` is unsupported; `--trace-file` is launch-time only.
* Input and screenshot stability polling are host-timed, not VBL/frame-locked.
