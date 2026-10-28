# Program 2c — Game Crash Detective

A host-side Tkinter application for Atari ST games and PRGs. It launches one
headless Hatari instance over MCP, watches the boot, classifies what it saw,
and — on failure — writes a self-contained triage bundle with screenshots,
hashes, logs, debugger context and ranked evidence-based hypotheses.

The shared foundation (`../common`) is used unchanged:

* `GameSession` launches TOSEC `.zip`/`.st`/`.msa` disks and supplies the
  boot/visible-frame classification.
* `DebugSession` stages PRGs in a space-free GEMDOS workspace.
* `Catalog` scans the local corpus on a background thread.
* `TaskRunner` keeps MCP work off the Tk thread.
* `Config` persists learned launch settings.

## Run

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project/examples/programs/crash_detective
./run.sh
```

The wrapper selects `mcp/.venv/bin/python`, sets `PYTHONPATH` and
`HATARI_MCP_REPO`, verifies `DISPLAY`, and forwards arguments. Equivalent form:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python mcp/project/examples/programs/crash_detective/app.py
```

Useful options:

```bash
# use a private state dir (safe for experiments/tests)
mcp/.venv/bin/python mcp/project/examples/programs/crash_detective/app.py --state-dir /tmp/crash-detective

# extra corpus roots
mcp/.venv/bin/python mcp/project/examples/programs/crash_detective/app.py --games /path/to/other/disks

# headless window smoke
xvfb-run -a mcp/.venv/bin/python mcp/project/examples/programs/crash_detective/app.py --no-scan --autoclose-ms 1500

# module form
cd mcp/project/examples/programs && ../../../.venv/bin/python -m crash_detective --no-scan
```

TOS is auto-discovered (`~/Projects/Hatari/tos`, `HATARI_TOS` overrides).
The default corpus is the TOSEC Games directory documented in
`examples/programs/README.md`; `HATARI_MCP_GAMES` and `--games` add roots.

## UI walkthrough

1. Pick a disk in **TOSEC corpus** (search by title/publisher/year), or switch
   to **PRG file** and browse to a `.prg`.
2. Choose machine (`st`, `ste`, `tt`, `falcon`), TOS and timeout.
   For a disk that boots to the GEM desktop, optionally enter an **Autorun
   path** (for example `RUNME.PRG` or `FOLDER/RUNME.TOS`). It is passed to
   Hatari as `--auto` on drive A:, is remembered per disk, and is ignored on
   the PRG tab. Custom boot-sector loaders ignore it.
3. Press **Launch**. The live status/log pane follows the emulator, the
   screenshot pane shows the captured frame, and the diagnosis table lists
   hypotheses with confidence.
4. **Abort** force-stops the active run. Every run stops Hatari after evidence
   collection, so failed experiments do not leave an orphan process.
5. Mark the run **success** or **failure**. Marks are appended to
   `outcomes.json`; the automatic detector's category is kept as `detected`.
6. **Copy report** copies the markdown report; **Open bundle** opens the
   bundle directory in the desktop file manager.

“Break on guest exceptions” adds `--debug-except all` to the launch. It is
useful for deliberate crashes but can stop games that use exceptions during
normal copy protection or startup.

## Reproducible examples

These three zipped TOSEC menu disks were verified on this machine with the ST
profile and discovered EmuTOS; they all produce a visible frame in under two
seconds:

| Disk | Expected |
| --- | --- |
| `Automation Menu Disk 270 (1990)(Automation)[STE fixed].zip` | booted, visible menu |
| `Medway Boys Menu Disk 102 (19xx)(Medway Boys)[a].zip` | booted, visible menu |
| `Pompey Pirates Menu Disk 112 (19xx)(Pompey Pirates)(Disk 2 of 2)(Part B).zip` | booted, visible menu |

Search for each title in the app and press Launch. Because the corpus contains
multiple regional/alternate rips, match the full filename shown in the list.

### Deliberately failing PRG

`fixtures/bus_error_loop.prg` is a small ST program that performs a
read-modify-write to an ST hardware register. On real ST hardware (and in
Hatari) the read side raises a bus error. Use the **PRG file** tab, select that
fixture, enable **Break on guest exceptions**, and launch. The run should fail
with a log line containing `Bus Error reading at address ...`; the bundle
contains the register file, PC, disassembly, stack/backtrace and the log line
that produced the hypothesis.

If the exception breakpoint is disabled, the failure is usually still detected
from the same log line, but some builds may instead show a TOS error dialog and
time out. In that case use **Mark run failure** and inspect the screenshot.

## State and triage bundles

