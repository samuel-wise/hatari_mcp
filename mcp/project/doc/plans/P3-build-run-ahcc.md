# P3 — Build/run integration, AHCC matrix, external corpus

Status: partial — AHCC build/run, workspace mapping and corpus harness implemented; Pterm auto-exit and native corpus toolchains remain
Owner: phase implementer (build/run)
Last updated: 2026-10-27
Roadmap: P3.1–P3.13 (inputs: P1.28, P2.15)

## Goal

Collapse the manual "build on the host, launch Hatari, autostart, wait, inspect"
loop into a small number of structured MCP operations, without making
compilation mandatory and without pretending Hatari has capabilities it lacks.

The phase delivers:

1. a workspace/path-translation model for host roots and the single GEMDOS
   directory Hatari actually supports;
2. `run_prg` with symbol autoload, an explicit exit-detection ladder, and a
   crash bundle;
3. an optional, off-by-default build command and a measured fast-restart
   strategy;
4. image builders, host/guest transfer, and machine profiles;
5. a shared AHCC CLI implementation used by both `scripts/ahcc-cli.py` and the
   MCP tool (one implementation, two entry points);
6. an AHCC build matrix and a read-only external-corpus harness for
   Okami-Shell, FreeMiNT, and GodLib.

Gate (roadmap): one tool call runs a freshly built PRG to completion and
returns exit code, console output, and a screenshot; a failing run returns a
crash bundle.

## Dependencies

Hard dependencies — do not start the dependent task until these are usable:

| Dependency | Why |
| --- | --- |
| P0.2, P0.3, P0.3a, P0.4 | Supervisor, dual command channel, debugger-input proof, stderr demuxer. |
| P0.6 | Profile merge and validated paths. |
| P1.2, P1.3, P1.4 | Machine/memory/TOS options, GEMDOS host directory, autostart path mapping. |
| P1.7, P1.9, P1.10 | NatFeats stderr/exit, run-to-exit helper, exception surfacing. |
| P1.11, P1.15–P1.17 | Symbols and breakpoint/step/wait APIs used by crash capture. |
| P1.20 | State save/load for optional crash snapshots. |
| P1.25–P1.27 | Path confinement, destructive annotations, hard limits. |
| P1.28 | Existing `ahcc_compile` tool; P3 extends it rather than forking it. |
| P2.1, P2.4, P2.6 | Frame-targeted capture and screenshot artifacts for run results. |

Soft dependencies:

* P1.29 / P2.15 for the AHCC GUI matrix case; the CLI matrix must pass without
  GUI automation.
* P2.7 for baseline comparison of run screenshots.
* P2.8 for AVI capture in corpus smoke tests.
* P3.11 is research-only and must not block P3.12/P3.13.

External prerequisites (discovered, never copied into the repository):

* `build-install/bin/hatari` and `hatari-prg-args`;
* EmuTOS or a user-supplied TOS image;
* `mcp/project/tools/ahcc/ahcc-68000/AHCCST.BIN` (CLI `AHCCST_P.TTP`, GUI
  `AHCCST.PRG`);
* read-only corpus roots `~/Projects/Hatari/{Okami-Shell,freemint,godlib}`
  (override with `HATARI_CORPUS_ROOT`);
* for image builders: `mtools` (`mformat`, `mcopy`), `mkdosfs`, `sfdisk`,
  `python3`;
* for native corpus builds: `m68k-atari-mint-gcc`, `vasmm68k_mot`,
  `m68k-atari-mint-ar` (currently absent on this host; tests must skip).

Blocking relationship: P3.2 can be implemented against P1.4/P1.9, but its
acceptance test needs P1.10 and P1.11. P3.13 is blocked on P1.28 and on the
AHCC multi-file spike.

## Deliverables and file ownership

All implementation paths are under `mcp/project/src/hatari_mcp/`.

| Roadmap | Deliverable | Primary path | Owner role |
| --- | --- | --- | --- |
| P3.1 | Workspace model, roots, guest mapping, path translation | `workspace.py`, `guestpath.py` (extend P1.25/P1.3) | workspace implementer |
| P3.2 | `run_prg` orchestration and result schema | `run.py`, `tools/run.py` (extend P1.9/P1.20) | run-loop implementer |
| P3.3 | Exit-detection ladder | `run.py` (exit-ladder section), `natfeats.py` (P1) | run-loop implementer |
| P3.4 | Crash bundle writer | `crash.py`, `tools/run.py` | run-loop implementer |
| P3.5 | Symbol autoload index and address→symbol resolution | `debug/symbols.py` (extend P1.11) | debugger implementer |
| P3.6 | Optional build command | `build_command.py`, `tools/build.py` | build implementer |
| P3.7 | Fast-restart strategy and measurement | `run.py` (restart section), `tests/integration/test_restart.py` | run-loop implementer |
| P3.8 | Floppy/HD image builders | `media/images.py` | media implementer |
| P3.9 | Host↔guest transfer | `transfer.py`, `guestpath.py` | workspace implementer |
| P3.10 | Machine profiles and capability expectations | `machines.py`; extend `config/profiles/*.toml` | config implementer |
| P3.11 | DWARF/source-map research spike | `debug/dwarf.py` (research, optional) | research implementer |
| P3.12 | External corpus discovery, manifests, adapters | `corpus/discover.py`, `corpus/manifest.py`, `corpus/runner.py`, `corpus/adapters/{okami,freemint,godlib}.py` | corpus implementer |
| P3.13 | AHCC multi-file/matrix extension and shared runner | `ahcc/project.py`, `ahcc/diagnostics.py`, `ahcc/runner.py`, `ahcc/matrix.py`, `tools/ahcc.py` (extend P1.28) | AHCC implementer |
| P1.28/P3.13 | Thin standalone wrapper, no duplicate logic | `scripts/ahcc-cli.py` (existing P1 wrapper; verify no logic drift) | AHCC implementer |

