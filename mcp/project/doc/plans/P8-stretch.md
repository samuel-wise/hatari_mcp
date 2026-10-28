# P8 — Stretch and research

Status: partial (P8.0, P8.1, P8.2, P8.3 and P8.10 spikes executed; P8.1, P8.3 and P8.10 are INCONCLUSIVE; P8.4–P8.9, P8.11 and P8.12 disposition only; no production implementation authorized) — latest fix pass under final review
Owner: P8 research owner (spike subagent)
Reviewer: independent adversarial review completed; latest fixes under final review
Last updated: 2026-10-27
Roadmap: P8.1–P8.12

Depends on: P6 (scenarios, artifacts, reports, coverage interface), P7
(framed protocol, notifications, binary readback, frame-locked input, clock),
P4/P3.11–P3.13 (debugger depth, source mapping, corpus harness)
Feeds: nothing on the critical path. P8 is explicitly off the delivery path.

## Goal

Evaluate the stretch ideas without turning them into accidental commitments.
Each P8 item must have a clear classification (research spike, conditional
feature, or out of scope), a bounded experiment, a measurable question, and a
GO/NO-GO result. A GO does **not** authorize implementation: it requires a new
ADR and a full plan with tests, security review, licensing review, and an owner.

P8 exists to prevent unbounded "wouldn't it be cool" work. A spike that ends
NO-GO is a successful spike.

## Dependencies

| Dependency | Needed by | Hard? | Notes |
| --- | --- | --- | --- |
| P6.3/P6.9 | P8.1, P8.3, P8.9 | Hard | Artifacts/replay are the measurement substrate. |
| P6.11 | P8.3 | Hard | If coverage returns `unsupported`, P8.3 is NO-GO until P6.11 changes. |
| P7.2/P7.3 | P8.6 | Hard | Frame notifications + binary framebuffer. |
| P7.4/P7.5 | P8.1, P8.7, P8.8, P8.12 | Hard for exactness | Without them, results are approximate and must be labeled. |
| P7.1/P7.6 | P8.7 | Hard | Multiplexing/broker needs framing; Windows pair mode needs P7.6. |
| P3.11/P4.13 | P8.2 | Hard | DWARF mapping and source-level stepping are shared. |
| P5.5–P5.7 | P8.4 | Hard | Serial/SCC streams exist; guest driver is the unknown. |
| m68k-atari-mint toolchain with DWARF | P8.2 | Soft | AHCC may emit no DWARF; if absent, NO-GO or use another corpus. |
| Dedicated benchmark host | P8.10 | Soft | Shared CI is too noisy for regression claims. |
| Physical hardware + capture rig | P8.11 | Hard | Not available; item is out of scope. |
| Optional `pyelftools` | P8.2 | Soft | Only if already acceptable; do not add a dependency for a spike without an ADR. `readelf`/`addr2line` may be used as reference. |

## Deliverables and file ownership

P8's actual deliverables are investigation records and disposable spike
harnesses. **No production code under `src/hatari_mcp/` is authorized by this
plan.** The candidate paths below are reservations for a future promoted plan,
not commitments.

### Actual spike deliverables (guarded, non-shipping)

| Path | Purpose |
| --- | --- |
| `labs/__init__.py` | Optional spike package; excluded from production exports and packaging. |
| `labs/harness.py` | Stock-Hatari process, control-socket, CPU-time and state helpers. |
| `labs/stats.py` | Median/CoV, permutation test and regression detector. |
| `labs/time_travel_spike.py` | Snapshot cadence, size/latency, observable restore, replay check. |
| `labs/dwarf_spike.py` | ELF/DWARF line mapping and parser-agreement experiment. |
| `labs/coverage_gap_spike.py` | Raw stock-profiler PC→symbol mapping; P6.11 integration untested. |
| `labs/bench_spike.py` | Fixed-workload benchmark, interleaving and variance probe. |
| `tests/labs/` | Harness logic, parser, statistics, packaging and isolation tests; not part of the release gate. |
| `doc/investigations/P8-benchmark.md`, `P8-coverage.md`, `P8-dwarf.md`, `P8-time-travel.md`, `P8-other-items.md` | Investigation reports with data, limits and GO/NO-GO. |

Unstarted spikes (P8.4–P8.9 and P8.12) reserve a future `labs/<name>_spike.py`
path but no file has been created. Do not treat a reserved path as delivered.

Rules: lab modules are import-guarded by `HATARI_MCP_LABS=1`, have no MCP tool
registration, are excluded from the wheel/package, and are deleted or promoted
after the investigation. They must not become shadow production code.

### Reserved production paths (only after a GO + ADR + new plan)

| Item | Candidate path |
| --- | --- |
| P8.1 | `src/hatari_mcp/time_travel.py` |
| P8.2 | `src/hatari_mcp/source_debug/{dwarf,lines,variables,sourcemap}.py` |
| P8.3 | `src/hatari_mcp/coverage/gaps.py` |
| P8.4 | `src/hatari_mcp/network/scc.py` |
| P8.5 | `src/hatari_mcp/remote/{service,tenancy,quota}.py` |
| P8.6 | `src/hatari_mcp/stream.py` |
| P8.7 | `src/hatari_mcp/pair/{broker,lease}.py` |
| P8.8 | `src/hatari_mcp/input/host_capture.py` |
| P8.9 | `src/hatari_mcp/llm/{suggest,minimize,cluster,explain}.py` |
| P8.10 | `src/hatari_mcp/bench/{runner,stats,report}.py` |
| P8.11 | `src/hatari_mcp/hardware/` (reserved; no work planned) |
| P8.12 | `src/hatari_mcp/input/analog.py` |

## Design / constraints

### Spike protocol

Every spike follows the same contract:

1. **Question and hypothesis** — one sentence each, tied to a user need.
2. **Timebox** — 2–5 working days, stated before starting. Stop at the box.
3. **Pinned environment** — Hatari commit, EmuTOS checksum, MCP commit,
   corpus artifact hashes, host details. No floating versions.
4. **Experiment** — scripted, repeatable, with raw measurements. Manual steps
   are allowed only for hardware/privacy items and must be written down.
5. **Safety** — no network exposure, no host shell, no corpus mutation, no
   proprietary ROM/software committed, bounded resource use.
6. **Result** — `GO`, `NO-GO`, or `INCONCLUSIVE` with the measurement that
   decides it, not an opinion.
