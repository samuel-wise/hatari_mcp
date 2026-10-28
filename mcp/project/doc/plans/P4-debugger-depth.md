# P4 — Debugger depth

Status: partial — advanced debugger/backtrace/profile/trace/DSP/state-diff subset implemented; stock watchpoints, DSP parity, trace files, history clear and source mapping remain
Owner: phase implementer (debugger)
Last updated: 2026-10-27
Roadmap: P4.1–P4.13

## Goal

Make the interactive Hatari debugger's common workflows reachable through
structured MCP tools, without hiding what stock Hatari cannot do. "Depth" means
watchpoints, conditions, actions, DSP, full CPU state, backtraces,
history/profile/trace, symbol management, state diffs, safe non-interactive
output, exception policy, crash triage, and a source-level research spike.

Gate (roadmap): the interactive debugger's common workflows are all reachable
through tools, with structured results.

Non-goal: inventing watchpoint or FPU capabilities that require a Hatari patch
without marking them `[P]`. A tool may return `unsupported` with the missing
mechanism and a patch candidate; it must not fabricate a result.

## Dependencies

| Dependency | Why |
| --- | --- |
| P0.3, P0.3a, P0.4 | Dual channel (stdin at prompt, socket while running), debugger-input proof, stderr demuxer. |
| P1.10 | Exception surfacing and `--debug-except` entry. |
| P1.11, P1.14 | Symbol loading and expression evaluation. |
| P1.15–P1.17 | Breakpoint set/list/delete, step/next/continue, breakpoint wait events. |
| P1.20 | State save/load for diffs and optional crash snapshots. |
| P1.25–P1.27 | Path confinement and output limits. |
| P3.4 | Crash bundle layout reused by P4.12. |
| P3.5 | Symbol index and address→symbol resolution. |
| P3.10 | Falcon/DSP profiles for P4.4; machine profiles for P4.5. |
| P3.11 (optional) | DWARF mapping input for P4.13; P4.13 is blocked without it. |

Soft dependencies: P2.1/P2.4 for crash screenshots; P3.2 for runs that start
profiling before the program executes; P3.7 for restarting with a different
exception policy.

## Deliverables and file ownership

All implementation paths are under `mcp/project/src/hatari_mcp/`.

| Roadmap | Deliverable | Primary path | Owner role |
| --- | --- | --- | --- |
| P4.1 | Memory watchpoints and range expansion | `debug/watchpoints.py`, extend `debug/breakpoints.py` | debugger implementer |
| P4.2 | Conditions, temporary breakpoints, hit policy | `debug/conditions.py`, extend `debug/breakpoints.py` | debugger implementer |
| P4.3 | Breakpoint actions and safe script generation | `debug/actions.py` | debugger implementer |
| P4.4 | DSP registers/memory/disassembly/breakpoints/step | `debug/dsp.py` | DSP implementer |
| P4.5 | CPU/FPU/MMU/cache state | `debug/cpu_state.py`, extend `debug/registers.py` | CPU-state implementer |
| P4.6 | Backtrace, stack window, struct view | `debug/backtrace.py`, extend `debug/disasm.py` | debugger implementer |
| P4.7 | History, profile, trace management | `debug/history.py`, `debug/profile.py`, `debug/trace.py` | profiling implementer |
| P4.8 | Symbol load/unload/collisions/demangling/base | extend `debug/symbols.py` (P1.11/P3.5) | symbol implementer |
| P4.9 | Memory checksums and state diffs | `debug/diff.py`, extend `tools/state.py` | state implementer |
| P4.10 | Non-interactive output/pager handling | `debug/output.py`, extend `channels.py` | channel implementer |
| P4.11 | Exception policy and vector breakpoints | extend `exception.py`, `debug/vector_bp.py` | debugger implementer |
| P4.12 | Crash classification heuristics | `debug/crash_classify.py` | crash implementer |
| P4.13 | Source-level research spike | `debug/source.py` (research) | research implementer |
| — | Tool registration and schemas | extend `tools/breakpoints.py`, `tools/execution.py`, `tools/debugger.py`, `tools/memory.py`; add `tools/profile.py`, `tools/trace.py` | debugger implementer |

Supporting test ownership:

* `tests/unit/test_watchpoints.py`, `test_conditions.py`, `test_actions.py`,
  `test_register_parser.py`, `test_profile_parser.py`, `test_backtrace.py`,
  `test_state_diff.py`, `test_output_limits.py`, `test_exception_policy.py`,
  `test_crash_classify.py`.
* `tests/integration/test_debugger_depth.py`, `test_dsp.py`,
  `test_exceptions.py`, `test_source_spike.py`.