Supporting test ownership (planned, not created by this doc):

* `tests/unit/test_workspace_paths.py`, `test_exit_ladder.py`,
  `test_crash_bundle.py`, `test_symbols.py`, `test_images.py`,
  `test_ahcc_project.py`, `test_ahcc_diagnostics.py`,
  `test_corpus_manifest.py`.
* `tests/integration/test_run_prg.py`, `test_exit_ladder_hatari.py`,
  `test_ahcc_matrix.py`, `test_corpus_harness.py`,
  `test_restart_benchmark.py`.
* `tests/fixtures/programs/` source for hello, NatFeats exit, Pterm/Pterm0,
  illegal instruction, infinite loop, multi-file, and watchpoint programs.

## Design / constraints

### P3.1 Workspace, drives, and path translation (important scope correction)

Stock Hatari has **one** GEMDOS host-directory slot:
`ConfigureParams.HardDisk.szHardDiskDirectories[0]` is written by
`--harddrive`, and `--gemdos-drive` assigns that single directory to one drive
letter (`C`–`Z`, or `skip`). The roadmap's "one or more host roots" must
therefore be implemented as:

* **primary root** (`mode = rw`): the only root directly exposed through
  `--harddrive`; default guest drive `C`;
* **additional roots** (`mode = ro`): not mounted as additional GEMDOS
  directories (stock Hatari cannot do that). P1's wording "P3 expands to a
  multi-root model" must be read as staged/read-only roots, not multiple
  direct mounts. They are consumed by
  (a) copying/staging selected files into a read-only subtree of the primary
  root, or (b) building a floppy/HD image with P3.8 and mounting that. Direct
  multi-directory mounts are a `[P]` item, not a P3 deliverable.

Path rules:

* canonicalize host paths with `Path.resolve()` and reject symlinks that escape
  a configured root; reject `..` traversal after canonicalization;
* translate host path → guest `X:\DIR\FILE.EXT` and guest → host with the
  configured drive letter; reject a guest path on an unmounted drive;
* apply GEMDOS limits (8.3 names where applicable, invalid characters
  `< > : " / \ | ? *`, reserved DOS device names, trailing dots/spaces), and
  report a collision instead of silently overwriting;
* honor `--gemdos-case`, `--gemdos-time`, and `--gemdos-conv`; document that
  host-case and guest-case are not a bijection;
* all writes are atomic (temporary file in the same directory, then rename);
* a run holds a workspace lock while the guest may be writing; no host-side
  mutation of the same directory during the run unless the caller opts in.

### P3.2 `run_prg`

Typed inputs: `prg` (host path or guest path), `profile`, `args`, `tos`,
`timeout_ms`, `symbols` (`auto|path|none`), `crash_bundle` (bool/path),
`screenshot` (`none|on_exit|on_crash`), `build` (optional named build target),
`restart` (`relaunch|reset`), `allow_restart`.

Sequence:

1. validate profile, TOS, PRG, and symbol file inside configured roots;
2. stage the PRG into the primary GEMDOS root if it is outside it; record
   source path and SHA-256 in the result;
3. choose/validate the drive letter and compute the guest path;
4. start or restart a supervisor-owned instance with `--harddrive`,
   `--gemdos-drive`, `--auto <guest path>`, `--symload exec`, `--natfeats on`,
   `--conout 2`, `--fast-boot on`, `--timer-d on`, and a deterministic RTC
   where the profile requests it;
5. subscribe to breakpoint/exception/NatFeats/exit events before resuming;
6. run the exit ladder (P3.3), collecting console and demuxed stderr;
7. on exception, capture the crash bundle (P3.4) while at the debugger prompt;
8. return structured data; never return a bare string.

Result schema (stable, versioned):

```json
{
  "ok": true,
  "run_id": "...",
  "exit_code": 0,
  "exit_source": "natfeats|pterm|pterm0|console|process|timeout",
  "crashed": false,
  "duration_ms": 1234,
  "console": "...",
  "stderr_tail": "...",
  "symbols_loaded": true,
  "symbol_source": "embedded|sym|none",
  "screenshot": "artifacts/.../screen.png",
  "crash_bundle": null,
  "artifacts": ["artifacts/.../run.json"],
  "raw": "..."
}
```

Do not run `--auto` against a PRG that is outside the exposed drive; do not
copy proprietary binaries into the repository.