7. **Artifacts** — investigation doc under `doc/investigations/`, raw data
   under `/tmp` or a gitignored `fixtures/` location, never large third-party
   data in git. Lab code is disposable.
8. **Promotion** — a GO produces an ADR and a new plan; it does not modify
   production code in this phase.

### Portfolio classification and order

| ID | Item | Classification | Depends on | Effort | Candidate path |
| --- | --- | --- | --- | --- | --- |
| P8.1 | Time-travel debugging | Research spike (conditional) | P6.9, P7.5 | M/L | `time_travel.py` |
| P8.2 | DWARF source debugging | Research spike (highest value) | P3.11, P4.13, toolchain | M | `source_debug/` |
| P8.3 | Coverage/test-gap analysis | Research (blocked on P6.11) | P6.11, P6.3 | S/M | `coverage/gaps.py` |
| P8.4 | Network/SCC tests | Research (limited; not IP) | P5.5–P5.7 | M | `network/scc.py` |
| P8.5 | Remote multi-user service | **Out of scope for v1** (isolation spike only) | P7.10 | XL | `remote/` |
| P8.6 | Framebuffer streaming | Spike (conditional) | P7.2, P7.3 | M | `stream.py` |
| P8.7 | Attach/pair mode | Spike (needs broker/patch) | P6.12, P7.1 | M/L | `pair/` |
| P8.8 | Host input capture | Spike (manual, privacy-gated) | P7.4 | S/M | `input/host_capture.py` |
| P8.9 | LLM-native test tooling | Spike (orchestration, no patch) | P6.3, P6.9 | M | `llm/` |
| P8.10 | Benchmarking | Spike; likely production | P6.3 | S/M | `bench/` |
| P8.11 | Real-hardware bridge | **Out of scope** | hardware | XL | `hardware/` (reserved) |
| P8.12 | Analog devices | Research (needs patch + test program) | P7.4 | M | `input/analog.py` |

Recommended order if demand appears: **P8.10 → P8.9 → P8.2 → P8.1 → P8.3 →
P8.6 → P8.7 → P8.8 → P8.12**. P8.4 is a narrow feasibility question; P8.5 and
P8.11 are not started without a changed product decision.

### P8.1 — Time-travel debugging

**Question:** Can periodic machine snapshots plus input replay provide useful
"step backward" at acceptable cost? **Not** instruction-level reverse
execution.

**Minimal spike:** On ST/STE with EmuTOS, run a deterministic demo/scenario for
10 seconds. Snapshot at 25/50/100 VBL intervals with `statesave`; measure file
size, save time, and load time. Rewind to a target frame by loading the nearest
snapshot and replaying the recorded input log (P6.9) to the target; compare
decoded screen hashes and a small memory region at three checkpoints. Repeat
with P7.5 available and with the stock `_vbl` fallback.

**What it proves:** the snapshot cost/cadence trade-off, the achievable rewind
latency, and whether replay is deterministic enough to make rewind trustworthy.
It does not prove reverse execution of individual instructions.

**GO:** rewind to a snapshot boundary in under 2 seconds for the test workload;
an **input-driven** replay with P7.5 matches at three checkpoints; snapshot
size and cadence fit the declared disk budget. The replay must be supplied as
an evidence record (bundle path, checkpoints, screenshot/memory hashes, event
count, matched replay status); a bare `--input-replay-proven` flag is
insufficient. A static, input-free screenshot match is explicitly insufficient.
Without P7.5 input replay evidence the overall result is INCONCLUSIVE, even
when snapshot cost passes. **NO-GO:** snapshot >64 MiB
or save+observable-load >2 seconds at the finest useful cadence; input replay
diverges at a checkpoint even with P7.5; host side effects (disk/media writes)
cannot be reverted or isolated. **Out of scope:** instruction-level reverse,
undoing network/printer/disk effects, production checkpointing, memory
diffing.

Candidate: `labs/time_travel_spike.py`; investigation
`doc/investigations/P8-time-travel.md`.

### P8.2 — DWARF source debugging

**Question:** Can we map m68k-atari-mint ELF/DWARF line info and variables to
guest addresses reliably enough for file:line breakpoints and local-variable
inspection, given that Hatari symbols have no line numbers and AHCC may emit no
DWARF?

**Minimal spike:** Build one small C program with the m68k-atari-mint
toolchain and `-g`; confirm DWARF sections. Parse line tables and one
function's locals (via `pyelftools` only if acceptable, otherwise
`readelf`/`addr2line` as reference plus a minimal parser). Apply the PRG load
base and compare 50 file:line → address mappings with `addr2line`. Set a
breakpoint by file:line, stop, and read one local from the stack/frame. Try a
second corpus binary built with optimization to see the failure mode.

**What it proves:** mapping accuracy with relocation, whether locals are usable
for simple frames, and whether the toolchain/corpus actually carries DWARF.
It does not prove a source view, optimized-variable correctness, or AHCC
support.

**GO (narrow):** the line-table parser agrees with `readelf` and `addr2line` on
every row of a full sample of at least 50 rows from an m68k Linux ELF. `??`,
empty file names and line 0 are unresolved, so the agreement threshold is
100%, not 90%, and the denominator is the full sample. This is **parser agreement on m68k
Linux ELF only**. It is not a GO for Atari PRG relocation, AHCC, or reading a
local from a stopped guest. **NO-GO:** fewer than 50 rows, any unresolved
sample, no DWARF in the available corpus, relocation/overlay makes mappings
unreliable, or variables require an unparseable debug-info format.
**Out of scope:** a source editor, expression evaluation beyond P1/P4,
optimized-code perfection, AHCC debug info, languages other than C.

Candidate: `labs/dwarf_spike.py`; investigation
`doc/investigations/P8-dwarf.md`.

### P8.3 — Coverage and test-gap analysis

**Question:** Can the P6.11 coverage backend identify untested functions in a
scenario and feed a useful test-gap report, without claiming line/branch
coverage it cannot measure?

**Minimal spike:** Run one P6 scenario with `profile` and, separately, a
bounded `trace` set. Map hot PCs to symbols and list functions with zero
samples. Manually review a 10-function sample. Repeat three times; measure
overhead, trace size, and stability. Correlate with P6 artifact checkpoints so
gaps are tied to scenario steps.

