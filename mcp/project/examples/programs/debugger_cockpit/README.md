# Debugger cockpit (example program 2a)

A host-side Tkinter interactive debugger for one headless Hatari instance.  It
uses the existing `common` package and talks to Hatari through the MCP server's
typed debugger tools.  All blocking MCP work runs on worker threads through
`common.ui.TaskRunner`; Tk is only touched on the main thread.

## Run

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project/examples/programs/debugger_cockpit
./run.sh
```

The wrapper selects `mcp/.venv/bin/python`, sets `PYTHONPATH` and
`HATARI_MCP_REPO`, verifies `DISPLAY`, and forwards arguments. Equivalent module
form:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
PYTHONPATH=mcp/project/examples/programs \
  mcp/.venv/bin/python -m debugger_cockpit
```

Or from the examples directory:

```bash
cd mcp/project/examples/programs
../../../.venv/bin/python -m debugger_cockpit
```

`Tkinter` is the only GUI dependency.  There is no Pillow/GTK/Qt requirement.

## Panes and controls

* Registers: D0-D7, A0-A7, PC, SR, USP/ISP, with typed register writes.
* Disassembly around PC, refreshed on a stop or step.
* Memory hex dump with address/length reads and validated hex writes.
* Stack words plus Hatari's best-effort A6 back-pointer backtrace.
* Breakpoint list and typed breakpoint editor (`exec`, `mem`, `reg`,
  condition, `:once`, count).
* MCP call/result log and a separate guest console pane.
* Crash-triage pane with the human-readable diagnosis and bundle path.
* Status bar: MCP connection, emulator lifecycle, prompt flag, PC, exception.

Keyboard shortcuts:

| Key | Action |
| --- | --- |
| `F5` | continue |
| `F10` | step |
| `F11` | step over |
| `Ctrl+B` | set breakpoint at the address field, else current PC |

Execution controls are also available from the toolbar and Debug menu.  The
status poll refreshes registers, disassembly, stack and the guest console when
Hatari stops at the prompt or records an exception.

## Bundled sample target

`sample/cockpit_sample.C` is a small AHCC/Atari ST program.  It prints the
addresses of its test state and then waits for the host to write `0xC0DE` to
`GO_FLAG`, so there is no race to attach:

* `GO_FLAG` arms the tick loop.
* `TICK_FN` is a stable place for a breakpoint.
* `COUNTER` changes on every tick and is visible in the Memory pane.
* `CRASH_FLAG` forces the deliberate illegal-instruction path.
* Left alone, it performs 12 ticks and then deliberately executes an illegal
  instruction, giving the triage pane a real exception to capture.

The **Build sample** button calls the MCP `ahcc_compile` tool and writes
`COCKPIT.TOS` under the cockpit state directory.  If the MCP tool is not
available, the app falls back to `mcp/project/scripts/ahcc-cli.py`.  To build it
manually:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python mcp/project/scripts/ahcc-cli.py \
  mcp/project/examples/programs/debugger_cockpit/sample/cockpit_sample.C \
  -o /tmp/COCKPIT.TOS
```

Then use **Load PRG...** and pick `/tmp/COCKPIT.TOS`, or use **Load sample**
after the in-app build.

Suggested sample exercise:

1. Build and load the sample, then **Enter debugger** (the `pc > $0`
   temporary-breakpoint workaround is implemented by `DebugSession`; entering
   the debugger also enables Hatari's CPU-exception debugger).
2. Refresh the guest console and read the printed addresses.
3. Set a breakpoint at `TICK_FN`, write `c0de` to `GO_FLAG` (Memory pane),
   then continue.  The stop creates a triage bundle as proof.
4. Step, read `COUNTER` memory, write `0001` to `CRASH_FLAG`, continue, and
   inspect the exception bundle.
5. Use the console with `cpureg`, `memdump`, `disasm`, `evaluate`, `symbols`,
   `backtrace` or bare `breakpoint` if you need a raw read-only view.

## Crash triage

On a stop after continue/run-to, and on any exception, the cockpit captures:

* status/exception payload from `emu_status` and `dbg_exception_get`;
* registers;
* disassembly around PC;
* stack words and the best-effort backtrace;
* guest console tail and Hatari/debugger log tail;
* a BMP screenshot.

The bundle is written atomically enough for inspection under:

```text
<state>/crash-bundles/<timestamp>-<reason>/
  report.md
  evidence.json
  registers.txt
  disassembly.txt
  stack.txt
  console-tail.txt
  log-tail.txt
  screenshot.bmp