### P3.3 Exit ladder

Ordered, with the first conclusive signal winning:

1. **NatFeats `NF_EXIT`** — parse `NatFeats: exit(N)` / the NatFeats event.
   This is the only reliable guest exit code. It requires a cooperative
   program; otherwise continue.
2. **Pterm / Pterm0** — use the documented trace path (`--trace gemdos`,
   `hatari-debug trace gemdos`) and parse the GEMDOS trace line for
   `GEMDOS 0x00 Pterm0()` and `GEMDOS 0x4C Pterm(N)`. `Ptermres` is **not** an
   exit; return `resident=true` and let the caller decide. The exact trace
   format and whether `--trace gemdos` can be enabled/disabled at runtime must
   be proven by the P3 exit spike (`[R]`). A breakpoint on the `GemdosOpcode`
   variable seen in `tools/hatari-prg-args.sh` is an alternative only after
   the spike documents it; it is not a documented MCP mechanism today.
3. **Opt-in console sentinel** — an exact line such as
   `__HATARI_MCP_EXIT__:<code>` on the TOS console, only when the caller
   requests it; reject near-matches and mark `exit_source=console`.
4. **Process exit** — Hatari itself exited (`quit`, emulator crash). Report
   `emulator_gone`/`crashed` and do not present the emulator's process exit
   status as the guest's exit code.
5. **Timeout** — configurable, default 30 s for `run_prg`; return partial
   console/stderr, `crashed=false`, `exit_source=timeout`, and a hint.

Never conflate "program returned to the desktop" with "exit code 0". TOS
programs that terminate without Pterm/NatFeats are a timeout, not a success.

### P3.4 Crash bundle

Triggered by an exception event or by an explicit `--debug-except` hit, while
the debugger is at its prompt. Capture:

* CPU registers (GPRs, SR, PC, USP/ISP) and DSP registers if the profile has
  DSP enabled;
* disassembly around PC plus the preceding instructions where available;
* a bounded stack window around A7 (default 256 bytes), with a note when A7 is
  outside plausible RAM;
* screenshot (P2.1/P2.4);
* demuxed stderr tail and console tail;
* symbol-annotated PC and nearest-symbol offsets;
* optional state snapshot via P1.20;
* resolved configuration, Hatari version, profile, and hashes.

Bundle layout under the configured artifact root:
`runs/<run_id>/manifest.json`, `registers.json`, `disasm.txt`, `stack.bin`,
`stack.txt`, `screen.png`, `console.txt`, `stderr.log`, `symbols.json`,
`state/` (optional). Write atomically; cap total size; never write outside the
artifact root. If the debugger cannot be entered, mark the bundle `incomplete`
with a reason; do not fabricate registers.

### P3.5 Symbol autoload and address resolution

Three modes, all explicit in the result:

* `auto` (default): launch with `--symload exec`; Hatari loads symbols for a
  GEMDOS-HD program when the debugger is invoked. Because a successful run may
  never enter the debugger, also try an explicit `symbols prg` load after the
  program is known to be loaded; if that races, report `symbols_loaded=false`
  and retry on the next stop.
* `eager` (opt-in): a generated `--parse` script breaks once at `pc = TEXT`,
  loads symbols, and continues. This adds a startup stop and must not be the
  default.
* `none`: no symbol loading.

Maintain a server-side index for address→symbol results: parse nm-style `.sym`
files first, then the debugger's symbol listing; binary formats (ELF, DRI/GST,
a.out) are delegated to Hatari's `symbols` loader until a parser exists. The
index stores `{name, address, type, source}` and resolves
`{symbol, offset}` for a PC. PRG base addresses must be applied with the
`symbols <file> <TEXT offset>` / `symbols prg` mechanism; do not assume a fixed
load address. Duplicate addresses and name collisions are reported, not
silently collapsed.

### P3.6 Optional build command

Off by default. Configuration shape (not a shell string):

```toml
[build]
enabled = false
cwd = "."                 # relative to the primary workspace root
timeout_ms = 120000
allow = ["/abs/path/to/ahcc-cli-wrapper"]

[[build.targets]]
name = "hello"
argv = ["/abs/path/to/ahcc-cli-wrapper", "src/hello.c", "-o", "build/hello.tos"]
env = { PATH = "/usr/bin:/bin" }
```

Rules:

* `argv` is executed with `subprocess.Popen(argv, shell=False)`; never
  `shell=True`, never string interpolation, never a model-supplied shell
  fragment;
* the executable must be an absolute path present in `allow`; arguments may
  reference only confined workspace paths;
* `cwd` is pinned; environment is allowlisted; stdout/stderr are capped;
* timeout kills the process group; no network guarantee is claimed;
* a disabled request returns `build_disabled`, not a silent no-op;
* the tool is marked destructive/privileged in its MCP description and is
  audited with the exact argv, cwd, exit status, and duration;
* the built-in AHCC path is preferred over arbitrary commands.

### P3.7 Fast restart

Implement both and measure on the reference host:

* **relaunch**: stop, reap, create a fresh socket, spawn, wait for socket and
  desktop/autostart; clean GEMDOS/socket state but pays process + boot cost;
* **reset**: `emu_reset cold` (or the documented reset command), then verify
  that the virtual INF autostart runs again; faster but can leak state and may
  not re-run `--auto` on every TOS.

Record p50/p95 over at least 20 warm runs, plus one cold-cache run, in the plan
work log. Default to relaunch until measurements show reset is both stable and
materially faster. A reset path that cannot prove autostart is not accepted.

### P3.8 Image builders

Wrap existing host tools; do not reimplement disk formats:

* blank floppy: `hmsa`/`createBlankImage` behavior or a deterministic
  720 KiB / 1.44 MiB zero-filled image, then a FAT filesystem via `mformat`;
* ST image from a directory/zip: `tools/zip2st.sh`;
* HD image: `tools/atari-hd-image.sh` (8–512 MiB, single FAT16 partition);
* outputs confined to the workspace/artifact root; input directories are
  read-only and copied, never built in place;
* record size and SHA-256; set `TZ=UTC` and a fixed timestamp epoch where the
  tool supports it. FAT timestamps may make byte-identical images impossible;
  document that and compare content hashes as well as image hashes;
* missing host tools return `tool_unavailable` with the exact package hint;
* never place images in the corpus or commit them.

### P3.9 Host↔guest transfer

Use the primary GEMDOS directory as the transport. Copy host→guest with
case/8.3 conversion, collision reporting, atomic replace, size cap, and a
post-copy checksum. Guest→host copies only files visible under the primary
root; guest paths are translated and confined. Read-only roots can be copied
*out* to a writable destination, never written in place. Directory recursion is
bounded and rejects symlinks. A transfer that partially fails reports exactly
which files were written and does not leave a half-replaced destination.

### P3.10 Machine profiles

Extend the existing ST/STE/TT/Falcon profiles with capability expectations:

| Profile | Required | Expected limitations |
| --- | --- | --- |
| ST | 68000, 1 MiB, TOS/EmuTOS | no TT RAM, no DSP, no Videl, blitter off unless enabled |
| STE | 68000, 2 MiB, blitter, DMA sound | no TT RAM, no DSP, no Videl |
| TT | 68030, 4 MiB ST + TT RAM, MMU | no Videl/DSP; ACSI/SCSI differences |
| Falcon | 68030, 4 MiB, Videl, DSP (`--dsp emu`) | IDE, crossbar, mic; DSP emulation is build-dependent |

Validate options against the machine; reject combinations Hatari ignores or
warns about. Profiles must pin TOS, memory, CPU/FPU/MMU, DSP, and headless
settings; tests assert boot, screenshot, and one device-specific fact per
profile. P5 owns the full device matrix; P3 only needs the profiles to be
launchable and to expose `profile_capabilities`.

### P3.11 Source mapping (`[R]`)

Research only: parse m68k-atari-mint ELF/DWARF `.debug_line` into a PC→file:line
table, apply the loaded PRG/ELF base address, and cache the table by build
hash. Hatari symbol tables carry no line numbers. If a host `m68k-atari-mint-gcc`
toolchain or `pyelftools` is unavailable, the spike fails closed and the result
is `source_map_unavailable`; symbol-level annotation remains. Do not add a hard
runtime dependency or claim source-level debugging in P3. Deliver a go/no-go
note and hand the result to P4.13/P8.2.

### P3.12 External corpus harness (read-only)

Discovery and manifests:

* roots: `HATARI_CORPUS_ROOT` or `~/Projects/Hatari/{Okami-Shell,freemint,godlib}`;
* each target declares `kind = prebuilt|source`, build system, required
  toolchain/ROM, entry point, expected artifacts, and skip reasons;
* before/after SHA-256 of selected source files proves the tree was not
  modified; builds run in a temporary copy or an out-of-tree build directory;
* never copy third-party source into `mcp/`; preserve license/README notes in
  the test report; no network access assumed.

Project-specific reality (verified on this host):

* **Okami-Shell** is a binary/resource release (`Files/*.PRG`, `*.TTP`,
  resources, docs); there is no source tree to compile. Harness smoke test:
  stage a copy of the required `Files` subtree into a temp GEMDOS root, boot
  `MSH.PRG`/`GEM.PRG`, wait for a stable screen, capture, and verify a
  non-blank frame. Compilation is skipped unless a source subtree is added.
* **FreeMiNT** is a large GCC/automake corpus. The host has
  `m68k-linux-gnu-gcc`, **not** `m68k-atari-mint-gcc`. Select small components
  (`cflib`, `libcmini`) and skip with `toolchain_unavailable` until the Atari
  Mint toolchain exists. Never build the full tree by default.
* **GodLib** requires `m68k-atari-mint-gcc`, `vasmm68k_mot`, `libcmini`, and
  its own linker setup (also absent). Build one isolated library/sample only
  after the toolchain probe; otherwise skip. AHCC cannot build GodLib
  wholesale.

The harness result is a matrix of `pass|fail|skip` with a machine-readable
reason, commands, duration, artifacts, and a read-only proof. A skipped corpus
test is visible and never counted as a pass.

