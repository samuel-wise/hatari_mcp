# P8.10 — Benchmark and regression-detection spike

Status: complete as a harness-feasibility spike; not a benchmark GO
Verdict: INCONCLUSIVE (shared host; retained run predates randomized A/B interleaving)
Date: 2026-10-27
Owner: P8 research spike
Review: independent review pending; self-review recorded in the P8 plan
Harness: `mcp/project/labs/bench_spike.py`

## Question and hypothesis

Can three headless machine profiles be measured with low enough variance to
detect a real regression, and what does a simple regression threshold look
like? Hypothesis: a fixed VBL workload is stable on an idle host, while
debugger and screenshot round-trips are dominated by the stock 50 Hz control
poll and therefore have high relative variance.

## Pinned environment

| Item | Value |
| --- | --- |
| Host | Linux 6.17.0-1020-oracle, aarch64, 2 × Neoverse-N1 |
| Hatari | `build-install/bin/hatari`, v2.6.1-devel (2026-10-27) |
| Python | 3.12.3 |
| Repo commit | `368942f1` (work tree has uncommitted P8 lab files) |
| ST ROM | EmuTOS `etos192us.img`, SHA-256 `2241df90967a81b29423e0844a75e1e2faabdc03a87fe7c95647860dcaa995c1` |
| STE ROM | TOS 1.62 `tos162us.img`, SHA-256 `1b284510424136a8426360f30a801f8c34272f97591d42d1214d110c57c21bb2` |
| Falcon ROM | TOS 4.04 `tos404.img`, SHA-256 `9e076c46e60c6b2c44d3b5f2307efa4edb5dc53f4e9257ab5b468dcd030be57a` |
| ST profile | `--machine st --memsize 1`, EmuTOS |
| STE profile | `--machine ste --memsize 2 --blitter on`, TOS 1.62 |
| Falcon profile | `--machine falcon --memsize 4 --dsp emu`, TOS 4.04 |
| Common flags | `--sound off --confirm-quit off --fast-boot on --timer-d on --rtc-year 1990 --statusbar off --drive-led off` |
| Control | Stock `--control-socket`; stderr sentinel correlation |

This is a shared development host, not a dedicated benchmark runner. Absolute
numbers must not be compared across hosts.

## Method

Command used for the retained run:

```sh
cd mcp/project
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs bench --reps 10 \
  --out /tmp/hatari-mcp-p8-final/bench
```

Per profile, ten repetitions of each measurement:

1. **Fixed workload** — spawn with `--run-vbls 500 --benchmark`, measure spawn
   to process exit. This includes boot and 500 emulated VBLs. The harness now
   asserts every return code is zero and records it; the retained run predates
   that check, so its return codes were not stored.
2. **Startup/boot phase** — spawn to acceptance of the control socket. This is
   emulator-ready, not desktop-ready. The harness reports the post-connect
   scenario time separately (`total_s - connect_s`).
3. **Debug latency** — ten `hatari-debug cpureg` round-trips after a 250 ms
   settle, measured with the stderr sentinel.
4. **Screenshot latency** — five debugger `screenshot` commands to PNG.
5. **Peak RSS** — `/proc/<pid>/status` `VmHWM`, polled at 5 ms.
6. **Host CPU time** — the harness now records child CPU time with
   `RUSAGE_CHILDREN`; the retained run predates this field.

Regression injection: the harness now runs baseline and injected ST workloads
in a reproducible randomized **interleaved** order (default ten pairs), asserts
all return codes are zero, and uses a median ratio plus a two-sided permutation
test (20,000 resamples, alpha 0.05, minimum ratio 1.05). The retained run used
an earlier non-interleaved ordering, so it is not treated as a regression GO.

## Results

Raw retained data: `/tmp/hatari-mcp-p8-final/bench/bench/bench.json`.

### Fixed workload and startup

| Profile | Workload median | Workload CoV | Workload min–max | Startup median | Startup CoV | Peak RSS median |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ST | 0.4292 s | 4.03% | 0.4284–0.4845 s | 14.3 ms | 6.8% | 24.84 MiB |
| STE | 0.4304 s | 0.62% | 0.4289–0.4383 s | 15.0 ms | 17.0% | 26.10 MiB |
| Falcon | 4.9378 s | 0.89% | 4.8921–5.0434 s | 14.5 ms | 4.9% | 26.89 MiB |

