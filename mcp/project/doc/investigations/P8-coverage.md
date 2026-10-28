# P8.3 — Raw stock-profiler coverage spike

Status: complete as a raw stock-Hatari spike; P6.11 backend integration untested
Verdict: INCONCLUSIVE (function-level data checks pass; manual review not done; P6.11 integration NO-GO)
Date: 2026-10-27
Owner: P8 research spike
Review: independent review pending; self-review recorded in the P8 plan
Harness: `mcp/project/labs/coverage_gap_spike.py`

## Scope correction

This spike does **not** exercise the P6.11 coverage backend or the P6 scenario
runner. It starts stock Hatari directly, enables the debugger profiler at a
breakpoint, and separately runs `--trace cpu_disasm`. P6.11 integration is
therefore **untested**, and it is a NO-GO for this spike. The result below is
evidence about the raw stock profiler only.

## Question and hypothesis

Can the stock profiler identify executed functions and zero-sample functions in
a known AHCC binary without claiming line or branch coverage? Hypothesis: the
CPU profiler can give stable function-level execution data, while full
instruction tracing is too large and slow to be a practical routine backend.

## Pinned environment

| Item | Value |
| --- | --- |
| Host | Linux 6.17.0-1020-oracle, aarch64 |
| Hatari | v2.6.1-devel (2026-10-27) |
| Machine | STE, 2 MiB, TOS 1.62 |
| TOS SHA-256 | `1b284510424136a8426360f30a801f8c34272f97591d42d1214d110c57c21bb2` |
| Binary | AHCC `HELLO_ST.TOS`, SHA-256 `16d082b5422f0b3d9d53e6f9b3a28f08d1055a3d4f8bc576bf71a4083f9004a8` |
| Map | `HELLO_ST.MAP`, SHA-256 `23466b069930ccdd70581010727db8af853f8e3543420178a4f4f7fe79b1fc68` |
| Program load base | `0x00f176` (observed; text ends `0x010528`) |
| Workload | 600 VBL, autostart `C:\HELLO_ST.TOS` |
| Flags | `--sound off --fast-boot on --statusbar off --drive-led off` |

The load base and exit offset are configurable in the harness (`--base`,
`--exit-offset`) but remain environment-specific. The timeout for a future
promotion must derive them from the binary/map instead of hard-coding them.

## Method

Command used for this retained run:

```sh
cd mcp/project
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs coverage \
  --out /tmp/hatari-mcp-p8-final/coverage
```

Retained JSON was then audited with the stricter analysis without re-running
the 1 GiB trace:

```sh
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs coverage \
  --from-json /tmp/hatari-mcp-p8-final/coverage/coverage/coverage.json \
  --out /tmp/hatari-mcp-p8-audit/coverage
```

1. Copy `HELLO_ST.TOS` to a temporary GEMDOS workspace under `/tmp`.
2. Three **profile** runs: break at the program entry (`0x00f176`), enable the
   debugger profiler, continue, break at the end of `exit` (`0x00f176+454`),
   then save the profile.
3. Three **baseline** runs with the same breakpoints but profiling disabled.
4. Three **trace** runs with `--trace cpu_disasm --trace-file …` for the same
   600-VBL workload; parse the trace, then delete it.
5. Map executed PCs to `HELLO_ST.MAP` function offsets using an explicit load
   base (the old trace parser inferred the base from the lowest plausible PC;
   the harness now passes the base explicitly).
6. Compare active functions and **hit counts** across profile and trace, require
   a minimum number of mapped active functions, and record whether a manual
   disassembly review was done.
7. Gate the function-level GO on a validated P6.11 evidence record, measured
   entry→exit CPU overhead within 2x, and a scenario-step mapping for every
   zero-sample gap; a fabricated JSON with positive counts and
   `--manual-review-done` is not enough.

The profile window is program entry→exit. The trace window is the fixed
600-VBL run including boot, so the two workloads are not identical even though
the mapped function counts happened to agree. This is a known mismatch, not an
independence proof.

## Results

Raw retained data: `/tmp/hatari-mcp-p8-final/coverage/coverage/coverage.json`.
Audited copy: `/tmp/hatari-mcp-p8-audit/coverage/coverage.json`. The CLI now
writes `<out>/coverage.json`; the retained pre-fix tree keeps its historical
`coverage/coverage/coverage.json` path.

### Profile backend

| Quantity | Value |
| --- | ---: |
| Profile file | 94,360 bytes |
| Unique PCs | 997 |
| Instructions counted | 10,825 |
| Active functions | 10 |
| Zero-sample functions | 17 |
| Profile elapsed (3 runs) | 6.8005–6.8016 s |
| Baseline elapsed (3 runs) | 6.8003–6.8013 s |
| End-to-end wall ratio | 1.0002 |
| Active set and hit counts stable 3/3 | yes |
| Profile/trace CPU overhead in the entry→exit window | INCONCLUSIVE (retained run did not record it) |