**Executed spike (raw stock-profiler only):** the spike that was run starts
stock Hatari directly and does **not** use the P6.11 backend or the P6 scenario
runner. The P6.11 integration is therefore **untested and NO-GO**; the report
is evidence about the raw stock profiler, not about the product coverage
interface. The run also did not perform the manual disassembly review, so its
function-level GO is withheld (INCONCLUSIVE).

**What it proves:** whether raw profile/trace function sampling is stable and
attributable enough to justify a future P6.11 integration. It does not prove
P6.11 integration, line/branch coverage, or test quality.

**GO:** P6.11 actually supplies the samples through a validated evidence
record (backend, scenario, artifact, profile/trace run ids, positive sample
source); profile and trace are both non-empty with positive unique
PCs/instructions; at least five mapped active functions; hit counts (not
zero/zero presence) agree; results are stable across three runs; a manual
disassembly sample is reviewed; overhead is a measured entry→exit host-CPU
result within a declared budget (the spike uses 2x, not boot-dominated wall
time); every zero-sample gap maps to a scenario step. Fabricated positive
counts plus `--manual-review-done` are not sufficient. **NO-GO:** P6.11 returns `unsupported`; empty/zero data;
sampling misses entire short functions so gaps are misleading; overhead makes
scenarios impractical. **Out of scope:** instrumentation patches, line/branch
coverage, coverage gating in CI, mutation testing.

Candidate: `labs/coverage_gap_spike.py`; investigation
`doc/investigations/P8-coverage.md`.

### P8.4 — Network/SCC tests

**Question:** Can two emulated machines exchange bytes over the SCC LAN path
deterministically, and can we capture that traffic at the host boundary?

**Minimal spike:** Use MegaSTE/TT profiles with `--scc-a-lan-in/out` connected
through a PTY relay (or paired FIFOs) and a guest test program/driver that
writes and reads a known byte pattern. Verify bidirectional, byte-exact
transfer for 60 seconds at a fixed baud rate. Capture at the relay and compare
the bytes. If no guest driver exists, the spike stops and records that blocker.

**What it proves:** byte-level SCC link feasibility and host-side capture. It
does **not** prove Ethernet, IP, TCP/UDP, FreeMiNT networking, packet capture,
or internet access. SCC LAN is a serial link, not a NIC.

**GO:** byte-exact bidirectional transfer with no loss at the configured rate
for 60 seconds; capture matches; setup is scriptable and deterministic.
**NO-GO:** only file-backed streams without flow control; no guest driver;
timing loss makes the test meaningless; the corpus cannot use the link.
**Out of scope:** IP networking, real network access, packet analysis of TCP,
security testing, remote services.

Candidate: `labs/scc_link_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.5 — Remote multi-user service (out of scope for v1)

**Question:** If the product ever changes, can authenticated remote sessions be
isolated strongly enough to avoid cross-tenant file/socket/process access?

**Minimal spike (only if the product decision changes):** one container per
session, one token per tenant, separate workspaces/sockets, quotas; two
tenants attempt path traversal, socket access, process discovery, and artifact
access. Verify that isolation is enforced by the container/runtime, not by
Python path checks.

**What it proves:** whether the current architecture can be wrapped safely. It
does not prove a viable multi-tenant product, billing, HA, or acceptable abuse
handling.

**GO:** no cross-tenant access under the attempted attacks; quotas and cleanup
work; auth is enforced at the edge and inside. **NO-GO:** isolation depends on
application-level checks, shared UID/workspace, or unauthenticated debugger
access. **Out of scope:** v1 multi-user hosting (explicit non-goal), public
internet exposure, SSO, compliance, billing, Kubernetes operators.

Candidate: `labs/remote_isolation_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.6 — Live framebuffer streaming

**Question:** Can the server stream frames to one client with bounded memory and
useful latency over the MCP transport (WebSocket/resource subscription), using
P7.3 binary readback?

**Minimal spike:** ST 320×200, 8/16 bpp, 50 Hz target. Stream full frames and a
simple delta/region mode; measure bandwidth, server CPU, latency, drops, and
memory on loopback for 60 seconds. Add a deliberately slow client to test
backpressure. Compare with a screenshot-per-frame baseline.

**What it proves:** the bandwidth/latency envelope and whether backpressure is
possible. It does not prove multi-client fanout, codec quality, or remote WAN
behavior.

**GO:** bounded queue with explicit drops; <5% drops at 50 Hz on loopback;
<100 ms added latency; no unbounded memory; CPU within the declared budget.
**NO-GO:** the transport cannot apply backpressure, memory grows without bound,
or the server cannot sustain the target without stealing emulation budget.
**Out of scope:** video codecs, recording, multi-client broadcast, WebRTC,
remote internet streaming.

Candidate: `labs/fb_stream_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.7 — Attach/pair mode

**Question:** Can a human at a Hatari UI and an agent share one instance without
corrupting commands or fighting over input?

**Minimal spike:** Build a broker that owns the single Hatari control
connection (the P6.12 constraint) and multiplexes two clients. The spike must
launch Hatari through the broker (or use FIFO for an already-running instance);
it cannot seize an existing control connection from another listener. Implement
an input lease: one client at a time may inject input; the other is read-only.
Test command ordering, disconnect/reconnect, screenshot without focus stealing,
and a human grabbing the lease mid-scenario. Requires P7.1 framing for sane
multiplexing.

**What it proves:** whether pairing is a broker/arbitration problem rather than
a second-connection problem, and whether leases are sufficient. It does not
prove remote pairing, multi-user safety, or Windows support.

**GO:** no interleaved/corrupted commands; lease transitions are explicit and
respect input ownership; disconnect leaves the emulator usable; no focus steal.
**NO-GO:** the only path requires an unsafe Hatari patch, or input arbitration
cannot prevent stuck keys/clicks. **Out of scope:** remote multi-user pair
sessions, audio/video chat, recording a human session for replay (separate item).

Candidate: `labs/pair_broker_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.8 — Host input capture

**Question:** Can host gamepad/keyboard input be recorded and replayed
deterministically for demo capture, with acceptable privacy?

**Minimal spike:** With explicit opt-in, capture 30 seconds of host gamepad and
keyboard events through SDL, map them to P7.4 frame-tagged guest events, save
the log, and replay. Compare the recorded event log and visual checkpoints.
Manual hardware is acceptable; CI cannot provide a gamepad.

**What it proves:** mapping feasibility and replay fidelity for a short, local
session. It does not prove privacy compliance, background capture, or
cross-platform parity.