### P3.13 AHCC CLI: one implementation, two entry points

P1.28 owns the extraction of the proven `scripts/ahcc-cli.py` flow into
`hatari_mcp.ahcc` (`models.py`, `project.py`, `runner.py`, `diagnostics.py`,
`cli.py`). P3 must **extend that shared implementation**, not create a second
copy and not re-plan the extraction:

1. verify `scripts/ahcc-cli.py` is already a thin wrapper around
   `hatari_mcp.ahcc.cli`; if it still contains logic, that is a P1.28 defect,
   not a reason to fork;
2. extend `ahcc/project.py` with multi-file C, optional `.S`, multiple include
   dirs, defines, cflags, libs, startup object, and output name; preserve the
   current single-file behavior exactly;
3. extend `ahcc/diagnostics.py` to map guest `SOURCE.C` / include paths back to
   host paths and return `{severity,file,line,column,message,raw}`; keep the
   current error/fatal classification as a fallback;
4. use the P1 `ahcc/runner.py` isolated headless build instance (it must not
   disturb the main supervisor); add any missing handshake/keypress/timeout
   behavior there rather than in a new runner;
5. add `ahcc/matrix.py` as a consumer of the same module; it must not shell out
   to `scripts/ahcc-cli.py` and must not duplicate project rendering;
6. add golden tests that run the old command shape through the shared module
   and compare the project file, command, diagnostics, and artifact behavior.

Hard constraints: temporary work under `/tmp` (or a confined build root), not
inside the corpus; no host shell; reject symlinked include trees or dereference
them into the temp copy; cap copied size; kill the process group on timeout;
never leave the build instance's socket or process behind. AHCC itself is guest
code: a malicious source can crash the emulator, which is reported, not hidden.

AHCC build matrix (bounded, explicit skip reasons):

| Case | Shape | Profile/ROM | Expected |
| --- | --- | --- | --- |
| A1 | single `hello.c`, no libs | ST + EmuTOS | compiles, PRG exists, console output, exit 0 |
| A2 | multi-file C + header | ST + EmuTOS | all objects linked; symbol for second function |
| A3 | include dir, `-D`, extra lib | ST + EmuTOS | flags reach AHCC; result runs |
| A4 | C + one `.S` (only if project format supports it) | ST + EmuTOS | link succeeds; skip with reason if unsupported |
| A5 | intentional C error | ST + EmuTOS | structured diagnostic, no artifact copied |
| A6 | missing include | ST + EmuTOS | structured diagnostic with mapped path |
| A7 | same source on STE profile | STE + EmuTOS | runs; profile capability check |
| A8 | TOS 1.04 vs EmuTOS | ST + each ROM | documented differences; skip if ROM absent |
| A9 | GUI build through `AHCCST.PRG` | ST + EmuTOS | P2.15-dependent; skip if GUI path absent |
| A10 | corpus-derived portable C | ST + EmuTOS | only after license/portability review; otherwise skip |

The matrix validates the AHCC CLI and the run loop. It does **not** claim that
AHCC builds FreeMiNT or GodLib; those depend on their native toolchains.

## Task checklist

### Workspace and transfer

- [ ] P3.1 Define `WorkspaceRoot`/`DriveMount` models and validation.
- [ ] P3.1 Implement host↔guest translation, 8.3/case rules, collisions,
      traversal/symlink rejection, atomic writes.
- [ ] P3.1 Document and test the single-GEMDOS-directory limitation; add a
      `[P]` note for multi-directory mounts.
- [ ] P3.9 Implement host→guest and guest→host transfer with checksums and
      partial-failure reporting.

### Run loop, exit ladder, crash

- [ ] P3.2 Implement `run_prg`, staging, result schema, and artifact root.
- [ ] P3.2 Subscribe to events before resume; never miss the first exception.
- [ ] P3.3 Implement NatFeats → Pterm/Pterm0 → sentinel → process → timeout.
- [ ] P3.3 Add the exit-detection spike for `--trace gemdos` and record the
      exact line formats; mark `GemdosOpcode` as unconfirmed until then.
- [ ] P3.4 Implement crash bundle capture and `incomplete` states.
- [ ] P3.4 Cap bundle size and confine all paths.
- [ ] P3.7 Implement relaunch vs reset and the benchmark harness.

### Symbols

- [ ] P3.5 Implement `auto|eager|none` symbol modes and `.sym` parsing.
- [ ] P3.5 Implement address→symbol resolution with base/offset handling.
- [ ] P3.5 Report duplicate addresses, collisions, and unloaded state.

### Build and media

- [ ] P3.6 Implement the disabled-by-default, allowlisted, shell-free build
      command with audit logging.
- [ ] P3.8 Wrap blank floppy, `zip2st`, and `atari-hd-image` builders with
      deterministic metadata and tool probing.
- [ ] P3.10 Extend ST/STE/TT/Falcon profiles and capability expectations.

### AHCC

- [ ] P3.13 Verify `scripts/ahcc-cli.py` is the P1.28 thin wrapper; no logic
      fork.