```

The state root is `mcp/project/examples/programs/state/debugger_cockpit` by
default.  Override it with `HATARI_COCKPIT_STATE` or `--state-dir`.

The diagnosis is deliberately evidence-bound.  It distinguishes a recorded
Line-F/Line-A/illegal/privilege/bus/trace exception from a plain breakpoint
stop, flags odd PCs and missing evidence, and says explicitly when the capture
cannot prove a root cause.

## Raw console safety

`dbg_command` is privileged.  The cockpit only forwards verbs on a read-only
allowlist (`cpureg`, `memdump`, `disasm`, `evaluate`, `symbols`, `backtrace`,
`find`, `history`, `info`, and bare `breakpoint` for listing).  It refuses
execution control, memory/state writes, file scripts and command chaining with
an explicit reason.  Every allowed command is logged with a warning; mutating
operations should go through the typed controls so the cockpit can capture
evidence.

## Tests

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python -m pytest -q mcp/project/tests/examples/debugger_cockpit
```

* Unit tests: address/value parsing, hex normalization/dumps, breakpoint and
  register command construction, safe console routing, triage diagnosis and
  bundle layout.
* Live smoke: starts EmuTOS, enters the prompt, reads registers, sets and hits
  a one-shot breakpoint, steps, continues, stops, and asserts the MCP server
  and its emulator descendants are gone.
* Extended live sample test: builds the bundled C target through the MCP AHCC
  tool, arms its flags, and verifies the deliberate exception path.
* GUI smoke: `xvfb-run -a` builds the full window and exits via `after()`.

## Local workarounds for MCP/Hatari quirks

* `:quiet` breakpoints are invisible to the supervisor's stop detector.  The
  cockpit therefore always sets non-quiet breakpoints for run-to and one-shot
  stops; otherwise Hatari stops while `emu_status` keeps reporting `running`.
* `--debug-except` alone only changes Hatari's saved exception mask.  The live
  mask is enabled by `--debug`, but doing that at launch stops on EmuTOS' early
  illegal-instruction probe.  The cockpit enables the mask at the debugger
  prompt with `setopt --debug-except all` followed by `setopt --debug`.
* `console_read` includes the debugger's own stdin echoes (`> cpureg`,
  `loadbin`, ...), so the guest-console pane is noisy after typed operations.
  The MCP log pane is the authoritative call/result record.

## Limitations

* Only one emulator instance can be controlled by one MCP server.
* Hatari's stock debugger has no enable/disable breakpoint and no `>=`
  comparison; the UI exposes only the supported comparisons and delete/re-add.
* Step and step-over require the debugger prompt; the cockpit returns a clear
  error instead of pretending they work while running. Commands that need the
  prompt can time out while the guest is running.
* There is no direct "enter debugger now" MCP tool; the cockpit uses a temporary
  `pc > $0` breakpoint through `DebugSession.ensure_prompt()`.
* The live MCP schema takes debugger addresses as strings; the UI normalizes
  typed decimal/hex values to `0x...` before sending them.
* Stock Hatari has no safe watchpoints, so the cockpit does not offer them.
* The backtrace is a best-effort A6 walk, not compiler-aware unwinding.
* Raw console commands are intentionally restricted; use the typed controls.
* The screenshot preview uses Tk's PNG support.  Triage always stores a BMP
  proof file, which Tk does not preview without an external decoder.
* Sample construction depends on the local AHCC 6.0 install and Hatari; when
  either is absent the MCP build fails and the app reports the `ahcc-cli.py`
  fallback command.
* `run.sh` requires a working `DISPLAY`; for headless use, run it through
  `xvfb-run -a`.