**GO:** replay reproduces the event sequence and checkpoints; capture is
foreground-only and visibly indicated; no persistent storage by default.
**NO-GO:** host timing cannot be mapped without loss, keylogging/privacy risk
is unacceptable, or replay requires elevated OS hooks.
**Out of scope:** background/OS-wide keylogging, credential capture, telemetry,
cloud upload, CI automation of host devices.

Candidate: `labs/host_input_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.9 — LLM-native test tooling

**Question:** Can LLM assistance synthesize valid scenarios, explain failures
from artifacts, minimize failing scenarios, and cluster crashes without
inventing evidence or executing unsafe steps?

**Minimal spike:** Use P6 artifacts from one failing scenario. (a) Generate five
candidate scenarios from a goal; validate and run all of them. (b) Delta-debug a
~100-step failing scenario to the smallest step/event set that still fails;
re-run to confirm. (c) Cluster 20 crash artifacts by the P6.10 signature.
(d) Produce a failure explanation that cites artifact fields only. Measure
validity, minimization ratio, cluster stability, and explanation grounding.

**What it proves:** whether the value is in orchestration over deterministic
artifacts (likely) rather than in the model. It does not prove autonomous code
fixes, open-ended test generation quality, or cost control.

**GO:** all generated scenarios pass schema validation and run; minimization
preserves the failure and reduces steps substantially; clusters are stable; no
explanation cites a file/artifact that does not exist; no host command
execution. **NO-GO:** the model routinely invents artifacts/addresses, produces
unsafe scenarios, or cost/latency exceeds the declared budget.
**Out of scope:** autonomous code changes, self-modifying server, unbounded
token spend, model training, ranking test quality without evidence.

Candidate: `labs/llm_test_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

### P8.10 — Performance benchmarking and regression detection

**Question:** Can we measure emulator/scenario performance with low enough
variance to detect a real regression on a dedicated runner?

**Minimal spike:** Fixed workload (boot + short scenario) across three profiles,
10 repetitions each, on a dedicated host. Measure boot time, scenario wall
time, CPU time, peak RSS, and achieved frame rate. Inject a deliberate ~10%
slowdown (e.g. extra work/option) and test detection with a non-parametric
test. Report coefficient of variation and the minimum detectable effect.

**What it proves:** whether benchmarking is actionable on controlled hardware.
It does not justify gating shared CI or comparing absolute scores across hosts.

**GO:** run on a declared dedicated runner with exactly three profiles (ST,
STE, Falcon) x at least 10 repetitions; coefficient of variation <5%; every
workload **and interactive** run returns code 0; baseline and injected samples
are randomized and interleaved; CPU time and the boot/scenario split are
recorded; the injected 10% regression is detected with a documented statistical
method; the harness is scriptable and stores raw samples. **NO-GO / INCONCLUSIVE:** shared host,
variance >5% (including the observed 5.4% run), non-interleaved ordering,
missing return codes/CPU time, or an unrepresentative workload. The current
shared-host result is harness feasibility only, not a benchmark GO.
**Out of scope:** shared-CI gating, cross-host leaderboards, GPU/audio
benchmarks, power measurements.

Candidate: `labs/bench_spike.py`; investigation
`doc/investigations/P8-benchmark.md`.

### P8.11 — Real-hardware bridge (out of scope)

**Question:** Could an emulator run be compared against a real Atari for timing
or compatibility? This requires physical hardware, capture, synchronization,
ROM/software licensing, and safety review.

**Minimal spike:** none planned. A useful spike would need a donated, documented
machine and a concrete comparison question; without both, any work is
speculative.

**Status:** out of scope. Do not start, do not reserve CI resources, do not
claim compatibility. Revisit only with hardware, a sponsor, and a new plan.
Candidate path remains reserved but empty: `src/hatari_mcp/hardware/`.

### P8.12 — Analog devices

**Question:** Can paddle/lightpen/STe analog joystick input be injected at a
frame boundary so a guest program observes correct values?

**Minimal spike:** Extend the P7.4 injection patch with paddle X/Y and lightpen
coordinates at VBL; write or find a small guest test program that reads the
relevant hardware registers and draws/records values. Sweep ranges, test
clamping/centering, and verify no regression to digital input.

**What it proves:** whether analog injection is a small extension of the input
patch and observable by a guest. It does not prove real-device fidelity,
calibration, or support for every analog device.

**GO:** values are observable in guest memory/screen; ranges and clamping are
documented; digital input is unaffected. **NO-GO:** no guest test program, the
patch destabilizes input/timing, or the required device model is absent.
**Out of scope:** physical paddle hardware, lightpen calibration, multi-device
simultaneous analog input, analog recording from host devices.

Candidate: `labs/analog_input_spike.py`; investigation
`doc/investigations/P8-other-items.md`.

## Task checklist

- [ ] P8.0 Write the spike protocol and lab-package guard (`HATARI_MCP_LABS=1`, no MCP tool registration, excluded from packaging/CI).
- [ ] P8.0 Record the demand/decision for each item; leave unrequested items unstarted.
- [ ] P8.1 Time-travel spike: snapshot cadence/size/latency, replay-to-frame, GO/NO-GO.
- [ ] P8.2 DWARF spike: ELF sections, 50 line mappings vs `addr2line`, one local, relocation, GO/NO-GO.
- [ ] P8.3 Coverage-gap spike: profile/trace mapping, 10-function manual sample, stability, GO/NO-GO.
- [ ] P8.4 SCC spike: PTY/FIFO relay, guest driver, 60 s byte-exact link, explicit non-IP scope.
- [ ] P8.5 Remote isolation spike: only if product decision changes; container isolation attacks; otherwise leave unstarted.
- [ ] P8.6 Framebuffer stream spike: 50 Hz, bandwidth, latency, backpressure, GO/NO-GO.
- [ ] P8.7 Pair-mode spike: broker, input lease, disconnect, focus behavior, GO/NO-GO.
- [ ] P8.8 Host-input spike: opt-in capture, privacy notice, replay, manual-hardware note.
- [ ] P8.9 LLM-tooling spike: scenario synthesis validation, delta-debug, crash clustering, artifact-grounded explanation.
- [ ] P8.10 Benchmark spike: 3 profiles × 10 reps, variance, injected regression, GO/NO-GO.
- [ ] P8.11 Real-hardware item: document out-of-scope rationale; do not start.
- [ ] P8.12 Analog spike: P7.4 extension, guest test program, range/clamp, GO/NO-GO.
- [ ] For every GO: write an ADR and a new phase plan; do not implement in P8.
- [ ] Delete or promote lab harnesses after each investigation; keep the package out of releases.
- [ ] Ensure no corpus mutation, no proprietary ROM/software commit, no large raw data in git.
- [ ] Record final independent-review sign-off before any promotion.