- [ ] P3.13 Extend `ahcc/project.py` with multi-file/optional `.S` rendering;
      preserve the single-file golden behavior.
- [ ] P3.13 Extend `ahcc/diagnostics.py` with guest→host diagnostic mapping.
- [ ] P3.13 Extend/reuse `ahcc/runner.py` for isolated builds and cleanup.
- [ ] P3.13 Add `ahcc/matrix.py` and matrix cases A1–A10 with skip reasons.

### Corpus

- [ ] P3.12 Implement root discovery, manifests, probing, and read-only proof.
- [ ] P3.12 Implement Okami-Shell prebuilt smoke test.
- [ ] P3.12 Implement FreeMiNT/GodLib native-build adapters with graceful
      toolchain/ROM skips.
- [ ] P3.12 Add license/prerequisite notes and artifact bundles.

### Research/docs

- [ ] P3.11 Run the DWARF/source-map spike and write a go/no-go note.
- [ ] Update `architecture.md` and `tools-reference.md` with new tools and the
      single-directory limitation.
- [ ] Add examples for `run_prg`, crash bundles, AHCC matrix, and corpus skips.

## Acceptance criteria

1. `run_prg` on a host PRG returns a stable structured result with exit code,
   exit source, console, duration, and a screenshot; no orphan process/socket.
2. A NatFeats fixture returns its exact code; a Pterm fixture and a Pterm0
   fixture are detected with the documented source; an infinite loop times out
   without hanging; a Ptermres fixture is reported as resident, not exited.
3. An illegal-instruction fixture produces a crash bundle containing
   registers, disassembly, stack, screenshot, and log; the bundle is confined
   and complete or explicitly incomplete.
4. Host→guest→host path translation round-trips for legal names; traversal,
   symlink escape, invalid characters, and collisions are rejected with typed
   errors.
5. `auto` symbol mode loads embedded/`.sym` symbols when possible; results
   annotate addresses with `symbol+offset`; failure is reported, not hidden.
6. Build execution is off by default; enabling it requires an allowlisted
   absolute executable; metacharacters are inert; disabled/missing executable
   returns typed errors; every build is audited.
7. Fast-restart measurements are recorded; the default strategy is justified;
   reset does not silently skip autostart.
8. Image builders produce expected sizes/contents and checksums when host tools
   exist; missing tools skip with the package name.
9. AHCC tool and `scripts/ahcc-cli.py` share one implementation; the matrix
   passes A1–A3 and A5–A8, including the mandatory multi-file A2 case; A4/A9/
   A10 may skip with reasons; diagnostics map to host files.
10. Corpus harness never modifies a corpus tree; Okami-Shell smoke passes when
    the tree is present; FreeMiNT/GodLib skip with `toolchain_unavailable`
    until their toolchains exist; skips are reported.
11. DWARF spike produces a documented go/no-go; no source-level claim is made
    without it.
12. Lint/type checks and unit/integration tests pass without proprietary ROMs.

## Test plan

### Unit

* Workspace: host↔guest round trip, drive letters, 8.3/case, invalid chars,
  reserved names, collisions, `..`, symlink escape, read-only roots.
* Exit ladder: recorded NatFeats/Pterm/Pterm0/Ptermres lines, false-positive
  sentinel rejection, precedence, timeout.
* Crash bundle: schema, path confinement, size caps, incomplete reasons,
  atomic write.
* Symbols: nm parsing, duplicate addresses, PRG base offsets, collision
  reporting, `none` mode.
* Build command: argv construction, allowlist, disabled path, timeout,
  metacharacter inertness, cwd/env pinning.
* Images: command construction, size/geometry, checksum metadata, missing-tool
  errors.
* AHCC: golden project files for single/multi-source, diagnostics parser,
  guest→host path mapping, matrix selection/skip reasons.
* Corpus: manifest validation, root discovery, read-only hash checks, skip
  reasons.

### Integration (real Hatari + EmuTOS, no proprietary ROMs)

* Run hello from a generated GEMDOS directory; assert console, exit, screenshot.
* NatFeats exit 0/1/negative; Pterm/Pterm0; Ptermres resident; infinite-loop
  timeout; illegal-instruction crash bundle.
* Autostart from a staged PRG outside the root; verify source checksum and
  cleanup.
* Symbols auto/eager/none; address→symbol in crash and normal results.
* Build command disabled/enabled; a shell-metacharacter argument is passed
  literally; audit record exists.
* Restart benchmark (relaunch vs reset) with p50/p95 and autostart verification.
* Image builders: blank floppy, `zip2st`, 8/16 MiB HD; boot or mount check.
* Host/guest transfer: copy in/out, case collision, partial failure.
* AHCC matrix A1–A8; A9/A10 skip with explicit reason.
* Corpus harness: Okami prebuilt smoke; FreeMiNT/GodLib skip or build per
  toolchain; before/after corpus hashes unchanged.

### Corpus / external

* Never run against a writeable corpus; build only temp copies.
* Record toolchain/ROM prerequisites and license notes in the result.
* Skip cleanly when roots or tools are absent; a skip must be visible in CI.

### CI

