# Hatari MCP example programs

Host-side Python 3.12 Tkinter applications built on the Hatari MCP server.
They run on the host, drive one Hatari instance over MCP stdio, and use the
local Atari disk corpus. The game launcher displays Hatari by default; the
debugger and crash tools start it headless unless configured otherwise. No
Atari-side build tools are required.

## Requirements

* Python 3.12 (the server is installed in `mcp/.venv`)
* Tkinter (`python3-tk`) for the GUI programs
* A graphical display (`DISPLAY`); TigerVNC, a local desktop, or Xvfb for
  headless automation
* Hatari built or installed where the MCP server can find it
* A valid TOS image (192/256/512 KiB; discovered under `~/Projects/Hatari/tos`,
  override with `HATARI_TOS`). ST games default to TOS 1.04; a valid EmuTOS
  `etos192us.img` is a last resort.

Always use the project virtualenv so the `mcp` SDK is available:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python mcp/project/examples/programs/common/smoke.py
```

## State, cache and configuration

The shared foundation is self-contained under `examples/programs/state`
(git-ignored). Override the root with `HATARI_MCP_EXAMPLES_STATE=/path/to/state`.

| Path | Purpose |
| --- | --- |
| `state/config.json` | Atomic JSON config, UI preferences, learned game launch settings |
| `state/metadata/<id>.json` | Sidecar/enrichment metadata and cached art references |
| `state/run/<game>/` | Space-free disk links/extractions and failure frames |
| `state/cache/art/` | Local artwork cache |
| `state/snapshots/` | Debugger state snapshots |

`config.json` is the only source of truth; it is written through a temp file,
fsynced and atomically replaced. Delete it to reset learned behavior.

## Game corpus

Discovery defaults to
`/home/ubuntu/Projects/Hatari/atari_st/archive.org/download/Atari_ST_TOSEC_2012_04_23/Games`.
Set `HATARI_MCP_GAMES` (one or more roots separated by `:`) or add directories
through `Config.add_game_dir()`. Discovery runs on a background thread in
batches, so Tk never waits for 13k files. Zipped TOSEC menu disks are extracted
on demand into `state/run/...`; Hatari never sees a path containing a space.

## Shared package (`common`)

```python
from common import MCPClient, GameSession, Catalog, Config

with MCPClient() as client:
    catalog = Catalog().start_scan()
    session = GameSession(client)
    result = session.start_game(catalog.get_page(limit=1)[0])
    if result.ok:
        print(result.frame_path, result.resolution)
    session.stop()
```

Public modules:

* `mcp_client` — blocking MCP stdio client (`MCPClient`, `MCPToolError`,
  `normalize_tool_result`), one background asyncio loop, bounded shutdown.
* `paths` — repo/TOS/game/state discovery and space-free disk materialization.
* `config` — atomic JSON `Config`, learned per-game launch records.
* `catalog` — background TOSEC discovery, search/pagination, sidecars,
  optional non-blocking enrichment hook.
* `art` — local art cache lookup, optional Pillow for JPEG, deterministic
  placeholder metadata.
* `ui` — `TaskRunner`, `ScrollableList`, `StatusLog`, `ImagePanel`,
  `ShutdownHooks`; importing it creates no Tk root.
* `game_runner` — `GameSession`, boot/failure classification, hot disk swap,
  failure evidence, learned config.
* `debug_session` — `DebugSession` for EmuTOS/PRG runs, registers, memory,
  disassembly, stack, breakpoints, stepping, snapshots and prompt detection.
* `smoke` — CLI proof that starts the MCP server, boots EmuTOS headless,
  captures a frame under `/tmp`, prints status and stops.

## Smoke check

```bash
mcp/.venv/bin/python mcp/project/examples/programs/common/smoke.py
# or
cd mcp/project/examples/programs && ../../../.venv/bin/python -m common.smoke
```

## Known MCP quirks (found while building this foundation)

* `emu_start(..., workspace=...)` previously defaulted `gemdos_time="off"`,
  which Hatari 2.6 rejects; the server default is now `"atari"`. The examples
  still pass it explicitly for compatibility with older servers.
* Debugger address arguments (`dbg_disassemble`, `dbg_breakpoint_set`) are
  string-typed in the live schema even though the docs accept integers.
  `DebugSession` converts integers to `0x...` strings.
* `emu_status()["tos"]` reports the server's discovered default TOS, not the
  image passed to `emu_start`; use your own launch record for provenance.
* Hatari's control parser does not quote paths: always use
  `paths.materialize_disk()` / `make_space_free()` for disks and
  `emu_set_option`.
* Stock Hatari comparison syntax has no `>=`; use single `<`, `>`, `=`, `!`
  breakpoint comparisons.

## Programs

All four programs are host-side Tkinter applications. They share the `common`
package and drive the same MCP server over stdio. Run them from the repository
root with the programs directory on `PYTHONPATH`:

The easiest path is the `run.sh` script in each program directory. It finds the
repository root, selects `mcp/.venv/bin/python`, sets `PYTHONPATH`, checks that
`DISPLAY` is available, and forwards any options:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project/examples/programs
./game_launcher/run.sh
./debugger_cockpit/run.sh
./time_travel_debugger/run.sh --sample
./crash_detective/run.sh
```