## Acceptance criteria

1. All twelve roadmap items have a written classification, dependency, timebox,
   and disposition (`GO`, `NO-GO`, `INCONCLUSIVE`, or `OUT OF SCOPE`).
2. No production code, MCP tool, or release behavior changes as a result of
   this plan; lab modules are excluded from packaging and have no tool
   registration.
3. Each executed spike produces `doc/investigations/P8-*.md` with a pinned
   environment, raw measurements, safety notes, and an evidence-based
   GO/NO-GO.
4. P8.5 and P8.11 are not started unless the product decision changes; their
   out-of-scope rationale is recorded.
5. P8.4 does not claim IP/Ethernet support; its report states exactly what the
   SCC byte link proves.
6. P8.2 reports a clear `unsupported` result if the corpus/toolchain has no
   DWARF; it does not fake source-level debugging.
7. P8.3 reports `unsupported` or `INCONCLUSIVE` if P6.11 cannot provide
   attributable coverage; it does not relabel sampling as line coverage.
8. P8.1 does not claim instruction-level reverse execution; rewind is bounded
   by snapshots and replay, and host side effects are documented as
   irreversible.
9. P8.9 outputs cite only existing artifacts; invalid or unsafe generated
   scenarios are rejected by the P6 validator.
10. P8.10 results are from a dedicated runner with a stated variance; shared-CI
    gating is not proposed without a separate decision.
11. Any promotion has an ADR, a full plan with unit/integration/corpus tests,
    a security review for remote/pair/host-input work, and a licensing review.
12. Raw data and third-party artifacts are not committed; the corpus tree hash
    is unchanged after every spike.

## Test plan

P8 is research, so it uses experiments rather than a release test gate. Any
promoted item must return with a full test plan of its own.

### Spike harness tests

| Area | Test |
| --- | --- |
| Lab isolation | `tests/labs/test_lab_guard.py`: importing `labs` without `HATARI_MCP_LABS=1` is inert; no tools register; a wheel build excludes `labs/`; importing lab code does not change the server tool set. |
| P8.1 | Hermetic checkpoint/cadence math; observable restore timing; static-only/input-replay verdict logic; evidence-record validation and bare-flag rejection. |
| P8.2 | Line-table parser tests; missing-DWARF error; full-sample denominator; `??`/line-0 unresolved failure; 100% agreement; 50-row minimum. |
| P8.3 | PC-to-symbol mapping with a fake map; empty/zero-data NO-GO; hit-count sample agreement; stability across three inputs; explicit trace base; fabricated-positive-count NO-GO; P6.11/overhead/gap-map GO gates; coverage JSON path. |
| P8.4 | Byte-pattern generator/comparator; PTY relay smoke (skip if unavailable); loss accounting. |
| P8.6 | Frame size/bandwidth math; delta encoding round-trip; bounded queue/drop accounting. |
| P8.7 | Lease state machine; command ordering log; disconnect/reconnect; no-stuck-input check. |
| P8.8 | Event-log mapping and replay comparison; redaction/privacy checks. |
| P8.9 | Scenario validator integration; minimization preserves a synthetic failure; cluster signature stability. |
| P8.10 | Statistics: CoV, non-parametric detection; reproducible balanced interleaving; injected-regression fixture; 3-profile x 10-rep gate; interactive return-code gate. |
| P8.12 | Range/clamp logic; register-value decoding; digital-input regression check. |

### Experiment protocol

Each spike records: hypothesis, environment (Hatari/MCP commit, EmuTOS SHA-256,
toolchain, host CPU/OS), exact commands, sample counts, raw results, analysis,
limitations, and GO/NO-GO. Repeat measurements at least three times unless the
item is manual/hardware. Report medians and spread, not a single best run.
Manual hardware runs include a timestamp, device, and operator note.

### Data and artifact handling

Raw data goes under `/tmp/hatari-mcp-p8-*` or a gitignored fixture directory.
Investigation docs may include small tables and hashes, not large binaries,
ROMs, third-party source, or screenshots that the license does not permit.
Corpus roots are read-only; a before/after tree hash is recorded. No network
services are exposed; no host input is captured without explicit, foreground
opt-in.

### Promotion test requirements

A GO must produce a new plan with unit tests (parsers, state machines,
statistics), integration tests (real Hatari/EmuTOS, patched/stock where
relevant), corpus tests (if it consumes corpus), security tests (remote, pair,
host input, analog), and a CI budget. P8 itself adds no required CI job except
an optional, non-gating lab smoke run.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Research expands without bound. | No delivery value, context/time burn. | Timebox every spike; stop at the box; GO/NO-GO with measurements. |
| A spike is mistaken for a commitment. | Roadmap drift and user expectations. | Status says research; promotion requires ADR + new plan; out-of-scope items explicit. |
| Time travel is oversold as reverse execution. | Unsafe debugging claims. | Scope to snapshot+replay; document host side effects; GO criteria require measured rewind. |
| DWARF is absent or relocation is unreliable. | P8.2 is unimplementable for the corpus. | Check sections/base first; return `unsupported`; no fake line info. |
| Coverage sampling is mistaken for line coverage. | False test-gap confidence. | P8.3 depends on P6.11; report limitations; NO-GO if not attributable. |
| SCC LAN is mistaken for Ethernet/IP. | Incorrect network-test claims. | Explicit non-IP scope; byte-link only; no packet-capture promise. |
| Remote service ignores tenancy. | Host compromise/data leakage. | Out of scope for v1; isolation spike requires containers, quotas, and attack tests. |
| Framebuffer streaming starves emulation. | Poor performance, dropped frames. | Bounded queues/backpressure; measure CPU/latency; NO-GO if unbounded. |
| Pair mode corrupts input or commands. | Stuck keys, lost work, human/agent conflict. | Broker + explicit input lease; disconnect/release-all tests; NO-GO without arbitration. |
| Host input capture becomes keylogging. | Privacy/security incident. | Opt-in foreground capture, visible indicator, no default persistence; out-of-scope background hooks. |
| LLM tooling invents evidence or unsafe steps. | Wrong tests, unsafe scenarios, cost blowups. | Validate every scenario, cite only artifacts, sandbox, token/time budgets, NO-GO on hallucination. |
| Benchmarks are noisy or overfit. | False regressions/improvements. | Dedicated host, repeated samples, CoV, injected regression, non-parametric test. |
| Real hardware work is unbounded. | Cost and legal risk. | Out of scope; no spike; revisit only with hardware/sponsor/new plan. |
| Analog work lacks a test program. | Unverifiable patch. | Require a guest program before GO; otherwise NO-GO. |
| Lab code leaks into production. | Scope creep and maintenance. | `HATARI_MCP_LABS` guard, no tool registration, packaging exclusion, delete/promote. |
| Third-party data/ROMs are committed. | License violation. | No corpus/ROM in git; hash checks; review before committing data. |