* EmuTOS only; no proprietary TOS in CI; optional Atari Mint toolchain job.
* Keep AHCC matrix time bounded; cache AHCC fixtures; do not commit images.
* Fail on orphan processes/sockets and on corpus mutation.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Stock Hatari has one GEMDOS host directory | Multi-root P3.1 cannot be implemented as direct mounts | Primary rw root + staged/read-only images; mark direct multi-mount `[P]`; record scope change. |
| `--auto`/INF autostart needs TOS ≥ 1.04 (per Hatari docs) | Old TOS profiles cannot autostart | Detect TOS version; use a boot disk/AUTO fallback `[R]` or reject with a hint. |
| Pterm trace format is not in our tool contract | Exit ladder step 2 may be wrong | Exit spike first; fall back to NatFeats/sentinel/timeout; never fake a code. |
| Hatari autoloads symbols only around debugger entry | Successful runs may lack symbols | Explicit `symbols prg` retry; `eager` opt-in; report `symbols_loaded=false`. |
| AHCC script currently compiles one C file | Multi-file/assembly matrix may fail; A2 is mandatory | Validate AHCC project syntax in a spike; if the project manager cannot express multi-file, use a documented compile-then-link stage or block A2 instead of marking it pass. |
| Corpus projects need GCC/Atari Mint, not AHCC | "Build each corpus with AHCC" is impossible | Separate AHCC matrix from corpus harness; native toolchain adapters; visible skips. |
| Okami-Shell has no source | Cannot compile it | Prebuilt boot/screenshot smoke; optional source path if added. |
| Image builders need host tools and FAT timestamps | Nondeterministic images | Probe tools; fixed epoch/TZ; content hashes plus image hashes. |
| Build command is arbitrary host code | Security boundary erosion | Off by default, absolute allowlist, no shell, pinned cwd/env, audit, privileged annotation. |
| Reset may not re-run autostart | Fast restart silently runs nothing | Verify after reset; otherwise force relaunch. |
| Corpus builds are heavy/untrusted | Resource use, arbitrary Makefiles | Temp copy, timeouts, resource caps, no network assumption, visible skip. |
| DWARF parsing is optional and base-address sensitive | Wrong line mapping | Research-only, fail closed, no source-level claim. |
| Long console/trace output | Memory/time blowups | Ring buffers, caps, chunked reads, artifact files. |

## Work log

### Done

* 2026-10-27 — phase implementer: read roadmap P3, architecture,
  tools-reference, `scripts/ahcc-cli.py`, `mcp/README.md`, profiles, and the
  AHCC notes. Verified current Hatari option names (`--harddrive`,
  `--gemdos-drive`, `--symload`, `--trace`, `--debug-except`), the single
  GEMDOS-directory field, and the corpus layouts/toolchain absence.
* 2026-10-27 — phase implementer: wrote this plan; no code or non-plan files
  were created.
* 2026-10-27 — build/run implementer: added
  `services/ahcc.py` (discovery, GUI paths, and `compile_c` via the existing
  `scripts/ahcc-cli.py` child process), `tools/build.py` (`ahcc_compile`,
  `ahcc_status`, `ahcc_gui_start`, `ahcc_gui_status`, `run_prg`,
  `workspace_info`, plus guest-path translation helpers), and
  `tests/unit/test_build_translate.py`. Compiles do not use the server
  supervisor; timeout/cancel kills the script and its whole descendant tree
  because the script starts Hatari in a new session. GUI start refuses to
  disturb a running instance and returns `unsupported_headless` without a
  display. `run_prg` uses `app.start`/`wait_for_exit`, maps the workspace as
  C:, write-protects external corpus trees, and reports only NatFeats exits.
  Verification: `py_compile` passed; unit tests 10 passed; real AHCC smoke
  compiled `/tmp/hatari-mcp-smoke/hello.c` to a 7070-byte TOS in 1.3 s with
  EmuTOS (temporary output removed). `server.py` still needs to call
  `build.register(server, APP)`; it was out of this task's edit scope.

* 2026-10-27, reconciliation: `run_prg`, workspace/guest-path translation,
  AHCC status/compile/GUI entry points, the corpus discovery/staging harness
  and the shared AHCC CLI path are implemented and registered. AHCC CLI
  compiles the smoke C source to a 7,074-byte TOS; the real scenario
  boot-capture-stop passes.

### Remaining / next steps

* `run_prg` has no Pterm auto-exit; a program that exits through Pterm without
  NatFeats can rely only on the timeout/exit ladder.
* Native corpus builds are blocked on `m68k-atari-mint-gcc`/`vasmm68k_mot` (and
  `m68k-atari-mint-ar`), which are absent on this host; FreeMiNT prebuilt smoke
  binaries are also unavailable.
* AHCC matrix cases A4/A10 and multi-file/`.S` project syntax remain
  unverified; they must keep skipping with an explicit reason until proven.
* P3.1 remains limited to Hatari's single GEMDOS host directory; direct
  multi-root mounts require a Hatari patch.
* P3.11 source mapping is research-only and blocked on an Atari-mint DWARF
  toolchain; it does not gate P3.12/P3.13.

