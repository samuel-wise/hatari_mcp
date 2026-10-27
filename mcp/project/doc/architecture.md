# Architecture

Status: draft, v1 design.
Related: `doc/adr/0001-language-and-transport.md`, `doc/tools-reference.md`.

## 1. Goal and scope

Give an MCP client (an AI agent) first-class control of one running Hatari
instance:

* lifecycle — launch, pause, resume, reset, quit;
* configuration — machine model, memory, TOS, video, sound, paths;
* input — keyboard, mouse, joystick, inserted key sequences;
* media — floppies, hard-disk images, cartridge, printer/RS232/MIDI;
* observation — screenshots, framebuffer, logs, traces, memory dumps;
* state — save/load snapshots, read/write memory;
* debugging — CPU and DSP registers, breakpoints, stepping, disassembly,
  expression evaluation, symbols, profiling, backtraces.

Out of scope for v1: replacing the Hatari UI, writing a new emulator,
Windows support (the control socket is Unix-only), and distributing ROMs.

## 2. What Hatari already gives us

The design deliberately reuses existing, documented emulator mechanisms. No
private globals and no GDB stub are required for v1.

### 2.1 Control socket (primary channel)

`--control-socket <path>` (Unix only, implemented in `src/control.c`).

* The MCP server creates an `AF_UNIX`/`SOCK_STREAM` socket and listens.
* Hatari connects to it (Hatari is the client).
* Commands are newline-delimited UTF-8, executed by `Control_ProcessBuffer()`.
* The socket is polled from `Control_CheckUpdates()` during the emulation loop,
  so execution is asynchronous relative to the caller.
* One `read()` is assumed to contain whole commands, buffered at
  `MAX_DEBUG_CMD_LEN` (4096 bytes) — callers must keep writes small.

Supported commands (`Control_Usage()`):

| Command | Effect |
| --- | --- |
| `hatari-debug <debugger command>` | `DebugUI_ParseLine()` — full debugger surface |
| `hatari-event <event>` | keyboard and mouse event injection; joystick only indirectly via keyboard emulation |
| `hatari-option <cli options>` | `Change_ApplyCommandline()` at runtime |
| `hatari-enable/disable/toggle <device>` | device toggles |
| `hatari-path <config name> <path>` | runtime path changes |
| `hatari-shortcut <name>` | invoke a UI shortcut action |
| `hatari-stop` / `hatari-cont` | pause/resume the emulation loop |
| `hatari-embed-info` | ask for embedded window size messages |

**Critical limitation:** the socket is bidirectional but carries no command
responses. The only bytes Hatari writes back are window-size messages after
`hatari-embed-info`. Debugger output is *not* sent to the socket.

### 2.2 Command FIFO (fallback)

`--cmd-fifo <path>` (Unix only). Hatari creates and polls a named pipe. It is
one-way and has no connection state, but is simpler to secure and is enough
for fire-and-forget input. Not preferred because device toggles and debugger
use are easier to reason about over a persistent socket.

### 2.3 Debugger line API

`hatari-debug <command>` calls `DebugUI_ParseLine()` (`src/debug/debugui.c`).
Debugger output goes to `debugOutput`, which is **stderr** by default (the
`logfile` command can redirect register/memory/disassembly dumps to a file).
`--parse <file>` runs a command script at startup through the same path.

Debugger commands relevant to the MCP surface:

* generic: `evaluate`, `info`, `history`, `screenshot`, `stateload`,
  `statesave`, `trace`, `logfile`, `setopt`, `reset`, `parse`, `echo`,
  `quit`;
* CPU: `cpureg` (`r`), `memdump` (`m`), `disasm` (`d`), `breakpoint` (`b`),
  `step` (`s`), `next` (`n`), `cont` (`c`), `memwrite` (`w`),
  `loadbin` (`l`), `savebin`, `find`, `profile`, `symbols`, `backtrace`,
  `address` (`a`), `struct`;
