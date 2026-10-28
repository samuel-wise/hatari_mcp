# Time-travel debugger (program 2b)

A Tkinter host application that drives one headless Hatari instance through
the Hatari MCP server. It captures periodic checkpoints, restores them on
demand, and offers a deliberately **approximate** replay comparison.

## Run

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project/examples/programs/time_travel_debugger
./run.sh --sample
```

The wrapper selects `mcp/.venv/bin/python`, sets `PYTHONPATH` and
`HATARI_MCP_REPO`, verifies `DISPLAY`, and forwards arguments. Equivalent form
from the repository root:

```bash
PYTHONPATH=mcp/project/examples/programs \
  mcp/.venv/bin/python -m time_travel_debugger --sample
```

Useful options: `--prg PATH`, `--tos PATH`, `--machine st|ste|tt|falcon`,
`--state-root DIR`, `--retention 24`, `--memory 0x70000:64`.

Without `--sample`, use **Run bundled sample**, **Load PRG...**, or **Boot
EmuTOS** in the toolbar.

## What it does

* Boots EmuTOS or autostarts a PRG/TOS through `common.debug_session`.
* Captures a checkpoint at a host-time cadence or every N VBL frames. A
  capture stops at the debugger prompt and stores:
  * `dbg_state_save` state file (`state.snap`),
  * PNG screenshot and its SHA-256,
  * normalized M68k registers,
  * configurable guest memory regions and their SHA-256 values,
  * guest VBL, host timestamp and capture metadata.
* Persists a timeline under the examples state directory:
  `state/time_travel_debugger/<session>/timeline.json` plus per-checkpoint
  directories.
* **Rewind** loads a checkpoint with `dbg_state_load` while the debugger is
  stopped, then shows live registers, disassembly, memory and the screenshot.
* Shows a timeline list and slider, before/after screenshots, register diff,
  memory byte-range diff and a log.
* **Replay** reloads a start checkpoint, continues or single-steps toward a
  recorded target, captures new replay checkpoints, and reports the first
  divergence in PC, registers, memory, or framebuffer hash.
* Prunes old checkpoints after each capture (`Keep` spinbox; 0 = unlimited).

## Bundled sample

`sample/tt_sample.c` paints the ST low-resolution framebuffer every VBL and
updates a 16-byte marker at `0x00070000`. The default memory region is
`0x70000:64`, so both screen and memory change over time.

`Run bundled sample` builds `sample/tt_sample.prg` with the MCP `ahcc_compile`
tool when it is missing. To build it explicitly:

```bash
PYTHONPATH=mcp/project/examples/programs \
  mcp/.venv/bin/python mcp/project/examples/programs/time_travel_debugger/build_sample.py --force
```

## Tests

```bash
cd mcp/project
../.venv/bin/python -m pytest tests/examples/time_travel_debugger -q
xvfb-run -a ../.venv/bin/python -m pytest \
  tests/examples/time_travel_debugger/test_gui_smoke.py -q
```

The live smoke starts a real EmuTOS instance, captures two checkpoints, loads
one, verifies registers/screenshot/memory and stops. It skips when Hatari or
EmuTOS is unavailable. All live artifacts live under pytest's temporary
directory and are removed afterwards.

## Limitations

**Replay is approximate, not deterministic.** Stock Hatari has no frame-locked
input replay (the P7.5 capability is absent), and host-timed captures are not
VBL-exact. A checkpoint restores the guest machine state, but `stateload` does
not undo host-side effects such as disk writes or media changes. A matching
replay only means the captured PC, registers, memory window and framebuffer
hash matched at the sampled points; it is not proof of input-exact determinism.

Capture samples the running machine: `dbg_state_save`, the screenshot,
registers and memory come from moments a few milliseconds apart. They are
consistent enough for inspection and approximate comparison but are not an
atomic frame snapshot.

While running, Hatari's `disasm` command times out, so the engine falls back to
`m68k-linux-gnu-objdump` (or another m68k objdump) over the bytes read from the
checkpoint PC. At the debugger prompt it uses Hatari's own disassembler.

**Known stock/common quirk:** repeated break-in through
`common.debug_session.DebugSession.ensure_prompt()` (a `:once` conditional
breakpoint set while running) can wedge this Hatari build on the second use.
`common` was intentionally not modified. This engine therefore captures and
rewinds while the machine is running, where only `stateload`/state and read
commands are needed. Single-step replay still asks for a prompt; if Hatari
reports the repeat break-in failure, restart EmuTOS or reload a checkpoint and
retry.

Only one emulator instance is controlled by one MCP server. `run.sh` requires a
working `DISPLAY`; for headless use, run it through `xvfb-run -a`.