### Next

1. Implement Pterm auto-exit detection without weakening the NatFeats ladder.
2. Add solved AHCC matrix cases and corpus build adapters only when their real
   toolchains are present; keep absent-tool skips visible.
3. Record the single-directory workspace limitation in examples and keep the
   read-only corpus staging proof.

## Adversarial review

**Review method:** the requested `Task` tool (`subagent_type: general`) is not
available in this environment, so an independent subagent review could not be
run. The implementer performed the documented adversarial self-review below.
It must be replaced by an independent review before P3.1 coding starts. The
build/run increment on 2026-10-27 was also self-reviewed; no independent
reviewer was available.

| Finding | Why it matters | Incorporated as |
| --- | --- | --- |
| Hatari exposes only one GEMDOS host directory (`szHardDiskDirectories[0]`); `--gemdos-drive` assigns that one directory. | The roadmap wording "one or more host roots, read-only mounts" is not implementable as multiple direct GEMDOS mounts. | P3.1 now defines one rw primary root plus staged/read-only images; direct multi-mount is `[P]`; scope change is explicit. |
| The P1 plan says "P3 expands to a multi-root model". | A later implementer could read this as multiple direct GEMDOS mounts and build an unimplementable feature. | P3.1 explicitly reinterprets multi-root as staged/read-only roots; P1 wording is called out as a correction. |
| Pterm/Pterm0 detection is not a documented MCP mechanism, and the trace format is unverified. | The exit ladder could report a false success or hang. | Added an exit spike and `[R]`; fallback is NatFeats → sentinel → timeout, never a fabricated code. |
| `--symload exec` only helps when the debugger is entered. | A clean run may return no symbols, breaking address annotation. | Added `symbols_loaded` reporting, explicit `symbols prg` retry, and opt-in `eager` mode. |
| The proven AHCC script compiles exactly one `.c`; multi-file/`.S` project syntax is unverified. | The matrix could promise cases that AHCC cannot build. | Added an AHCC project-format spike; A4/A10 skip with reasons until proven. |
| FreeMiNT and GodLib require the Atari Mint GCC toolchain, which is absent; Okami-Shell has no source. | "Compile representative C from each corpus with AHCC" is false for the current trees. | Split AHCC matrix from corpus harness; native toolchain probes; Okami prebuilt smoke; visible skips. |
| `--auto` requires TOS ≥ 1.04. | Old TOS images would silently fail to launch. | TOS-version check and a documented fallback/rejection. |
| Image builders depend on host tools and FAT timestamps. | Tests could be flaky or claim determinism falsely. | Tool probes, fixed epoch/TZ, content and image hashes. |
| An allowlisted build command is still arbitrary host code. | It contradicts the "no arbitrary host shell" non-goal if enabled casually. | Off by default, absolute allowlist, `shell=False`, pinned cwd/env, audit, privileged tool annotation. |
| `emu_reset` may not re-run the INF autostart. | Fast restart could benchmark a machine that never runs the program. | Verify after reset; default relaunch until proven otherwise. |
| Corpus builds may run untrusted Makefiles or write in-tree. | Supply-chain/resource risk and corpus mutation. | Temp copy, read-only hash proof, timeouts, resource caps, no network assumption. |
| `scripts/ahcc-cli.py` starts Hatari with `start_new_session=True`; killing only the script's process group would orphan the emulator on timeout/cancel. | A failed/timed-out compile could leave a Hatari process and socket behind. | `services/ahcc.py` walks `/proc` for descendants and terminates the whole tree, then falls back to SIGKILL; the unit test spawns a new-session grandchild and asserts it dies. |
| The standalone script defaults its output to `source.with_suffix(".TOS")`, which would write into a read-only corpus. | Compiling a corpus source without `-o` would mutate third-party trees. | `ahcc_compile` always supplies an output under `mcp/` or `/tmp` (or `HATARI_MCP_BUILD_DIR`), and rejects an explicit output outside those roots. |
| `app.start` has no parameter for guest argv. | `run_prg(..., args=...)` cannot use a native helper without changing `app.py`. | Guest arguments are injected with a generated `--parse` basepage script modeled on `hatari-prg-args`; the missing `app.start` guest-argv support is reported as a core change, not hidden. |
| `run_prg` cannot detect Pterm/Pterm0 exits; only NatFeats `NF_EXIT` sets `program_exit_code`. | A normal Pterm program is reported as a timeout, not a fake success. | Tool description and result fields (`pterm_auto_exit=false`, `exit_detection="natfeats"`) state this explicitly; `exit_code` stays null unless NatFeats supplied it. |
| `ahcc_gui_start` copies AHCCST.BIN into a `/tmp` workspace and leaves it for inspection. | Repeated GUI starts can accumulate copies, but writing into `tools/ahcc` would mutate the distribution. | Documented `/tmp` workspace in the result; cleanup is left to the caller/OS to avoid deleting an active GUI project. |
| `server.py` does not register `tools/build.py`. | The new tools exist but are not reachable through the MCP server until wiring changes. | Reported as a required core change; this task was not allowed to edit `server.py`. |