## Work log

### Done

- 2026-10-27 — P8.0 lab guard: `mcp/project/labs/` is a guarded, non-shipping
  package. `import labs` is inert; every spike submodule requires
  `HATARI_MCP_LABS=1`. The directory is outside `src/`, registers no MCP tools,
  and the lab test now builds a wheel and asserts that `labs/` is absent and
  that importing the lab package does not change the server tool set. Files:
  `labs/__init__.py`, `labs/harness.py`, `labs/stats.py`,
  `labs/bench_spike.py`, `labs/time_travel_spike.py`, `labs/dwarf_spike.py`,
  `labs/coverage_gap_spike.py`, `labs/__main__.py`, `labs/README.md`.
  Hermetic tests are in `tests/labs/`.
- 2026-10-27 — P8.1 time travel (rerun with observable load timing): 10
  snapshots at 50-VBL cadence on ST/EmuTOS. Size median 126,880 bytes
  (123.9 KiB); save median 46.2 ms (max 89.5 ms); `stateload` command median
  15.8 ms; **observable restore median 41.2 ms**; rewind-to-checkpoint max
  0.882 s. Static screenshot/RAM checkpoints matched 3/3 and replay was
  deterministic 3/3. Cadence 25/50/100 VBL costs 14.52/7.26/3.63 MiB per 60 s.
  Overall **go=false / INCONCLUSIVE** because the workload has no guest input
  and P7.5 input replay is untested. Command:
  `HATARI_MCP_LABS=1 ../.venv/bin/python -m labs time-travel --out /tmp/hatari-mcp-p8-audit/time-travel`.
  Report: `doc/investigations/P8-time-travel.md`.
- 2026-10-27 — P8.2 DWARF (rerun): `m68k-atari-mint-gcc` is absent;
  `m68k-linux-gnu-gcc`, `readelf`, `addr2line` and `objdump` are present. A
  minimal DWARF 2–4 line parser matched `readelf` and `addr2line` on the full
  50/50 sampled rows from a linked m68k Linux ELF, with no unresolved rows.
  The denominator is the full sample; a fixture with fewer than the requested
  50 rows is NO-GO. Locals were found in DWARF but **not read from a guest**.
  GO is narrowed to **parser agreement on m68k Linux ELF**; Atari PRG
  relocation, AHCC DWARF and local-read claims are NO-GO. Command:
  `HATARI_MCP_LABS=1 ../.venv/bin/python -m labs dwarf --out /tmp/hatari-mcp-p8-audit/dwarf`.
  Report: `doc/investigations/P8-dwarf.md`.
- 2026-10-27 — P8.3 coverage (raw stock profiler, retained JSON audited, trace
  not rerun): profiled AHCC HELLO_ST.TOS on STE/TOS 1.62. Three profile runs
  had 10 active functions, 997 unique PCs, 10,825 instructions and identical
  hit counts. The 10-function sample now compares **hit counts on active
  functions** (fputc 159/159, fclose 75/75, _printf 72/72, Exit_iob 69/69,
  `<_fflush>` 42/42, write 33/33, `<c_f>` 30/30, printf 27/27, fflush 24/24,
  main 12/12), so zero/zero presence no longer inflates agreement. 17 functions
  had zero samples. Trace was 321,825,989 bytes = **306.9 MiB** (321.8 MB
  decimal), 5,132,070 lines per 600 VBL, at 12.75× overhead. The retained
  end-to-end wall ratio is 1.0002 but is **boot-dominated and therefore
  INCONCLUSIVE as profiler overhead**; the harness now records entry→exit host
  CPU time for a future rerun. **P6.11 backend integration is untested/NO-GO.**
  Manual disassembly review was not performed, so function-level GO is
  withheld (`go_function_level=false`, `go_function_level_data_only=true`).
  Audited with:
  `HATARI_MCP_LABS=1 ../.venv/bin/python -m labs coverage --from-json /tmp/hatari-mcp-p8-final/coverage/coverage/coverage.json --out /tmp/hatari-mcp-p8-audit/coverage`.
  Report: `doc/investigations/P8-coverage.md`.
- 2026-10-27 — P8.10 benchmark (harness revised, retained data reused):
  the harness now asserts zero return codes, records host CPU time and the
  boot/scenario split, and randomizes/interleaves baseline and injected runs.
  The retained run (3 profiles × 10 reps) showed ST 0.4292 s (CoV 4.03%, one
  outlier), STE 0.4304 s (0.62%), Falcon 4.9378 s (0.89%), and a detected
  +13.4% injected shift at p = 0.0104. Its injection baseline is byte-identical
  to the ST profile workload, so it is a **reused baseline, not independent
  A/B evidence**; the p-value is a detector demonstration only. It predates
  interleaving and did not store CPU/return-code fields, and repeated runs
  reached **5.4% CoV**.
  It is therefore **harness feasibility / INCONCLUSIVE, not a benchmark GO**;
  `go` now also requires an explicit `--dedicated-runner` declaration. Report:
  `doc/investigations/P8-benchmark.md`.
- 2026-10-27 — P8.4–P8.12 disposition: documented demand, blockers and
  classification for SCC, remote, streaming, pair, host input, LLM tooling,
  real hardware and analog input without starting production work. Report:
  `doc/investigations/P8-other-items.md`. P8.5 and P8.11 remain out of scope;
  no CI or hardware resources were reserved.