* Fixtures: watchpoint writer, DSP (Falcon), 030/MMU, illegal instruction,
  bad jump, stack smash, null deref, deep call chain, hot loop, symbol
  collision, and (if the Mint toolchain exists) a DWARF binary.

## Design / constraints

### Channel and result rules

All debugger commands use the active channel selected by supervisor state:

* running/remotely paused → control socket;
* at the debugger prompt → stdin.

Every command has a timeout and an output cap. A result is structured when a
parser exists; `raw` is always included for best-effort output. If the state is
ambiguous, fail with `debugger_state_unknown`; never wait on a socket reply
that cannot arrive while stopped (see architecture §2.3/§4).

Commands that leave the prompt (`cont`, `dspcont`, `quit`, `reset`) must not be
followed by a sentinel. Commands that stay at the prompt may use the sentinel.

### P4.1 Watchpoints

Stock Hatari conditional breakpoints can track **value changes** using the
redundant-comparison shorthand:

```text
b (<address>).w ! (<address>).w
```

This is a write/change proxy, not a read watchpoint:

* it triggers when the tracked value changes (including writes by Hatari
  itself and writes of the same value are not changes);
* it cannot distinguish a read from a write;
* the tracking value is initialized from the current memory value.

Therefore:

| Requested mode | Implementation | Honesty |
| --- | --- | --- |
| `w` | value-change breakpoint, default width word | supported as a write/change proxy |
| `rw` | same as `w` | supported only as a change proxy; a pure read is not detected |
| `r` | none in stock Hatari | `[P]/[R]`; return `unsupported_read_watchpoint`; patch candidate is read-access instrumentation in the CPU memory path / `breakcond.c` |

Range watchpoints are expanded to one condition per aligned byte/word/long.
Hatari allows at most four conditions per conditional breakpoint, so a range is
split across multiple breakpoints; cap the total (proposed 16 bytes / 8
breakpoints) and reject or explicitly degrade wider ranges with a typed error.
Do not silently watch only the first four bytes.

The server maintains a stable watchpoint ID, its Hatari breakpoint index(es),
initial values, width, mode, and hit counts (best-effort from stops). Delete
removes every expansion; there is no enable/disable in stock Hatari.

### P4.2 Conditional and temporary breakpoints

Supported stock options (verified in the debugger manual): conditions joined
by `&&` (maximum four), `:count`, `:once`, `:file`, `:info`, `:lock`,
`:noinit`, `:print`, `:quiet`, `:trace`.

API mapping:

* `condition`: validated expression passed through, quoted where required;
* `once`: maps to `:once`;
* `every_nth`: maps to `:count` (Hatari stops on every Nth hit, not "after N
  hits");
* `stop_after_hits`: server-side event counting; after N actual stops, delete
  the breakpoint and optionally continue;
* `hit_count`: best-effort count of observed stops; Hatari does not expose a
  native total, and `:trace` breakpoints do not stop, so counting them is not
  reliable. Report `hit_count_confidence: exact|observed|unknown`;
* temporary run-to-address reuses P1.16 `dbg_run_until`, which must delete its
  temporary breakpoint on hit and on timeout.

Condition validation rejects control characters and empty expressions, limits
length, enforces the four-condition cap, and never passes a model-supplied
string to a host shell. The debugger expression language is still powerful
(memory reads, symbols, indirect addressing); document it as privileged but
not as host shell execution.

### P4.3 Breakpoint actions

Safe action vocabulary, generated by the server:

* `print`: a literal string (no control characters, bounded length);
* `info`: an allowlisted `info` subcommand (`registers`, `disasm`,
  `basepage`, `gemdos`, `videl`, `mmu`, `dsp`, etc.);
* `stack`: a bounded memory dump around A7;
* `struct`: a validated type/skip/count specification with a byte cap;
* `screenshot`: only to a server-chosen artifact path;
* `continue`: `:trace` semantics (continue after printing);
* `stop`: normal breakpoint entry.

Do **not** accept a raw debugger script from the model for `:file`. A `:file`
script can execute `quit`, `reset`, `stateload`, `logfile`, `parse`, `setopt`,
and `symbols`, which is effectively host file/control access. If a script
action is added later, it must be a server-generated file in a confined
temporary directory, with an allowlist of commands and a size/line cap; a
`privileged_raw_script` escape hatch may exist only behind an explicit server
flag and destructive annotation.

Actions must not leave the debugger in an unexpected state. `:trace` actions
never stop; `:file`/`:info` actions that stop are followed by a normal capture.
Auto-continue actions are rate-limited and counted so a hot breakpoint cannot
spin the server.

### P4.4 DSP

Requires a Falcon profile with `--dsp emu` (or `dummy` only where documented);
if DSP emulation is unavailable, return `dsp_unavailable` with the build/profile
reason.

Commands: `dspreg`, `dspmemdump` (`P`/`X`/`Y` spaces), `dspdisasm`,
`dspbreak`, `dspstep`, `dspnext`, `dspcont`, `dspsymbols`, `dspprofile`,
`dspaddress`. Parse registers and memory into the same structured shape as the
CPU where possible. DSP breakpoint conditions use the DSP address space modes
(`P`, `X`, `Y`) and R0–R7 only (per the debugger manual); validate before
sending.

A DSP breakpoint may stop the DSP while the CPU is running or at the prompt;
the supervisor must classify the entry from stderr and report
`{cpu_stopped, dsp_stopped, reason}`. This behavior must be proven by the DSP
integration test before the tool advertises a combined stop.

### P4.5 CPU state beyond GPRs

Structured `dbg_cpu_state`:

* GPRs `d0–d7`, `a0–a7`, `pc`, `sr`, `usp`, `isp`;
* 68020+ `caar`, `cacr`, `dfc`, `sfc`, `msp`, `vbr` via `cpureg`;
* MMU state via `info mmu` (TT/Falcon profiles with MMU enabled);
* cache state via `cacr`/`caar` and profiler cache statistics
  (`profile caches`), not as a full cache-content dump;
* FPU type from configuration, but **FPU data/control registers are not
  exposed by the stock debugger**. Return
  `fpu_state: {available: false, reason: "not exposed by stock debugger",
  patch_candidate: ...}` and keep `[P]/[R]` for actual FP0–FP7/FPCR/FPSR/FPIAR
  readback. Patch candidate: extend `cpureg`/`info` with FP0–FP7, FPCR,
  FPSR, FPIAR, or add a documented state-file parser. Do not parse an
  undocumented state file as if its format were stable.

All fields carry `availability` so clients can distinguish a real zero from
"unsupported". Setting state uses `cpureg`/`dbg_registers_set`; MMU/cache
writes are out of scope unless a documented command exists.

### P4.6 Backtrace, stack, struct

* `backtrace` uses the debugger's profiler-derived backtrace. It requires
  program symbols and `profile on` **before** the code of interest runs;
  enabling it only after a crash cannot retroactively reconstruct the stack.
  When unavailable, return `backtrace_unavailable` with the missing
  prerequisite.
* The frame-pointer `bt` form mentioned in Hatari's manual is not in
  `tools-reference.md`; expose it only as an optional `[R]` mode after a spike,
  and label it less reliable.
* `stack_window`: read a bounded region around A7 (default 256 bytes), check
  that A7 is in a plausible RAM range, and annotate return-address candidates
  with symbols. Label candidates, not facts.
* `dbg_struct`: validate `name address [field:type...]`, cap types/counts and
  total bytes, and never allow a spec that reads outside configured RAM.
* `dbg_backtrace` result:
  `{frames: [{pc, symbol, offset, source?, confidence}], source:
  "profile|frame", truncated, raw}`.

### P4.7 History, profile, trace

* `history` must be enabled before the event of interest (`history on` at boot
  or early). Tool can enable it, read N entries, and report that enabling after
  the fact yields no history. Parse CPU and DSP entries where present; cap.
* `profile start/read/stop` maps to `profile on`, `profile`/`profile save`, and
  `profile off`. Parsing returns
  `{address, symbol, count, cycles, percent, cache}` where available. Profiling
  changes timing and adds overhead; never enable it implicitly for a
  deterministic screenshot run.
* `profile stack`, `profile caches`, `profile save` are bounded artifact
  operations; profile save paths are confined.
* DSP profiling uses `dspprofile` and is separate from CPU profiling.
* `trace` sets are validated against `trace help`; enable only requested sets;
  large sets are refused or capped. `--trace-file`/`logfile` paths are
  confined; trace lines are classified by the demuxer. Trace files can grow
  without bound, so a run has a byte cap and rotation/stop policy.
* `dbg_history` and profile tools report whether data is complete or truncated.

### P4.8 Symbol management

Extend P3.5:

* load `symbols prg`, `symbols <file> [TEXT offset]`, `dspsymbols`;
* unload with `symbols free` (verify exact behavior for CPU vs DSP);
* autoload modes `exec|debugger|off` and `symbols autoload` reporting;
* list/filter with `symbols name <substring>`; resolve duplicates and report
  address collisions instead of silently choosing one;
* demangle on the host only when `c++filt`/`m68k-atari-mint-c++filt` exists;
  keep raw names and provide both; never rewrite the emulator's table;
* PRG/base handling: use `symbols prg` or an explicit TEXT offset; expose the
  base and section offsets in results;
* symbol files are confined; no arbitrary host file reads through the debugger.

### P4.9 Memory checksums and state diffs

Two levels:

1. **reliable**: register snapshots + memory-region checksums/byte diffs
   (`savebin` to artifacts, compare host-side); report changed ranges and
   annotate them with symbols;
2. **opaque full-state**: `statesave` files are large and version-specific.
   A byte-level diff may be offered as `[R]` with a warning; do not claim a
   semantic state diff. Exclude obvious header/version noise only if the format
   is documented for this Hatari version.

`state_diff` returns `{registers: {...}, regions: [{start,len,changed,hash}],
snapshot_diff: "unsupported|raw", confidence}`. Restoring a state is
destructive and gated.

### P4.10 Non-interactive output and pager handling

Hatari's debugger has no built-in file pager; the real risks are:

* readline writing a prompt to stdout (which is the TOS console) or blocking;
* unbounded `memdump`/`disasm`/profile output;
* an external client/host pager consuming output or waiting on a TTY.

Implementation:

* run with a non-TTY-safe reader (P0.3a); never spawn a host pager;
* cap lines and bytes per reply; return a cursor/continuation token;
* send large dumps to an artifact via `savebin`/`logfile` and return metadata;
* detect a `--More--`, `(q)uit`, or prompt-like line and fail
  `pager_or_prompt_blocked` rather than sending `q` blindly;
* never let a single debugger command block the event stream; use timeouts.

### P4.11 Exception policy

Launch-time `--debug-except` flags (verified): `none`, `nohandler`, `bus`,
`address`, `illegal`, `zerodiv`, `chk`, `trapv`, `privilege`, `trace`,
`linea`, `linef`, `dsp`, `autostart`, `all`. Support comma-separated masks and
`+`/`-` modifications where Hatari accepts them; runtime changes via `setopt`
must be verified before being advertised.

Policy object:

```json
{
  "stop_mask": ["bus", "address", "illegal", "zerodiv"],
  "ignore_vectors": [4],
  "report_once": true,
  "max_reports": 10,
  "cooldown_ms": 250,
  "auto_continue": false
}
```

* first-chance vs repeated: dedupe by `(vector, pc, sp)` and time window;
* `report_once` suppresses repeats but records a count;
* `auto_continue` is explicit and rate-limited; it must never create an
  infinite stop/continue loop (max reports, then stop and report);
* vector breakpoints: read the vector target from guest memory and set a
  breakpoint at the handler, deleting it on hit/timeout; handlers can change,
  so re-resolve before use and label the result;
* exception events are delivered as events, not as replies to a queued socket
  command.

### P4.12 Crash classification heuristics

Evidence-based, with a confidence score and the raw evidence attached:

| Pattern | Evidence | Confidence rule |
| --- | --- | --- |
| Illegal instruction | exception vector/opcode, `--debug-except illegal`, disasm | high if vector + opcode agree |
| Bad jump | PC outside known code/text or unmapped RAM, odd PC, branch target | medium/high with segment/symbol evidence |
| Stack corruption | A7 outside plausible stack, return address not in code, backtrace discontinuity | medium; never high without a validated frame |
| Null deref | fault address < 0x100 or close to zero, data access instruction | high if address and instruction agree |
| Bus/address error | exception text, fault address, access size | high for the exception, medium for the cause |

Tie the report to P3.4's bundle and P3.5 symbols. Source line is included only
if P3.11/P4.13 delivered a validated mapping. The result includes
`classification`, `confidence`, `evidence`, `symbol`, `source?`, and
`alternatives`; do not present a heuristic as a root cause.

### P4.13 Source-level research (`[R]`)

Build on P3.11: DWARF line table → PC ranges, load base, inline frames if
feasible, cache keyed by binary hash. Deliver a spike with:

* a real m68k-atari-mint DWARF binary (if toolchain present);
* mapping accuracy checks at known symbols/lines;
* PRG base/relocation handling;
* failure behavior when DWARF is absent (AHCC `-g` is symbol-level, not line
  info);
* a go/no-go recommendation for line breakpoints/stepping.

Do not expose file:line breakpoints or source stepping until the mapping is
validated. Without it, symbol-level debugging is the honest contract.

## Task checklist

### Watchpoints and conditions

- [ ] P4.1 Implement write/change watchpoints with initial-value tracking.
- [ ] P4.1 Implement range expansion with the four-condition cap and a total
      byte/breakpoint cap; reject wider ranges with a typed error.
- [ ] P4.1 Return `unsupported_read_watchpoint` for `r` with a patch candidate.
- [ ] P4.2 Validate conditions, enforce the four-condition and length caps.
- [ ] P4.2 Implement `once`, `every_nth` (`:count`), `stop_after_hits`, and
      best-effort hit counts with confidence labels.
- [ ] P4.2 Ensure temporary breakpoints are removed on hit and timeout.

### Actions, DSP, state

- [ ] P4.3 Implement the safe action vocabulary and generated `:file` scripts
      only if needed; no raw model scripts.
- [ ] P4.3 Add auto-continue rate limiting and loop protection.
- [ ] P4.4 Implement DSP state/memory/disasm/breakpoints/step and the
      combined-stop classification.
- [ ] P4.5 Implement CPU/MMU/cache state with `availability`; return a clear
      unsupported result for FPU registers.
- [ ] P4.6 Implement profiler backtrace, stack window, struct view, and
      confidence labels.
- [ ] P4.7 Implement history enable/read, CPU/DSP profile, trace sets, and
      trace-file caps.
- [ ] P4.8 Implement symbol unload/collisions/demangling/PRG base reporting.
- [ ] P4.9 Implement register/memory checksums and diffs; keep full-state diff
      research-only.

### Output, exceptions, crashes

- [ ] P4.10 Implement output caps, continuation cursors, pager/prompt
      detection, and artifact fallback.
- [ ] P4.11 Implement exception masks, dedupe, report-once, cooldown, and
      vector breakpoints.
- [ ] P4.12 Implement crash heuristics with evidence and confidence; integrate
      with P3.4.
- [ ] P4.13 Run the DWARF/source spike and record go/no-go.

### Docs/tests

- [ ] Update `tools-reference.md` and `architecture.md` with the supported
      limitations (`r` watchpoints, FPU state, range caps, hit-count
      confidence).
- [ ] Add debugger-depth examples and failure hints.
- [ ] Add unit/integration tests listed below.

## Acceptance criteria

1. Write/change watchpoints stop on a value change; `rw` documents that reads
   are not detected; `r` returns a typed unsupported result with `[P]/[R]`.
2. Range watchpoints cover the requested range up to the documented cap or
   fail explicitly; no silent partial watch.
3. Conditional breakpoints, `:once`, every-Nth, and stop-after-N work; hit
   counts carry a confidence label.
4. Safe actions print/inspect/continue without host command execution; a
   script action cannot run `quit`, `reset`, `parse`, `logfile`, or `setopt`.
5. On a Falcon with DSP emulation, DSP registers, memory, disassembly,
   breakpoints, and stepping work; without DSP, the tool returns
   `dsp_unavailable`.
6. CPU state includes GPRs, SR, USP/ISP, 68020+ registers, MMU and cache
   control state where available; FPU register state is explicitly
   unsupported, not fabricated.
7. Profiler backtrace works when profiling was enabled before the code ran;
   otherwise the result says why it is unavailable. Frame-pointer mode is
   marked `[R]`.
8. History must be enabled before the event; profile and trace results are
   bounded and report truncation.
9. Symbols can be loaded, unloaded, listed, filtered, and resolved; collisions
   and duplicate addresses are reported; demangling is optional and preserves
   raw names.
10. Register/memory diffs are reliable; full-state semantic diff is explicitly
    research-only.
11. Large outputs never hang; pager/prompt detection fails safely.
12. Exception policy masks, dedupe, and vector breakpoints work; auto-continue
    cannot loop forever.
13. Illegal instruction, bad jump, stack corruption, and null deref fixtures
    get evidence-based classifications with confidence, linked to
    symbols/source when available.
14. The source spike produces a documented go/no-go; no source-level claim
    without it.
15. Lint/type checks and tests pass without proprietary ROMs.

## Test plan

### Unit

* Condition parser/validator: four-condition cap, quoting, widths/spaces,
  control-character rejection, length cap.
* Watchpoint expansion: byte/word/long alignment, range cap, duplicate
  suppression, stable IDs, delete-all.
* Action generator: allowlisted commands, path confinement, no `quit/reset/
  parse/logfile/setopt`, loop/rate limits.
* Register/CPU-state parser: GPR/SR/USP/ISP/020+, MMU/cache availability, FPU
  unsupported shape.
* Profile/backtrace/history parsers with recorded output, truncation, and
  missing sections.
* State diff: register changes, memory ranges, symbol annotation, opaque-state
  warning.
* Output limits: line/byte caps, cursor continuation, pager/prompt detection.
* Exception policy: dedupe keys, report-once, cooldown, max reports,
  auto-continue termination.
* Crash classifier: each fixture's evidence and confidence; no high confidence
  on weak evidence.

### Integration (real Hatari + EmuTOS; Falcon for DSP; optional proprietary TOS)

* Watchpoint fixture writes a word; stop and capture; read-only request returns
  unsupported; range request respects the cap.
* Conditional breakpoint (register/memory condition), `:once`, every-Nth,
  stop-after-N, and temporary run-until removal.
* Breakpoint action prints registers/memory and continues; unsafe commands are
  rejected.
* Falcon DSP: registers, P/X/Y memory, disassembly, breakpoint, step/next/
  continue; verify combined stop state. Skip with `dsp_unavailable` if the
  build lacks DSP.
* CPU state on TT (MMU) and 030 cache profile; FPU field reports unsupported.
* Backtrace with `profile on` at start + symbols; without profiling, explicit
  unavailable result. Stack window on a deep call chain.
* History enabled at boot and read after a stop; profile start/read/stop;
  trace set/file with caps.
* Symbol load/unload/free, duplicate address, collision, PRG base, optional
  demangling; DSP symbols.
* State diff between two snapshots/memory regions; restore is gated.
* Huge `memdump`/`disasm` returns capped/artifact results; no pager hang.
* Exception masks (bus/illegal/address), report-once, vector breakpoint;
  auto-continue stops at max reports.
* Crash fixtures: illegal instruction, bad jump, stack smash, null deref;
  verify bundle + classification + symbol link.
* Source spike: build a DWARF fixture if `m68k-atari-mint-gcc` exists; assert
  line mapping; otherwise skip with a clear reason.

### Corpus / external

* Run crash/heuristic fixtures against Okami-Shell or GodLib binaries where
  available, without requiring source.
* If FreeMiNT/GodLib are built by P3 corpus adapters, use them for backtrace
  and symbol stress tests; skip cleanly otherwise.
* Never modify corpus trees; artifacts go to temp/artifact roots.

### CI

* EmuTOS only; no proprietary TOS; DSP test skipped on builds without DSP.
* Source/DWARF job optional and non-blocking until the spike passes.
* Assert no orphan emulator, no leaked symbol/action temp files.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Stock Hatari has no read watchpoints | P4.1 "read" cannot be implemented | Write/change proxy for `w`/`rw`; `r` returns `[P]/[R]`; do not claim read detection. |
| Range watchpoints are not native and are capped at four conditions per breakpoint | Large ranges need many breakpoints or a patch | Byte/breakpoint caps; explicit rejection; `[P]` for wide ranges. |
| FPU registers are not exposed by the debugger | P4.5 FPU state is impossible without a patch/state parser | Return unsupported with `[P]/[R]`; do not infer from memory. |
| Hit counts are not natively reported | "hit counts" can be wrong | `:count` for every-Nth; server-side stop counting with confidence labels. |
| `bt` frame-pointer backtrace is not in our tool contract | Could advertise an unreliable feature | Profile backtrace first; `bt` only as `[R]` after validation. |
| `:file` breakpoint scripts can execute dangerous debugger commands | Host file/control access | Server-generated scripts only; allowlist; no raw model scripts. |
| Profiling/history must be enabled before the event | Post-crash results may be empty | Enable explicitly at boot; report prerequisites and truncation. |
| DSP support is build- and machine-dependent | P4.4 tests fail on non-Falcon/minimal builds | Probe `--dsp`/`info dsp`; skip with `dsp_unavailable`. |
| DSP break stop semantics may stop the CPU too | Wrong state reporting | Spike/integration test before advertising combined state. |
| Full state snapshots are opaque/version-specific | Semantic diff is not trustworthy | Memory/register diffs reliable; full-state diff `[R]` raw only. |
| Exception auto-continue can spin | CPU burn, missed crashes | Rate limit, max reports, cooldown, explicit opt-in. |
| Hatari has no debugger pager, but readline/TTY does | Prompt pollution or blocking | Non-TTY reader, caps, prompt detection, no host pager. |
| Source mapping depends on DWARF and load base | Wrong lines/breakpoints | Research-only; fail closed; no source-level claim. |
| Debugger output interleaves with logs/traces | Parser misattribution | Demuxer classification, sentinels at prompt, raw fallback. |

## Work log

### Done

* 2026-10-27 — phase implementer: read roadmap P4, architecture,
  tools-reference, and Hatari's debugger manual/source. Verified breakpoint
  options/conditions, `cpureg` register set, `info mmu`, cache control
  registers, DSP command surface, profiler backtrace prerequisites, and the
  absence of FPU register readback in the stock debugger.
* 2026-10-27 — phase implementer: wrote this plan; no code or non-plan files
  were created.
* 2026-10-27 — advanced-debugger implementer: added
  `src/hatari_mcp/tools/debug_advanced.py` and
  `tests/unit/test_debug_advanced.py`. The module registers 17 tools:
  `dbg_backtrace`, `dbg_stack_dump`, `dbg_profile_start`,
  `dbg_profile_stop`, `dbg_trace_start`, `dbg_trace_stop`, `dbg_history`,
  `dbg_command_history_clear`, `dbg_dsp_registers`, `dbg_dsp_memory`,
  `dbg_dsp_disassemble`, `dbg_dsp_breakpoint_set`,
  `dbg_dsp_breakpoint_list`, `dbg_dsp_continue`, `dbg_dsp_step`,
  `dbg_state_diff`, and `dbg_breakpoint_actions`.
* 2026-10-27 — advanced-debugger implementer: verified command syntax and
  output against `stacktrace.c`, `debugcpu.c`, `debugdsp.c`, `history.c`,
  `profile.c`, `log.c`, `breakcond.c` and `dsp.c`. `backtrace` is an A6
  frame-pointer walk (not a profiler reconstruction); `profile on/off` and
  `dspprofile on/off` are distinct; `trace` selects logging categories and has
  no file argument; `history <count>` prints only entries not shown earlier;
  DSP is enabled only by `Config_IsMachineFalcon()`; there is no history
  clear command.
* 2026-10-27 — advanced-debugger implementer: `dspcont` leaves the prompt but
  is not in `Supervisor.TERMINAL`, so the module calls the supervisor's
  terminal helper rather than sending a sentinel. `py_compile` and the
  advanced unit suite pass (18 tests); the full `project/tests` suite passes
  (164 tests). No runtime Hatari probe was required for parser tests.
* Core wiring still needed: `server.py` does not import or register
  `tools/debug_advanced.py`. This module cannot be exposed until the owner of
  `server.py` adds `from .tools import debug_advanced` and
  `debug_advanced.register(server, APP)`.

* 2026-10-27, reconciliation: `tools/debug_advanced.py` is registered and the
  advanced subset is implemented: preview-only breakpoint actions, DSP
  registers/memory/disassembly/breakpoints/step (Falcon builds), frame-pointer
  backtrace and stack words, history/profile/trace controls, and byte-level
  state diffs. The full suite is 452 passed, 1 skipped.

### Remaining / next steps

* No stock watchpoints: P4.1/P4.2 real, read and wide-range watchpoints and
  conditions remain blocked on a Hatari patch (the current watchpoint tools
  return an explicit `unsupported` result).
* DSP support is Falcon-only; non-Falcon/minimal builds must skip with
  `dsp_unavailable`, and combined-stop classification is not complete.
* `trace` selects logging categories only and has no trace-file output; history
  clear is unsupported by stock Hatari.
* FPU register readback is absent from the stock debugger, so P4.5 cannot
  promise full CPU/FPU/MMU state.
* P4.13 source-level mapping is blocked on an `m68k-atari-mint` toolchain that
  emits DWARF; only a narrow Linux-ELF parser spike exists in P8.2.
* P4.12 crash classification and full semantic state diffs remain incomplete;
  P4.10 output caps must stay in force while the pager is disabled.

### Next

1. Keep unsupported watchpoint/FPU/trace-file results explicit; pursue the
   watchpoint patch only with an ADR.
2. Finish crash classification and checked state-diff coverage on the existing
   debugger surface.
3. Revisit source mapping only after the Atari-mint DWARF toolchain exists.

## Adversarial review

**Review method:** the requested `Task` tool (`subagent_type: general`) is not
available in this environment, so an independent subagent review could not be
run. The implementer performed the documented adversarial self-review below.
It must be replaced by an independent review before P4.1 coding starts.

| Finding | Why it matters | Incorporated as |
| --- | --- | --- |
| Hatari breakpoints track value changes, not reads. | P4.1 literally asks for read/read-write watchpoints, which stock Hatari cannot do. | `w`/`rw` are documented change proxies; `r` returns `unsupported_read_watchpoint` with `[P]/[R]`. |
| Conditional breakpoints allow only four conditions. | Range watchpoints cannot be implemented as a single native range. | Range expansion with caps; explicit rejection beyond the cap; `[P]` for wide ranges. |
| FPU data/control registers are not exposed by `cpureg` or `info`. | P4.5 "FPU state" would otherwise be fabricated. | CPU state returns `fpu_state.available=false` with a patch candidate; no memory inference. |
| Hit counts are not a native debugger feature. | P4.2 "hit counts" could be reported as exact when they are not. | `every_nth` uses `:count`; stop-after counts observed stops; confidence field. |
| `bt` is in Hatari's manual but not in `tools-reference.md`. | A frame-pointer backtrace may be less accurate and outside the contract. | Profile backtrace is primary; `bt` is optional `[R]` after a spike. |
| `:file` scripts can run `quit`, `reset`, `logfile`, `parse`, `setopt`. | Breakpoint actions are a host-control escape hatch. | Server-generated actions only; strict command allowlist; no raw model scripts. |
| Profiling and history must be enabled before the event. | Post-crash backtrace/history will be empty or misleading. | Tools report prerequisites; explicit boot-time enable; no retroactive claim. |
| DSP support depends on Falcon and the build; DSP break semantics are unverified. | P4.4 could break on normal profiles or misreport stop state. | `dsp_unavailable` probe; combined-stop integration test before advertising. |
| Full state snapshots are opaque and version-specific. | A semantic state diff would be unreliable. | Register/memory diffs are reliable; full-state diff is `[R]` raw only. |
| Auto-continue on exceptions can loop forever. | CPU burn and missed failures. | Explicit opt-in, cooldown, max reports, then stop. |
| The debugger has no pager, but readline/TTY can block or pollute stdout. | Tools may hang or corrupt the console stream. | Non-TTY reader, output caps, prompt detection, no host pager. |
| Source mapping needs DWARF plus a load base; AHCC provides symbols only. | Source-level stepping could be promised without evidence. | Research-only; fail closed; symbol-level remains the contract. |

**2026-10-27 advanced-debugger implementation self-review.** The `Task` tool
is still unavailable in this environment, so this is a self-review, not an
independent review. It corrects the earlier assumption that `backtrace` is
profiler-derived: in this tree `backtrace`/`bt` walks the A6 frame-pointer
chain (`stacktrace.c`), while `profile stack` is the profiler view.

| Finding | Why it matters | Incorporated as |
| --- | --- | --- |
| `dspcont` leaves the prompt but is absent from `Supervisor.TERMINAL`. | Sending it through `Supervisor.debug()` would append a sentinel that cannot arrive and time out. | The new module calls the supervisor terminal helper directly; a core change should add `dspcont` to terminal classification or expose a public terminal-command API. |
| The stock `trace` command has no file operand; `--trace-file` is read by `Log_Init()` at startup. | A runtime `file=` argument would silently write somewhere else or nowhere. | `dbg_trace_start(file=...)` resolves the path with `app.guard_path()` and returns typed `trace_file_unsupported`; restart guidance is included. |
| DSP is enabled only for Falcon (`Config_IsMachineFalcon()` in `dsp.c`), not TT. | Advertising DSP on TT would send commands that cannot work. | Non-Falcon profiles return `dsp_unavailable`; unknown profiles probe `dspreg` before `dspcont`/`dspstep`/list. |
| `history <count>` prints only entries not already shown. | Repeated reads look empty even when history exists. | Result includes a note and `completeness: unknown`; no claim of a complete dump. |
| There is no debugger history-clear command. | A fabricated clear would mislead clients. | `dbg_command_history_clear` returns typed `unsupported` with the reallocation workaround. |
| `backtrace` is frame-pointer based and depends on a valid A6 chain and symbols. | Treating it as profiler output would overstate confidence. | Result labels `source: frame_pointer`, `confidence: best_effort`; errors and truncation are returned separately. |
| `dbg_state_diff` can only compare bytes. | It cannot provide a semantic CPU-state diff. | The tool stores `/tmp` `savebin` snapshots, returns offsets/ranges/bytes, and opens no arbitrary host file. |
| Breakpoint `:file` actions execute debugger scripts. | A model-supplied script is a host-control escape hatch. | `dbg_breakpoint_actions` previews `:file` only, never executes it, and excludes `info file`; safe actions run only with `apply=true`. |
| Snapshot baselines live in a bounded process-local registry. | Baselines do not survive a server restart and are evicted after 16 entries. | Registry cap unlinks evicted files; clients should re-baseline after restart or eviction. |
| `_require_dsp` probes with `dspreg`. | Adds one debugger command to DSP operations. | Probe is explicit and safe; its result is not reused to avoid stale state. |