* DSP: `dspreg` (`dr`), `dspmemdump` (`dm`), `dspdisasm` (`dd`),
  `dspbreak` (`db`), `dspstep` (`ds`), `dspnext` (`dn`), `dspcont` (`dc`),
  `dspsymbols`, `dspprofile`, `dspaddress`.

**Critical limitation:** while the debugger is at its prompt, the emulation
loop is blocked in `DebugUI_GetCommand()` reading **stdin**, and
`Control_CheckUpdates()` is not called (its only caller is
`GuiEvent_EventHandler()`). Socket commands sent after a breakpoint or
exception are queued but not processed until the debugger exits.

The supervisor therefore needs two command channels:

* the control socket while the CPU is running or remotely paused
  (`hatari-stop`);
* Hatari's stdin pipe while the debugger is at its prompt (breakpoint,
  exception, `--parse`, manual entry).

`step` and `next` return to the prompt and stay on stdin; `cont`, `quit`, and
`reset` leave the debugger and switch back to the socket. Readline may write
its `> ` prompt to **stdout**, which is also the TOS console stream, so a
non-TTY-safe reader (build without readline, or a small patch to use `fgets`
when stdin is not a TTY) is preferred. Polling the control channel from the
debugger loop is the robust upstream fix (Phase 7).

### 2.4 Determinism and headless operation

* `--run-vbls <n>` exits after `n` vertical blanks — useful for scripted runs.
* `--benchmark` disables interactive timing.
* `SDL_VIDEODRIVER=dummy` runs SDL without a window while still emulating and
  rendering the framebuffer, so debugger `screenshot` keeps working.
* Do **not** use `--disable-video` when screenshots or framebuffer reads are
  needed; it skips rendering (`src/sdl/screen.c`).
* `--sound off`, `--confirm-quit off`, `--configfile <path>` avoid dialogs and
  host-state leakage.

## 3. Components