- 2026-10-27 — Review fixes (C1, H1–H7 and medium items):
  coverage now requires non-empty data, positive PCs/instructions, at least
  five mapped active functions, stable hit counts, and an explicit manual
  review before a function-level GO; an empty-profile negative test asserts
  NO-GO. Coverage is relabeled as a raw stock-profiler spike with P6.11
  integration untested. Time-travel measures observable restore completion,
  labels the static checkpoint check as insufficient, and keeps `go=false`
  without input replay. DWARF divides by `len(sample)`, requires every sample
  to resolve, enforces the 50-row minimum, and narrows the GO label. Benchmark
  assertions/interleaving/CPU/boot-split were added and GO is now
  dedicated-runner-gated. Lab tests skip when the gitignored AHCC corpus is
  absent, and the wheel/server-tool packaging guard is exercised. Path/doc
  names, venv paths, trace units/base handling, full hashes and stale counts
  were corrected.
- 2026-10-27 — Verification (historical; superseded by the final result
  below): `mcp/.venv/bin/python -m pytest mcp/project/tests/labs -q` →
  **27 passed**, 1 warning (unknown `asyncio_mode` option). Full suite then:
  **388 passed, 1 skipped**, 1 warning. The skip is the pre-existing corpus
  test `tests/corpus/test_corpus_scenarios.py:24` (FreeMiNT has no prebuilt
  component smoke binaries). No commits were made; no MCP tools were
  registered; no writes occurred outside `mcp/project` and `/tmp`.

- 2026-10-27 — Second review fixes (remaining P8 findings):
  P8.3 `go_function_level` now requires a validated P6.11 evidence record
  (backend/scenario/artifact/run ids/positive samples), a measured entry→exit
  host-CPU overhead within the 2x spike budget, and a scenario-step mapping for
  every zero-sample function. Fabricated positive counts plus
  `--manual-review-done` remain INCONCLUSIVE (negative test added). P8.1
  replaces the bare `--input-replay-proven` flag with a validated evidence
  record (existing bundle path, ≥3 hashed checkpoints, hashes, positive event
  count, matched replay status); the bare flag remains INCONCLUSIVE (tests
  added). P8.2 treats `addr2line` `??`/line 0 as unresolved and requires 100%
  agreement on the full sample; hermetic tests no longer skip for a missing
  toolchain. Coverage output is normalized to `<out>/coverage.json`, and the
  stale trace range is corrected to 6.60–6.90 s. The benchmark report relabels
  the retained baseline as **reused** (not independent A/B) and its maxima as
  maxima (not p95); the gate now requires exactly 3 profiles x ≥10 reps and
  zero interactive return codes. Verdicts remain honest: P8.1 and P8.3
  INCONCLUSIVE, P8.2 GO only for parser agreement on m68k Linux ELF, P8.10
  harness feasibility only.
- 2026-10-27 — Historical verification after the second review fixes:
  `mcp/.venv/bin/python -m pytest mcp/project/tests/labs -q` → **37 passed**,
  1 warning (unknown `asyncio_mode` option). The 1 GiB coverage trace was not
  rerun; the retained JSON was audited at
  `/tmp/hatari-mcp-p8-audit/coverage/coverage.json` and still reports
  `go_function_level=false` with P6.11 evidence, measured overhead and
  scenario-step gap mapping all absent. No commits were made; no writes
  occurred outside `mcp/project` and `/tmp`.

### Remaining / next steps

- [ ] Final review sign-off of the latest fixes is pending; independent
      adversarial review has been run and its findings are merged above.
- [ ] P8.3: integrate the actual P6.11 backend/scenario path and rerun with
      matched profile/trace windows. Perform the manual 10-function
      disassembly review and record it via `--manual-review-done`; only then
      can the function-level GO be reconsidered. Record entry→exit CPU
      overhead. Do not relabel sampling as line/branch coverage.
- [ ] P8.3: replace the observed `0x00f176` load base and `+454` exit offset
      with a resolver derived from the binary/map; keep them as spike
      parameters until then.
- [ ] P8.1: implement/test P7.5 frame-locked input replay with an input-driven
      animation workload and record a valid evidence JSON (bundle path,
      checkpoints, hashes, event count, replay status). Three static screenshot
      matches and a bare flag are not enough. Measure rewind latency after
      input replay, not just snapshot load.
- [ ] P8.2: install/pin an `m68k-atari-mint` toolchain that emits DWARF, build
      a TOS PRG with `-g`, verify the PRG load/relocation model, read a local
      from a stopped guest, and test DWARF 5 and optimized locals before any
      Atari promotion.
- [ ] P8.10: rerun exactly 3 profiles x 10 reps on a dedicated runner with CPU
      affinity/cgroup quotas and interleaved A/B samples; record workload and
      interactive return codes, CPU time and boot/scenario split. The current
      shared-host result is harness feasibility, not a regression gate.
- [ ] P8.4, P8.6, P8.7, P8.8, P8.9 and P8.12 remain unstarted until a concrete
      user need exists. P8.5 and P8.11 remain out of scope.
- [ ] After review, delete or promote the lab harnesses. Do not copy them into
      `src/hatari_mcp` without an ADR and a promoted plan.
- [ ] Keep raw measurements in `/tmp/hatari-mcp-p8-*`; do not commit ROMs,
      third-party source, traces, profiles or snapshots. Define a retention
      window and delete `/tmp/hatari-mcp-p8-*` when the investigation closes.

### Next

1. Final review sign-off of the executed spikes, the lab guard and these fixes.
2. Demand triage for the unstarted items.
3. P6.11 integration and manual coverage review before any coverage promotion.
4. P7.5 input replay before any exact time-travel claim.
5. Dedicated-runner interleaved benchmark before any regression gate.
6. Atari-mint toolchain and PRG relocation/local-read experiment before DWARF
   promotion.
7. Leave P8.5 and P8.11 untouched unless the product decision changes.

### Final reconciliation (2026-10-27)

- Full suite after the latest fixes: **452 passed, 1 skipped**; the skip is the
  FreeMiNT prebuilt-corpus test and the single warning is `asyncio_mode`
  because `pytest-asyncio` is absent. The earlier **388 passed** run is
  historical.
- Server registers **102 tools**; labs remain non-shipping and register no MCP
  tools.