The end-to-end time is boot-dominated, so the wall ratio is not profiler
overhead. The harness now records host CPU time at the entry and exit
breakpoints and will report an entry→exit CPU ratio when it is rerun. This
retained dataset cannot support an overhead claim, so overhead is labeled
**INCONCLUSIVE** rather than "negligible."

### Trace backend

| Quantity | Value |
| --- | ---: |
| Trace file median | 321,825,989 bytes = 306.9 MiB (321.8 MB decimal) |
| Trace lines median | 5,132,070 |
| Trace elapsed (3 runs) | 6.60–6.90 s (6.6017, 6.7491, 6.8957) |
| Fixed 600-VBL baseline (3 runs) | 0.529–0.530 s |
| Overhead ratio | 12.75× |
| Active functions | 10 |
| Active set stable 3/3 | yes |

A 10-second scenario at this rate would produce hundreds of MiB per run. Trace
is usable for a one-off forensic run, not routine scenario coverage. Do not run
this trace again without a concrete need; audit retained JSON instead.

### Function sample (combined hit counts over three runs)

| Function | Profile | Trace | Exact hit-count agree |
| --- | ---: | ---: | --- |
| fputc | 159 | 159 | yes |
| fclose | 75 | 75 | yes |
| _printf | 72 | 72 | yes |
| Exit_iob | 69 | 69 | yes |
| <_fflush> | 42 | 42 | yes |
| write | 33 | 33 | yes |
| <c_f> | 30 | 30 | yes |
| printf | 27 | 27 | yes |
| fflush | 24 | 24 | yes |
| main | 12 | 12 | yes |

The sample contains ten active functions (minimum five) and no zero/zero rows.
Profile/trace hit-count agreement is 10/10. The zero-sample list still includes
`Init_iob`, `XA_alloc`, `XA_free`, `_uldiv`, `_ulmod`, `close`, `free`,
`isatty`, `isdigit`, `isupper`, `lseek`, `ltoa`, `malloc`, `strlen`, `strlwr`,
`strrev`, and `ultoa`.

**Manual review was not performed.** No human checked a disassembly/coverage
sample against the map. The harness records `manual_review_done=false` and
withholds a function-level GO until that review is supplied via
`--manual-review-done`.

## GO / NO-GO

- **P6.11 backend integration: NO-GO/untested.** This spike is a raw stock
  profiler experiment only.
- **Raw function-level data checks: pass** (`go_function_level_data_only=true`):
  positive PCs/instructions, ten mapped active functions, stable active set and
  hit counts, exact 10/10 hit-count agreement.
- **Function-level GO: withheld** (`go_function_level=false`). The gate now
  also requires a validated P6.11 evidence record, measured entry→exit CPU
  overhead within 2x, and a scenario-step mapping for every zero-sample
  function. None is present in the retained run, so fabricated positive counts
  plus `--manual-review-done` cannot produce a GO.
- **Line/branch coverage: NO-GO.** The AHCC binary has no DWARF, Hatari
  symbols are address-only, and the profiler provides instruction sampling at
  addresses, not source lines.
- **Trace-as-coverage: NO-GO.** 306.9 MiB and 12.75× for 600 VBL.
- **Overall verdict: INCONCLUSIVE.**

## Limitations and safety

- Function ranges come from `HELLO_ST.MAP` plus the observed load base
  `0x00f176`; this is specific to TOS 1.62 and this HELLO build.
- The profiler must be enabled at the debugger prompt and saved before another
  control command resets its buffers; the spike documents that workflow.
- Zero-sample means "no sampled execution in this scenario", not "unreachable".
- The profile and trace windows differ (entry→exit vs fixed 600 VBL including
  boot); the trace is a separate mechanism, not the same workload.
- No overhead number is claimed from the retained run; it is INCONCLUSIVE.
- The 2x overhead budget is a spike gate, not a production service-level
  target; a promoted plan must declare its own budget.
- `??`/line-0 is a DWARF concern, not coverage; coverage remains address-based
  sampling and cannot claim line/branch coverage.
- No instrumentation patch, coverage gate, mutation testing, or production
  code was added.
- Trace files and profiles are under `/tmp`; no third-party data is committed.
  Remove `/tmp/hatari-mcp-p8-*` when the investigation is closed.

## Promotion prerequisites

A promoted coverage plan needs a real P6.11 integration test, a stable
symbol/load-base resolver, a scenario artifact format, a documented
profile-session protocol, a manual zero-sample review workflow tied to scenario
steps, and an explicit statement that results are function-level sampling.
Line/branch coverage requires a DWARF or instrumentation path and is out of
scope without a new ADR.