Equivalent explicit-module form from the repository root:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
export PYTHONPATH=mcp/project/examples/programs
mcp/.venv/bin/python -m game_launcher
mcp/.venv/bin/python -m debugger_cockpit
mcp/.venv/bin/python -m time_travel_debugger
mcp/.venv/bin/python -m crash_detective
```

On a headless host, prefix a GUI run with `xvfb-run -a` (or start TigerVNC and
export the matching `DISPLAY`). Each program has its own README with
configuration details, a deeper feature list and its complete limitations.

### `game_launcher/` — Atari ST game launcher

Three-pane browser for one or more TOSEC game directories. It discovers the
catalog in the background, filters by machine, genre and year, shows cached or
generated cover art, and launches a selected disk in Hatari. It selects an
ST/STE/TT/Falcon profile, resolution and TOS, learns the last successful
configuration, records failed configurations with the reason, and hot-swaps a
disk without restarting when the machine/TOS are unchanged. Metadata is
local-first (sidecar JSON plus per-field provenance); online enrichment is
opt-in and off by default.

Limitations: one disk image per archive, multi-disk sets are manual, changing
machine/TOS requires a restart, Falcon only uses TOS 4.x, and JPEG art requires
Pillow (otherwise a deterministic placeholder is shown). One Hatari instance is
managed at a time; launching another game swaps the disk or restarts the
emulator.

### `debugger_cockpit/` — interactive debugger

Multi-pane cockpit with registers, memory, disassembly, stack/backtrace,
breakpoints, triage, screen and MCP/console logs. Supports conditional and
one-shot breakpoints, step/step-over/continue/run-to, register and memory
writes, state save/load, screenshots, automatic refresh on prompt stops, and
timestamped crash bundles with an evidence-based diagnosis. A bundled AHCC
sample demonstrates a real illegal-instruction crash.

Limitations: one emulator per server, stepping requires the debugger prompt,
backtrace is Hatari's A6 walk, the raw console is intentionally restricted, and
stock Hatari has no safe watchpoints. Use non-quiet breakpoints: `:quiet`
suppresses the hit line the supervisor uses to detect a stop. There is no direct
enter-debugger tool; the cockpit uses a temporary `pc > $0` breakpoint, and the
live schema expects debugger addresses as strings (the UI normalizes them).

### `time_travel_debugger/` — checkpoint/replay debugger

Captures checkpoints containing debugger state, screenshot hash, registers,
selected memory regions and VBL, with a persistent timeline and retention
policy. Rewind loads a checkpoint; before/after register, memory and screenshot
views expose changes. Replay can continue to a target, step, and report the
first divergence in PC, registers, memory or framebuffer. A bundled sample is
included.

Limitations: one emulator per server; replay and capture are host-timed, not
frame-locked, so replay is explicitly approximate. State restore does not undo
host-side disk or media changes. Repeated prompt entry can wedge some Hatari
builds; the engine avoids it except for single-step replay.

### `crash_detective/` — automated failure triage

Searches the catalog or accepts a PRG, launches it, and classifies the outcome
(visible boot, exception, blank screen, stall, console/disk error, reset or
emulator exit). It writes a portable failure bundle with report, manifest,
checksums, screenshots, log tails, registers, disassembly, stack, backtrace,
exception, media status and launch configuration, then ranks hypotheses with
confidence and evidence. User outcomes are persisted to improve triage.

Limitations: the launcher records the initial boot frame as a baseline,
requires the frame to change from it, and requires the PC to leave TOS ROM
before declaring success. If the MCP cannot report the PC it falls back to a
longer stable-frame check, so a game that never leaves TOS ROM or a boot screen
that changes without loading a game can still be misclassified. Invalid or
intermediate TOS images are rejected by size. The crash detective still uses
visible-frame classification and can mistake a GEM desktop fallback for a game.
Hypotheses are heuristics, not proof. STX copy protection, multi-disk sets,
hard-disk games and regional TOS variants are out of scope.

## Testing

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python -m pytest mcp/project/tests/examples -q
xvfb-run -a mcp/.venv/bin/python -m pytest mcp/project/tests/examples -q
```

The suite uses the local Hatari build, valid TOS images, and real zipped corpus
disks when available and skips cleanly when they are absent. The first command skips the
time-travel GUI test when `DISPLAY` is unset; the second includes it.

## Known MCP workarounds used by the examples

* `:quiet` breakpoints hide the hit line used for `AT_PROMPT` detection, so the
  cockpit uses non-quiet breakpoints.
* There is no direct "enter debugger now" tool; the examples use a temporary
  `pc > $0` breakpoint through `DebugSession.ensure_prompt()`.
* `dbg_disassemble` can time out while the guest is running; the time-travel
  program falls back to host `m68k-linux-gnu-objdump` for running-state
  disassembly.
* `console_read` includes debugger stdin echoes; the cockpit's MCP log pane is
  the authoritative call/result record.
* Hatari's comparison syntax has no `>=`, and debugger address schemas are
  string-typed; `DebugSession` handles both.
* `emu_status()["tos"]` reports the server-discovered default, not necessarily
  the image passed to `emu_start`.
* The supervisor now classifies `Bus Error reading ...` as an exception; the
  crash detective still scans raw log tails for older servers and other
  exception spellings.

The example state directory (`state/`) is git-ignored; delete it to reset
learned configuration, cached art, timelines and bundles.