- Verdicts remain: P8.1 time travel INCONCLUSIVE (no P7.5 input replay), P8.2
  GO only for a narrow Linux-ELF DWARF parser (no Atari-mint DWARF toolchain or
  PRG/local-read proof), P8.3 coverage INCONCLUSIVE (no P6.11 integration or
  manual review), P8.10 benchmark harness-feasibility only (no dedicated
  runner/cgroup quota).
- Independent adversarial review was run; the latest fixes are under final
  review.

### Self-review (independent review subsequently completed)

| # | Finding | Impact | Disposition |
| --- | --- | --- | --- |
| 1 | Coverage could return GO with empty data and zero/zero sample rows. | False confidence. | Fixed: positive PCs/instructions, ≥5 mapped active functions, stable hit counts, empty-profile negative test. |
| 2 | Coverage sample was tautological and the trace workload differed. | Overstated agreement. | Fixed: hit-count comparison on active functions; workload mismatch documented. |
| 3 | Coverage spike bypassed P6.11. | Misleading product claim. | Relabeled raw stock-profiler spike; P6.11 integration explicitly untested/NO-GO. |
| 4 | Profiler overhead was boot-dominated. | Wrong cost claim. | Labeled INCONCLUSIVE; harness now records entry→exit CPU time. |
| 5 | Time-travel checks were static and ignored missing P7.5 replay. | False time-travel GO. | `go=false`/INCONCLUSIVE until input replay is proven. |
| 6 | `stateload` timing measured only a command round-trip. | Understated restore cost. | Added observable restore completion; median 41.2 ms vs 15.8 ms command. |
| 7 | DWARF ratio could be 100% with 1/50 resolved. | False mapping accuracy. | Divide by `len(sample)`, require all 50, fail unresolved. |
| 8 | Benchmark GO contradicted 5.4% CoV and uncontrolled ordering. | False regression gate. | Assert return codes, interleave A/B, record CPU/boot split, require dedicated runner; result now INCONCLUSIVE. |
| 9 | Lab tests depended on gitignored AHCC corpus. | Clean-checkout failure. | Skip guards added; packaging test builds a wheel and checks server tools. |
| 10 | Plan/README paths, doc names, units, hashes and counts drifted. | Hard to reproduce. | Corrected paths/doc names, venv path, 306.9 MiB, full hashes, trace base handling and test counts. |

### Adversarial review

**Provenance:** an earlier self-review could not obtain an independent review.
Independent adversarial review was subsequently run; this table merges that
review's findings with the earlier structured self-review and is retained as
history. The latest fixes are under final review.

| # | Finding | Severity | Disposition in this plan |
| --- | --- | --- | --- |
| 1 | Coverage GO could pass with zero data; empty sets were "stable" and 0/0 counted as agreement. | Critical | Fixed: data positivity, positive PCs/instructions, ≥5 mapped active functions, hit-count agreement, empty negative test. |
| 2 | P8.3 bypassed P6.11 and implied product coverage. | High | Relabeled raw stock-profiler spike; P6.11 integration untested/NO-GO. |
| 3 | Coverage sample was tautological and lacked manual review. | High | Hit counts on active functions; manual review recorded as not done and GO withheld. |
| 4 | Profiler overhead was boot-dominated. | High | Overhead INCONCLUSIVE; entry→exit CPU timing added to the harness. |
| 5 | Time-travel checks were insensitive and ignored missing P7.5. | High | Static-only label; overall `go=false` until input replay; observable restore timing. |
| 6 | DWARF denominator could hide unresolved samples. | High | Full-sample denominator, all-samples-resolved requirement, 50-row minimum, narrow GO label. |
| 7 | Benchmark GO contradicted 5.4% CoV and had uncontrolled ordering. | High | Assert return codes, interleaved randomized A/B, CPU/boot split, dedicated-runner gate; INCONCLUSIVE. |
| 8 | Lab tests failed on clean checkout without gitignored AHCC. | High | Explicit skip guards; packaging/server-tool guard retained. |
| 9 | Time travel is assumed to mean reverse execution; host side effects cannot be undone. | High | P8.1 scopes to snapshot boundaries, lists irreversible effects, and does not claim reverse execution. |
| 10 | DWARF source debugging is assumed available in the Atari corpus. | High | P8.2 returns NO-GO for AHCC/PRG and only claims parser agreement on m68k Linux ELF. |
| 11 | Coverage is treated as line/branch coverage. | High | P8.3 is function-level sampling only; line/branch and trace-as-coverage are NO-GO. |
| 12 | SCC LAN is mistaken for Ethernet/IP. | High | P8.4 explicitly limits to byte-link over a serial SCC path and excludes IP/packet capture. |
| 13 | Remote multi-user service is a product non-goal. | High | P8.5 is out of scope; only a container-isolation spike is allowed if the decision changes. |
| 14 | Framebuffer streaming can starve emulation. | High | P8.6 requires bounded queues/backpressure; NO-GO otherwise. |
| 15 | Pair mode is impossible as a second control connection. | High | P8.7 uses a broker/input lease and depends on P7.1; NO-GO if it needs an unsafe patch. |
| 16 | Host input capture can become keylogging. | High | Opt-in foreground capture, visible indicator, no default persistence. |
| 17 | LLM tooling can hallucinate artifacts or unsafe scenarios. | High | P8.9 validates every scenario, cites only artifacts, sandboxes, and budgets; NO-GO on hallucination. |
| 18 | Real-hardware bridge has no hardware or legal basis. | High | P8.11 is out of scope; no spike. |
| 19 | Analog injection has no guest test program. | High | P8.12 requires a test program and P7.4 extension; NO-GO otherwise. |
| 20 | Lab code could ship. | Medium | `HATARI_MCP_LABS` guard, no tool registration, wheel test, delete/promote after each spike. |
| 21 | Plan/README drift (paths, doc names, venv, units, stale numbers). | Medium | Corrected; see Work log. |
| 22 | Raw data retention is unbounded. | Medium | Raw data stays under `/tmp`; cleanup is a Remaining item and no large data is committed. |
| 23 | The requested review is not independent. | Process | Recorded here; blocking action in Remaining. |

Changes already reflected: data-valid coverage GO, raw-profiler relabeling,
hit-count sampling, manual-review gating, entry→exit overhead measurement,
input-replay-gated time travel, observable restore timing, full-sample DWARF
denominators and narrow GO wording, interleaved/dedicated-runner benchmarking,
clean-checkout lab skips, wheel/server-tool packaging checks, and corrected
paths/units/hashes/counts.