```
┌──────────────────┐   MCP (stdio, JSON-RPC)   ┌──────────────────────────┐
│  MCP client      │ ◄───────────────────────► │  hatari-mcp server       │
│  (AI agent)      │                           │  (Python, src/)          │
└──────────────────┘                           │                          │
                                               │  1. process supervisor   │
                                               │  2. control channel      │
                                               │  3. stream demuxer       │
                                               │  4. tool handlers        │
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

Owns exactly one Hatari child per server session (v1). Responsibilities:

* resolve the binary and profile, merge defaults, spawn with
  `SDL_VIDEODRIVER=dummy` unless a display is requested;
* create the control socket before launch, then accept Hatari's connection;
* capture stdout/stderr separately (stderr carries debugger output; stdout
  carries emulated TOS console output), and own the **stdin** pipe used by the
  debugger prompt;
* enforce a startup timeout waiting for the socket connection;
* terminate gracefully (`hatari-debug quit` → `SIGTERM` → `SIGKILL`) and
  remove the socket file;
* expose `status` (including `running|paused|at_debugger_prompt`),
  `exit_code`, and the last N log lines.

### 3.2 Control channel

A serialized request/response wrapper with two transports selected by state:

* **socket** while emulation is running or remotely paused: `hatari-debug`,
  `hatari-event`, `hatari-option`, `hatari-shortcut`, and the rest of
  `control.c`;
* **stdin** while the debugger is at its prompt: the same debugger command
  language, one line per command, used for breakpoint inspection, `step`,
  `next`, and `cont`;
* a single async lock: Hatari processes one command buffer at a time and its
  debugger is not re-entrant;
* writes are newline-terminated and kept well under 4096 bytes;
* lines beginning with `hatari-debug` are correlated with output (see §4);
* all other commands are fire-and-forget, with errors inferred from the
  stderr stream and the emulator's state.

The supervisor tracks the transition into the debugger from parsed stderr
(breakpoint/exception messages) and out of it when `cont`/`quit`/`reset`
succeeds. A wrong guess must fail with `debugger_state_unknown`, never hang.

### 3.3 Stream demuxer

A background reader per pipe that:

* splits stderr into lines and classifies them (debugger output, log message,
  trace, breakpoint hit, sentinel);
* records a bounded ring buffer for log retrieval and for late diagnostics;
* pushes asynchronous events (breakpoint hit, exception, debugger entry) to
  the MCP layer so they can be surfaced as notifications/resources;
* never blocks the tool handler.

### 3.4 Tool handlers

Thin, typed wrappers over the channel: validate arguments, render a command,
await completion, parse the text result into structured JSON, and map failures
to MCP errors. See `doc/tools-reference.md`.

## 4. Response correlation (v1 without an upstream patch)

Hatari does not frame debugger replies, so a reply is delimited with a sentinel:

1. send `hatari-debug <command>`;
2. immediately send `hatari-debug echo __hatari_mcp_done_<uuid>__`;
3. collect stderr lines until the sentinel line appears;
4. strip sentinel/echo noise and return the rest.

`echo` writes to `debugOutput` (stderr) and is processed in order with the
preceding command, so the sentinel terminates that command's output. The same
mechanism works on the stdin channel for commands that stay at the prompt
(`step`, `next`, register/memory reads). Do **not** queue a sentinel after
`cont`, `quit`, or `reset`: those leave the debugger, and the stale line would
be consumed on the next entry. The supervisor must select the channel from the
debugger state.

Known limitations, to be fixed by the patch in `patches/`:

* log/trace lines can interleave with debugger output; they are classified but
  cannot always be attributed;
* output produced asynchronously after the sentinel (breakpoint hits, exception
  dumps) is not part of the reply and must be delivered as an event instead;
* `cont`/`quit`/`reset` and commands that hit a breakpoint never reach the
  sentinel — handlers must time out or use the event stream;
* very large dumps may contain a line identical to the sentinel;
* no error codes: success is inferred from absence of `ERROR`/`Unknown
  command` and from the sentinel being reached.

## 5. Upstream patch (target state)

`patches/` will carry an opt-in extension to the control protocol:

* framed request/response with a monotonic request id, `OK`/`ERR`, and an
  explicit end marker;
* a protocol version handshake at connect;
* notifications for debugger entry, breakpoint hits and exceptions;
* a binary framebuffer/RAM read path (no hex parsing);
* optional `--headless` that keeps rendering but never opens a window.

The server advertises the capability via `hatari-debug version` / a handshake
and falls back to §4 when running against unpatched Hatari.

## 6. Security model

The debugger is equivalent to arbitrary code execution as the invoking user:
it can read/write guest memory, load files, and run host commands via
`setopt`/`parse`/`logfile`. Therefore:

* the transport is stdio only — the server does not listen on TCP;
* the control socket lives in a private directory (`0700`) with a random name,
  is unlinked on exit, and is never world-writable;
* the server refuses to attach to an existing Hatari process;
* file paths supplied by the model are resolved and confined to configured
  roots unless the client explicitly opts out;
* destructive tools (`quit`, `reset`, `memwrite`, `statesave` overwrite) are
  marked and can be gated by a server-side allowlist.

## 7. Error handling and timeouts

* Every awaited command has a per-command timeout (default 2 s for debugger
  commands, 10 s for state/media operations, 30 s for boot).
* Timeout → cancel the wait, return an MCP error containing the collected
  output and a hint that Hatari may be stopped at a breakpoint.
* If the socket closes or Hatari exits, all in-flight calls fail with
  `emulator_gone` and the supervisor transitions to `exited`/`crashed`.
* Parsing is best-effort: a tool may return `raw` text alongside structured
  fields when the format is unknown (e.g. trace output).

## 8. Testing strategy

* **Unit** — command builders, sentinel demuxer, output parsers, profile
  merging. No emulator required.
* **Integration** — launch Hatari with an EmuTOS image and a generated
  floppy/HD image from `fixtures/`; exercise boot, screenshot, breakpoint,
  step, memory read/write, state save/load, input injection.
* **Protocol/E2E** — drive `src/` through the MCP stdio protocol with a real
  client and assert tool results and notifications.
* **Regression** — replay recorded stderr logs through the demuxer.

Fixtures are fetched or generated by `scripts/`; ROMs are never committed.