Default app state:
`mcp/project/examples/programs/state/crash_detective/` (override with
`--state-dir` or `HATARI_MCP_EXAMPLES_STATE`).

```
crash_detective/
├── config.json           learned launch records (common.Config)
├── outcomes.json         user-marked success/failure per run
├── last_run.json         latest evidence + classification + hypotheses
├── emulator/<run-id>/    per-run GameSession/DebugSession scratch disks and frames
└── runs/<run-id>/bundle/
    ├── report.md         human-readable report
    ├── report.json       complete evidence (classification + hypotheses)
    ├── README.txt        bundle contents
    ├── manifest.json     size + SHA-256 for every bundle file
    ├── screenshots/      copied BMP frames
    ├── logs/
    │   ├── stderr.log    Hatari/debugger log tail
    │   └── console.log   guest TOS console tail
    └── debug/
        ├── debug.json    registers, exception, stack, backtrace, media status
        ├── disassembly.txt
        └── backtrace.txt (when Hatari produced frames)
```

`verify_bundle(path)` recomputes every checksum and reports missing, extra or
changed files.

## How boot detection works

The app records evidence, then classifies it. A run is never labelled with an
invented root cause.

* **Booted** — a visible frame at or above the non-black threshold, no fatal
  log line, no guest exception. Disk sessions additionally use the common
  `GameSession` success rule (stable/changed visible frame). A visible frame
  with warnings is still booted but lower confidence.
* **Guest exception** — `emu_status.exception`, `dbg_exception_get`, or an
  exception pattern in the log (`Bus Error`, `Address Error`, `Illegal
  instruction`, `Line A/F`, ...).
* **Blank / no visible frame** — BMP sampling found essentially no non-black
  pixels, or repeated captures failed.
* **Stalled boot** — the timeout expired with a non-black but unchanging frame,
  or the session reported `timeout_no_stable_frame`.
* **Console/log error** — fatal-looking lines remain hypotheses because some
  games print harmless warnings.
* **Repeated resets** — two or more cold/warm/hard/soft reset lines.
* **Emulator exit / prompt / unresponsive** — the status machine left the
  running state, entered the debugger, or missed heartbeats.

Hypotheses carry a confidence between 0 and 1, the exact observations that
produced them, and a reminder that they are not confirmed. Typical hypotheses
are guest boot crash, missing/corrupt/non-bootable disk, blank frame, stalled
boot, TOS/machine mismatch, unusual resolution, GEMDOS/PRG autostart config,
emulator exit and capture failure.

## Tests

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python -m pytest mcp/project/tests/examples/crash_detective -ra -q
```

* Unit tests cover classification, hypothesis ranking and confidence,
  manifest/checksum integrity, outcome persistence and catalog filtering.
* The live smoke skips cleanly unless a Hatari binary, EmuTOS/TOS and a TOSEC
  zip are present. It launches a real zipped disk, records evidence, stops and
  checks the emulator PID is gone.
* The GUI smoke runs the app under `xvfb-run -a`, builds the window with
  `--no-scan`, and exits via `after()`.

## Limitations

* Disk success follows the common foundation's visible-frame rule. A disk that
  falls back to the GEM desktop can look “booted”; mark it failure and inspect
  the screenshot/log. The app does not OCR the screen.
* Hypotheses are heuristics over observable evidence. A black screen can be a
  video mode, a TOS mismatch, a disk loader waiting on input, or a crash — the
  bundle shows which signals were actually present.
* PRG autostart uses Hatari's GEMDOS workspace. A PRG that expects a specific
  drive letter, current directory or cartridge hardware may need extra args.
* Disk autorun uses Hatari's `--auto` on drive A: and requires TOS 1.04 or
  newer. It relies on the GEM desktop, so custom boot-sector loaders ignore it;
  relative paths are anchored to `A:\`.
* Debugger reads can fail while Hatari is running or already exited; those
  failures are recorded in `debug.errors` rather than crashing the run.
* The FR/DE/UK TOS variants, hard-disk installs, STX copy protection and
  multi-disk swaps are out of scope for this program.

## Known common/MCP quirks found while building this

* `emu_status()["exception"]` is populated from supervisor log text. The
  supervisor now recognises Hatari's `WARN : Bus Error reading ...` form as
  well as `Bus error exception`. This program still scans the raw log tail for
  older servers and other exception spellings.
* Debugger reads while Hatari is running can time out. The crash detective
  attempts a best-effort `ensure_prompt()` before reading registers,
  disassembly and the stack, and records failures instead of assuming values.
* `screen_frame_capture` writes BMP files; Tk 8.6 cannot open BMP directly, so
  the preview converts the capture to PPM in memory/temp before loading it.