The ST set contains one 0.4845 s outlier (+12.8%). Excluding it, the ST CoV is
about 0.2%. The spread is host noise, not emulator variance. The ST peak-RSS
minimum (11.69 MiB) is an early-start sampling artifact; the median is stable.

Host CPU time and the boot/scenario split were not captured in the retained
run. The revised harness records both; no CPU numbers are claimed here.

### Debug and screenshot latency

| Profile | `cpureg` median | `cpureg` max | Screenshot median | Screenshot max |
| --- | ---: | ---: | ---: | ---: |
| ST | 16.73 ms | 32.86 ms | 17.19 ms | 32.79 ms |
| STE | 16.73 ms | 33.28 ms | 16.79 ms | 33.81 ms |
| Falcon | 16.98 ms | 31.30 ms | 19.27 ms | 33.48 ms |

The medians sit near one 50 Hz frame (20 ms), and the observed maxima sit near
two frames. These are maxima for the retained sample, not p95 percentiles. This
is expected for stock Hatari: socket commands are polled from the emulation
loop, not handled immediately. Relative CoV is 28–41%; absolute jitter is
bounded at about one to two frames.

### Injected slowdown (retained, non-interleaved, reused baseline)

| Quantity | Value |
| --- | ---: |
| Baseline median | 0.42916 s |
| Injected median | 0.48673 s |
| Median ratio | 1.1341 (+13.4%) |
| Permutation p-value | 0.0104 |
| Detected at ratio ≥ 1.05, p < 0.05 | yes |

The retained `baseline_s` list is byte-identical to the ST profile workload
list above; it is a **reused baseline**, not an independent A/B sample. The
retained injected numbers were collected later, without interleaving. The 13.4%
shift and p = 0.0104 therefore demonstrate the detector on one reused dataset;
they are **not independent A/B evidence** and must not be cited as such. Only
the revised interleaved harness can support a regression claim.

## GO / NO-GO

- **Harness feasibility: positive but not a GO.** The fixed workload is stable
  on STE/Falcon and the injected shift is detected.
- **Benchmark GO: NO / INCONCLUSIVE.** Repeated runs during the session
  reached **5.4% CoV**, which contradicts a <5% GO threshold. The host is
  shared, the retained run was not interleaved, and CPU/boot split were not
  captured. The revised gate requires exactly three profiles (ST, STE, Falcon)
  x at least 10 reps, zero workload and interactive return codes, CoV <5%, a
  detected injected regression, and an explicit `--dedicated-runner`
  declaration before it can set `go=true`.
- **Shared-CI gating: NO-GO.** Do not gate on this host.

Suggested threshold only after a dedicated-runner run:

- compare medians of at least 10 interleaved samples;
- flag when median ratio ≥ 1.05 and permutation p < 0.05;
- reject the run when baseline CoV ≥ 5%;
- never compare absolute times across hosts.

## Limitations and safety

- Shared host, no CPU isolation; outliers are real but not attributable.
- The retained run used non-randomized ordering and reused the ST workload
  values as its injected baseline; the new harness interleaves baseline and
  injected runs reproducibly and does not reuse a profile sample.
- The retained latency table reports observed maxima; its values are not p95
  percentiles.
- The injection is a host SIGSTOP duty cycle, not an emulator option.
- RSS sampling can miss the earliest allocation; the median is used.
- No production code, MCP tool, packaging, or test gate was added.
- No ROM or third-party data was written into the repository.
- Raw measurements are under `/tmp`, not git. Remove `/tmp/hatari-mcp-p8-*`
  when the investigation is closed.

## Promotion prerequisites

A promoted benchmark plan needs a dedicated runner, cgroup or affinity policy,
interleaved sampling, stored return codes and CPU time, sample retention, a
documented statistical test, and a decision on whether regression gating
belongs in shared CI. None of that is authorized here.
