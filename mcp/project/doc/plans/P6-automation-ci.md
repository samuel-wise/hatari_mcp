# P6 — Automation and CI

Status: partial implementation (P6.1–P6.6, P6.8, partial P6.9/P6.14; P6.7/10/11/12/13 not implemented) — latest fix pass under final review
Owner: P6 implementer (implementation subagent)
Reviewer: independent adversarial reviews completed; latest P6/P7/P8 fixes under final review
Last updated: 2026-10-27
Roadmap: P6.1–P6.14

Depends on: P0.2–P0.4, P1.7–P1.10, P2.1–P2.13, P3.2–P3.5,
P3.12–P3.13, P4.1–P4.7, P5.1–P5.12
Feeds: P8.3 (coverage/test-gap), P8.5 (remote service), P8.9 (LLM test tooling)

## Goal

Turn the server from an interactive debugging bridge into a dependable test
runner. A CI job must be able to run a versioned scenario across a machine
matrix, in parallel, with bounded resources, and publish reproducible artifacts
and a machine-readable pass/fail report — without leaking emulator processes,
sockets, temporary directories, or disk. The scenario format must be a stable
contract that a model can author, a human can review, and CI can trust.

P6 is deliberately buildable **without** the Phase 7 patch. Its "strict"
determinism mode is exact only where stock Hatari allows it; frame-locked replay
is a documented P7 improvement. Every P6 feature must degrade explicitly rather
than silently claim determinism it cannot provide.

## Dependencies

| Dependency | Need | Hard? | Notes / fallback |
| --- | --- | --- | --- |
| P0.2 supervisor | Launch, stop, socket/stdin ownership | Hard | P6 cannot invent a second lifecycle path. |
| P0.4 demuxer | Console/log assertions and failure evidence | Hard | Assertions read bounded buffers, never unbounded logs. |
| P1.7–P1.10 | NatFeats exit codes, Pterm fallback, exceptions | Hard | `exit` and `crashed` assertions depend on these. |
| P2.1–P2.5, P2.7 | Frame-targeted capture, pixels, diffs | Hard | Exact/tolerant screen assertions. |
| P2.8, P2.12 | AVI/audio capture, input event log | Soft | Required for P6.3 media artifacts and P6.9 replay. |
| P2.13 | Determinism basics | Hard | P6 extends, does not replace, P2.13. |
| P3.2–P3.5 | `run_prg`, exit ladder, crash bundle, symbols | Hard | Scenario `start` and crash assertions. |
| P3.7 | Fast restart strategy | Soft | Affects per-case budget, not semantics. |
| P3.12–P3.13 | Corpus discovery/build matrix | Hard for P6.14 | Corpus scenarios skip with a reason until present. |
| P4.1–P4.7 | Watchpoints, conditions, profile, trace | Soft | Fuzzing crash triage, coverage spike. |
| P5.1–P5.12 | Media and audio/video configuration | Soft | Required only by scenarios that use them. |
| P7.4/P7.5 | Frame-locked input and frame stepping | Soft | Without them, strict replay is approximate; label it. |
| Python YAML parser | YAML scenario syntax | Soft | JSON is the mandatory core; add YAML only via an optional project extra. Use the validation approach chosen in P0; do not introduce a second schema stack. |
| `resource`/cgroup v2 | CPU/RAM/file caps | Soft | Best-effort; see P6.4 limitations. No hard `psutil` dependency. |

## Deliverables and file ownership

All new server code lives under `mcp/project/src/hatari_mcp/`. P6 owns the files
below; it may modify existing supervisor/channel modules only through their
published interfaces, and must not change P7 protocol modules.

| Path | Purpose |
| --- | --- |
| `src/hatari_mcp/scenario/__init__.py` | Public scenario API and version constant. |
| `src/hatari_mcp/scenario/schema.py` | Versioned dataclasses/validators for scenario, steps, assertions, matrix, timeouts. |
| `src/hatari_mcp/scenario/loader.py` | JSON/YAML load, safe parsing, variable expansion, path confinement, resolved-scenario snapshot. |
| `src/hatari_mcp/scenario/runner.py` | Sequential step engine, deadlines, cancellation, setup/teardown, failure locations. |
| `src/hatari_mcp/scenario/steps.py` | Step handlers (`start`, `wait`, `input`, `assert`, `capture`, `state`, `media`, `teardown`). |
| `src/hatari_mcp/scenario/assertions.py` | Screen/memory/console/exit/log/artifact/state assertions and structured failures. |
| `src/hatari_mcp/scenario/artifacts.py` | Bundle layout, manifest, checksums, atomic finalize, redaction, retention. |
| `src/hatari_mcp/scenario/report.py` | JSON report, JUnit XML, CI annotations, stable exit-code mapping. |
| `src/hatari_mcp/scenario/matrix.py` | Cartesian matrix expansion, case naming, per-case overrides. |
| `src/hatari_mcp/scenario/scheduler.py` | Bounded worker pool, slot leases, cancellation, per-case isolation. |
| `src/hatari_mcp/scenario/resources.py` | rlimit/cgroup detection, caps, disk-budget checks, watchdog. |
| `src/hatari_mcp/determinism.py` | Strict/tolerant profiles, option allowlist, repeatability measurement. |
| `src/hatari_mcp/replay.py` | Record bundle schema, replay, checkpoint verification, divergence report. |
| `src/hatari_mcp/fuzz.py` | Seeded input/memory fuzzing, region allowlist, crash signatures, dedup, minimization hooks. |
| `src/hatari_mcp/coverage.py` | Coverage interface, profile/trace backends, explicit `unsupported` result. |
| `src/hatari_mcp/attach.py` | FIFO attach and session-relay attach, capability refusals. |
| `src/hatari_mcp/sessions.py` | Session registry, ownership tokens, orphan detection, safe cleanup. |
| `src/hatari_mcp/corpus.py` | External-root discovery, prerequisite checks, staging, skip reasons. |
| `src/hatari_mcp/tools/automation.py` | MCP tools: `scenario_run`, `scenario_validate`, `scenario_matrix`, `session_*`, `emu_attach`, `fuzz_*`, `coverage_report`. |
| `src/hatari_mcp/cli.py` | CLI subcommands mirroring the MCP tools for CI (`run-scenarios`, `validate-scenarios`, `sessions`, `report`). |

Adjacent deliverables (outside `src/`, owned by P6 but not runtime code):

| Path | Purpose |
| --- | --- |
| `tests/unit/scenario/` | Schema, runner, artifacts, reports, scheduler, replay, fuzz, sessions. |
| `tests/integration/scenario/` | Real-Hatari runner, parallel, matrix, attach, record/replay, fuzz smoke. |
| `tests/corpus/scenarios/` | Versioned Okami-Shell, FreeMiNT, GodLib scenarios. |
| `tests/corpus/baselines/` | Optional generated baselines with provenance metadata. |
| `tests/e2e/test_ci_flow.py` | End-to-end CI invocation and artifact publication. |
| `Dockerfile.ci`, `ci/` workflows | Reproducible image and CI jobs. |
| `scripts/run_scenarios.py`, `scripts/update_baselines.py` | Thin CI wrappers; no business logic. |
| `doc/tools-reference.md`, `doc/architecture.md` | Tool and lifecycle updates required before release. |

## Design / constraints

### 1. Scenario format (P6.1)

Core format is JSON with a versioned schema; YAML is an optional convenience
front-end that lowers to the same resolved object. Both are parsed with safe
loaders (no arbitrary object construction). Unknown keys are errors, not
silently ignored. Step ids are unique and required; the runner reports failures
by `scenario_id`, case id, `step_index`, and `step_id`.

```yaml
version: 1
id: ste-okami-boot
description: Boot Okami-Shell and read the desktop
requires:
  env: [OKAMI_ROOT]
  files: ["${OKAMI_ROOT}"]          # checked after variable expansion
  rom: emutos                        # emutos | tos | either
  tools: []                          # ahcc, m68k-atari-mint, ...
  skip_reason: "Okami-Shell tree not configured"
determinism: strict                  # strict | tolerant | off
timeouts:
  scenario_ms: 120000
  step_ms: 5000
config:
  profile: ste
  tos: "${TOS}"
  args: []                           # allowlisted keys only, see §11
steps:
  - id: wait-desktop
    wait:
      screen_change: { settle_ms: 500, stable_ms: 250 }
  - id: type-command
    input:
      text: "dir\r"
      inter_key_ms: 20
  - id: assert-output
    assert:
      console: { regex: "Volume|Directory", timeout_ms: 5000 }
  - id: capture-desktop
    capture:
      name: desktop
      format: png
      hash: pixels                     # decoded RGBA, never PNG bytes
  - id: assert-desktop
    assert:
      screen:
        baseline: ste-okami-desktop.png
        mode: pixel                    # exact | pixel | region
        max_channel_delta: 2
        max_diff_pixels: 0
teardown:
  - stop: { force: false, timeout_ms: 15000 }
```

Step vocabulary (v1):

| Step | Fields | Semantics |
| --- | --- | --- |
| `start` | `profile?`, `tos?`, `args?`, `workspace?`, `display?` | Uses P3 `run_prg`/lifecycle; no raw shell. |
| `wait` | `frames?`, `vbl?`, `screen_change?`, `console?`, `breakpoint?`, `timeout_ms?` | Event-driven; polls only where the underlying event has no push path. |
| `input` | `text?`, `keys?`, `mouse?`, `joystick?`, `inter_key_ms?` | Server expands text; frame-locked only when P7.4 is active. |
| `capture` | `name`, `format`, `region?`, `hash?` | Writes under the case artifact directory only. |
| `assert` | one of `screen`, `memory`, `console`, `exit`, `log`, `artifact`, `registers`, `state` | Returns `{ok, expected, actual, detail}`; never only a boolean. |
| `state` | `save?`, `load?`, `path?` | Paths confined to the case workspace. |
| `media` | `action`, `target`, `path?` | Delegates to P5; validated paths. |
| `teardown` | `stop`, `save_artifacts`, `timeout_ms` | Always executed, including after timeout/cancel. |

Variables are limited to matrix axes and declared environment names
(`${NAME}`); expansion is single-pass and undefined variables are errors.
There is no shell expansion and no arbitrary host command execution. Scenario
arguments are checked against an allowlist (§11); a scenario is not a
general-purpose Hatari command line.

Assertions:

* screen: decode the framebuffer, compare in RGBA. `exact` compares a digest;
  `pixel` applies `max_channel_delta` and `max_diff_pixels`; `region` limits the
  comparison to a rectangle. Return a diff image and the bounding box.
* memory: address/length/expected bytes or expression, read through P1 tools.
* console/log: substring, regex, or count, with a `since` cursor so assertions
  are order-sensitive.
* exit: NatFeats/Pterm code from P1/P3; `crashed` is separate from a non-zero
  exit.
* registers/state: symbol/address expressions from P1/P4.
* artifact: existence, size, and digest.

### 2. Runner, timeouts, failure locations (P6.2)

The runner is sequential inside a case; parallelism is across cases (§4).
Every step has an effective deadline = `min(step.timeout_ms,
scenario.remaining_ms)`. Defaults: `start` 30 s, `wait` 30 s, `input` 2 s,
`capture` 10 s, `state` 10 s, `assert` 5 s (screen assertions 10 s),
`teardown` 15 s. A step may lower, not raise, the scenario deadline.

Semantics:

* On timeout, stop the step, attempt a best-effort screenshot/register/memory
  capture, run teardown with its own deadline, and return
  `step_timeout` with the last successful step and last observed event.
* Cancellation (SIGINT/SIGTERM) enters the same teardown path; a second signal
  force-kills the process group. No slot or socket may leak.
* Teardown failure is reported as a secondary error and never masks the primary
  failure.
* Retries are opt-in per scenario (`attempts`, `retry_on`), default 1. Timeouts
  are not retried unless explicitly requested; otherwise flakiness is hidden.
* A missing prerequisite becomes `skipped` with the declared reason, not a
  failure. `--fail-on-skip` makes skips fail for strict CI.
* No wall-clock polling loops. Waits consume demuxer events or frame counts;
  a bounded poll is allowed only for file-based attach/artifact cases.

Failure result shape:

```json
{
  "ok": false,
  "error": "step_timeout",
  "scenario": "ste-okami-boot",
  "case": "ste/emutos/okami",
  "step_index": 4,
  "step_id": "assert-output",
  "phase": "assert",
  "elapsed_ms": 5012,
  "last_success": "type-command",
  "artifacts": ["logs/stderr.log", "screenshots/step-04.png"]
}
```

### 3. Artifact bundles (P6.3)

One directory per case attempt, created atomically and marked complete only
after the manifest and checksums are written:

```text
<artifacts_root>/<run_id>/<case_id>/attempt-<n>/
  manifest.json
  junit.xml
  scenario.resolved.json
  environment.json
  checksums.sha256
  logs/{stdout,stderr,server}.log
  screenshots/step-04.png
  diffs/step-05-diff.png
  media/
  state/
  replay/
  .complete
```

`manifest.json` records schema version, run/case/scenario ids, matrix axes,
status (`passed|failed|skipped|error|cancelled`), timings, exit code, crash
flag, seed, determinism mode, merged config hash, Hatari version (from
`hatari --version`), MCP version/git commit, host metadata (OS, CPU count, load
at start), artifact list with SHA-256 and size, redactions, and the reason for
skips. `environment.json` records env keys by allowlist, never values of
secrets. Command lines are recorded with secret-looking values redacted.

Rules: paths are confined to the artifacts root; case/step ids are sanitized
before use as directory names; bundles are `0700`, files `0600`; per-bundle and
per-run size caps abort with `artifact_quota_exceeded`; retention (`--keep`,
`--keep-failures`, max age) is explicit. A crashed or timed-out case must still
produce a usable bundle; an incomplete bundle is flagged.

### 4. Parallel instances and resource caps (P6.4)

`Scheduler` runs cases in worker slots. Default concurrency is
`min(4, cpu_count)`; `--jobs` overrides with a hard maximum. Each worker gets:

* an isolated workspace and `TMPDIR` under the run root;
* a private socket directory (`0700`) with a random name;
* a private artifacts directory;
* an ownership token recorded in the session registry;
* a process group so cancellation kills the whole tree.

Resource controls are layered:

| Resource | Mechanism | Honest limitation |
| --- | --- | --- |
| CPU time | `RLIMIT_CPU` / cgroup `cpu.max` if writable | `RLIMIT_CPU` kills on exhaustion; it does not throttle. cgroups are often unavailable in unprivileged CI. |
| Memory | `RLIMIT_AS` / cgroup `memory.max` | Too-tight `RLIMIT_AS` breaks SDL and JIT-less but mmap-heavy builds; defaults must be generous and per-profile. |
| Open files | `RLIMIT_NOFILE` | Affects the whole process; set high enough for media + state. |
| File size | `RLIMIT_FSIZE` / per-case disk budget | Conflicts with large state dumps; use per-case budgets and abort after each step. |
| Disk | Workspace quota check + abort | No hard enforcement without filesystem quotas; document that a runaway guest can still fill the run root until the watchdog fires. |
| Wall time | Per-case watchdog | Cancels via teardown then SIGKILL after grace. |

Capability detection chooses cgroups when permitted, otherwise rlimits, and
records the chosen mechanism in `environment.json`. Caps are **policy
controls**, not a security boundary against a hostile guest program; the
strong boundary is a container/VM.

Slots are leased and always released in `finally`; a slot leak is a test
failure. The scheduler must not start a case unless its workspace, socket, and
artifact directory were created successfully.

### 5. Matrix execution and reports (P6.5)

Matrix axes are declared in the scenario or on the CLI (`machine`, `tos`,
`profile`, `determinism`, arbitrary scalar axes). Expansion is deterministic
(sorted axis keys, sorted values, stable case ids derived from ids — not from
host paths or timestamps). A case cap (`--max-cases`) and fail-fast option are
mandatory to bound accidental explosions. Each case may override timeouts,
config, and inputs but not the scenario step sequence.

Aggregate report (JSON):

```json
{
  "schema": "hatari-mcp.report/1",
  "run_id": "...",
  "started_utc": "...",
  "finished_utc": "...",
  "environment": {...},
  "summary": {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "cancelled": 0},
  "cases": [
    {"case": "...", "status": "failed", "duration_ms": 0,
     "failure": {"error": "step_timeout", "step_id": "..."},
     "artifacts": "relative/path"}
  ]
}
```

Stable exit codes:

| Code | Meaning |
| --- | --- |
| 0 | All cases passed; skips allowed unless `--fail-on-skip`. |
| 1 | One or more test failures. |
| 2 | Usage/config/schema error. |
| 3 | Infrastructure error (emulator missing, ROM missing, Docker/CI failure). |
| 4 | Global run timeout/watchdog. |
| 5 | Internal server error. |
| 130 | Interrupted. |

Skips are reported separately from passes and failures. A missing optional
corpus tree is `skipped`; a missing required ROM for a selected case is
`error`/`blocked`, not a silent skip.

### 6. JUnit/JSON output and CI annotations (P6.6)

One `<testcase>` per case, classname = scenario id, name = stable case id.
`skipped` uses `<skipped message="...">`; failures include the step id, typed
error, and artifact paths. System-out/err are truncated to a fixed budget.
Suite properties carry run id, Hatari/MCP versions, determinism mode, and the
resource mechanism.

CI annotations: emit GitHub Actions workflow commands only when
`GITHUB_ACTIONS=true`; otherwise emit a neutral `annotations.json`. Do not
depend on a specific CI vendor. JUnit XML is validated against its schema in
unit tests.

### 7. Docker and CI (P6.7)

Image:

* pinned base digest, non-root runtime user, pinned Python/uv;
* Hatari built from the repository at a recorded commit, with the build flags
  needed for headless screenshots (SDL2, libpng, zlib, readline as available);
* EmuTOS fetched at build time with a SHA-256 check and its license retained;
* **no proprietary TOS ROMs, no AHCC, no corpus source** in the image;
* `/work` for mounted corpus/artifacts, `/tmp` for scratch;
* entrypoint `hatari-mcp`; a `--version`/smoke command for health checks.

CI jobs:

| Job | Trigger | Content |
| --- | --- | --- |
| lint/unit | every push/PR | Ruff, type check if configured, unit tests (no emulator). |
| integration | every push/PR | Build Hatari, fetch EmuTOS with checksum, run a tiny scenario. |
| matrix | nightly/manual | ST/STE/TT/Falcon profiles, strict + tolerant, two jobs. |
| docker | PR touching Dockerfile/CI, nightly | Build image, run smoke scenario, scan licenses. |
| corpus | self-hosted/manual | Okami-Shell/FreeMiNT/GodLib scenarios, skips cleanly when trees absent. |

All jobs have timeouts, concurrency cancellation, artifact upload on failure,
and no cached secrets. Corpus jobs must not upload third-party source or
screenshots that the license does not permit; upload only manifests and
test-owned artifacts.

### 8. Deterministic mode (P6.8)

Two profiles:

* **strict** — frame-based waits, fixed `--rtc-year`, `--timer-d on`,
  `--fast-boot on`, `--fastfdc off`, `--fast-forward off`, `--cpu-exact on`
  where the profile allows it, sound off, fixed monitor/borders/statusbar,
  no host device streams, `TZ=UTC`, `LC_ALL=C`, fixed workspace paths, decoded
  pixel hashes. Frame-locked input is used when P7.4 is present; otherwise the
  `_vbl` breakpoint fallback is used and the report says `fidelity:
  approximate-frame`.
* **tolerant** — wall-clock timeouts, pixel tolerance, optical/font
  differences accepted, and `--fastfdc on` allowed for speed. A tolerance
  failure reports the diff and the measured variance.
* **off** — no repeatability promise; only used for exploratory runs.

Known residual nondeterminism must be listed in every strict report: TOS/EmuTOS
version and patches, RTC *date* (stock Hatari exposes only `--rtc-year`, not a
full frozen clock), uninitialized RAM, host scheduling, SDL/audio timing,
floppy seek timing, and guest programs that read host time or entropy. P6 does
not claim byte-identical PNG files; it compares decoded RGBA and console/log
content.

A `repeatability` command runs a scenario N times (default 20) and reports
which observable changed first (screen hash, console, exit, registers). Strict
mode is only accepted for a scenario after that measurement.

### 9. Record/replay (P6.9)

Record bundle (`replay/` in the artifact tree):

```text
replay/manifest.json
replay/events.jsonl          # frame/vbl, type, payload, applied_at
replay/start.state           # optional snapshot
replay/trace.bin|txt         # optional, bounded
replay/checkpoints.jsonl     # frame, screen_sha256, memory_sha256, console_sha256
```

Recording captures the state/config hash, Hatari/MCP versions, seed, input
events with their delivery frame (or "host-timed" when P7.4 is absent), and
optional trace sets. Replay loads the start state, replays events, and verifies
checkpoints. Output is a first-divergence report with frame, expected/actual
hashes, and artifacts.

Fidelity labels are mandatory: `exact` requires P7.4/P7.5; `approximate`
uses host-timed input and must never be used to claim a deterministic
regression. Trace recording is opt-in and size-capped because Hatari trace
volume is unbounded by default.

### 10. Fuzzing and coverage (P6.10, P6.11)

Fuzzing modes: `input` (keys/mouse/joystick events), `memory` (random writes
inside an explicit guest-RAM allowlist), and `scenario` (mutation of scenario
steps within the schema). Safety rules: no host shell, no writable media unless
the scenario declares it, devices that can touch host files disabled, EmuTOS
only in CI, confined workspace, per-execution timeout, memory/disk caps, and
no network. Fuzzing MMIO or DMA without restrictions can write disk images or
host files; such addresses are outside the allowlist by default.

Crash detection uses exception/illegal-instruction/trap signals plus process
exit, never the console alone. Crash signature: exception vector, PC, SR, stack
window hash, last N trace/tool events. Corpus entries store the seed, scenario
hash, signature, and a minimal reproduction; duplicates are clustered by
signature. Minimization is delta-debugging over the event/step list, bounded by
time and re-verification.

Coverage (P6.11) is **[R]/[P]**, not a gate. The deliverable is an interface
(`CoverageCollector`) with three backends:

1. `profile` — Hatari's profiler histogram; approximate symbol/PC coverage.
2. `trace` — CPU disassembly/trace sets; high overhead and volume.
3. `instrumentation` — requires a Hatari patch; not implemented in P6.

If no backend is usable, return `{supported: false, reason: ...}`. The P6 spike
must measure overhead and compare against a known small program; it must not
claim basic-block coverage. A GO result requires an ADR and a new plan.

### 11. Attach mode and session manager (P6.12, P6.13)

**Attach is constrained by Hatari's socket direction.** Hatari is the *client*:
it connects once to a listening socket at startup (`Control_SetSocket()`), and
the process that created the listener owns that connection. A third party
cannot connect to a running Hatari's control socket. Therefore "attach to
someone else's Hatari" has three honest forms:

| Mode | Works today | Capability | Limitation |
| --- | --- | --- | --- |
| FIFO attach | Yes, if launched with `--cmd-fifo <path>` | Fire-and-forget input/options/shortcuts; no replies | No debugger responses, no state correlation; screenshot must be polled from a file. |
| Session relay | Yes, via an owning-server relay | Full tools of the owning server | It attaches to a *session*, not directly to Hatari; requires an explicit relay endpoint and auth. |
| Brokered/patched attach | No | Full two-way control of an unowned instance | Needs a broker launched before Hatari, or a P7 patch. Out of P6 scope. |

P6 implements FIFO attach and session relay, refuses tools that require
debugger replies in FIFO mode with `attach_limited`, and documents that the
security model in `architecture.md` §6 ("server refuses to attach") is being
relaxed only behind an explicit opt-in allowlist. That architecture change
requires an ADR; attaching is equivalent to granting arbitrary guest/host file
access through the emulator, so it is disabled by default.

The session relay is a local Unix-domain socket in the runtime directory
(`0700`), token-authenticated and opt-in. It is not an MCP transport and never
listens on TCP. It cannot seize a Hatari that is already connected to a
different listener: the relay must be started before Hatari, or the owning
server must expose it. If no relay is enabled, attach is FIFO-only.

Session registry: `$XDG_RUNTIME_DIR/hatari-mcp/<uid>/sessions.json`, `0600`,
protected by a file lock. Each record has an opaque ownership token, PID, process
start time (from `/proc/<pid>/stat` or `pidfd` where available), socket/FIFO
path, workspace, and server id. Cleanup only kills processes whose token and
start time match; PID reuse is rejected. `session_list` lists only registered
sessions, never every Hatari on the host. `session_cleanup` is dry-run by
default. Orphans are reported, not silently killed, unless the record is ours
and stale.

### 12. Corpus scenarios (P6.14)

External roots are read-only inputs, discovered by environment variable or the
documented default, and never copied into the repository:

| Corpus | Default root | Scenario intent |
| --- | --- | --- |
| Okami-Shell | `OKAMI_ROOT` or `~/Projects/Hatari/Okami-Shell` | Boot the GEM shell, capture the desktop, exercise a TTP tool, assert console/no crash. |
| FreeMiNT | `FREEMINT_ROOT` or `~/Projects/Hatari/freemint/<subproject>` | Run prebuilt smoke binaries for selected components (cflib/gemlib/libcmini/mintlib); assert exit/console/screen. |
| GodLib | `GODLIB_ROOT` or `~/Projects/Hatari/godlib` | Run a prebuilt sample/demo or a tiny harness; assert initialization and a stable frame. |

Hard rules:

* Never expose a corpus root as a writable GEMDOS drive: Hatari guest writes go
  to the host directory. Stage only the required files into a per-case
  workspace, or use a read-only image. Corpus trees are never mutated.
* Do not build whole projects in P6; consume prebuilt artifacts or the P3.12
  harness. If a build is required and absent, skip with the exact missing
  prerequisite.
* AHCC and real TOS are not redistributable by assumption; scenarios that need
  them are skipped unless the operator supplies them. Verify licenses before
  uploading any artifact.
* Baselines are generated per Hatari build/profile with provenance metadata;
  cross-version baselines are not trusted. Baselines are optional: a scenario
  can assert "non-blank, stable, no crash, expected console" without one.
* Every scenario declares budget (wall time, artifact size), deterministic
  inputs, expected artifacts, and a `skip_reason`. CI must complete the
  available subset within the job budget.

Scenario files: `tests/corpus/scenarios/okami-shell-boot.yaml`,
`okami-ttp-smoke.yaml`, `freemint-cflib-smoke.yaml`,
`freemint-gemlib-smoke.yaml`, `godlib-sample-smoke.yaml` (each with a README
note on prerequisites and licenses).

### 13. Scenario trust and path safety

A scenario is executable configuration, not trusted code. The loader:

* rejects absolute paths outside configured roots, `..` traversal, NUL/newline
  injection, and symlinks that escape the root;
* validates `args` against a per-profile allowlist and rejects options that
  redirect logging, sockets, FIFOs, host devices, or config files outside the
  case workspace;
* forbids `script`/shell steps entirely; the only executable path is the
  allowlisted P3.6 build command, which is off by default;
* sanitizes case/step ids for filesystem use and caps path length;
* records the resolved scenario and its hash in the bundle so a failure can be
  reproduced exactly.

## Task checklist

### P6.1 — Scenario format

- [ ] Define `scenario/schema.py` v1 with versioning, strict unknown-key errors, step ids, timeouts, requires, matrix, determinism, teardown.
- [ ] Implement JSON loader and optional YAML front-end lowering to one resolved object.
- [ ] Implement single-pass `${VAR}` expansion from declared env/matrix only; reject undefined variables.
- [ ] Implement path confinement and argument allowlist validation (§13).
- [ ] Emit `scenario.resolved.json` with a stable hash.
- [ ] Add fixtures for valid/invalid scenarios in `tests/unit/scenario/fixtures/`.

### P6.2 — Runner and timeouts

- [ ] Implement step engine with per-step and scenario deadlines, cancellation, and teardown-always semantics.
- [ ] Implement typed failures with `step_index`, `step_id`, phase, last success, and partial artifacts.
- [ ] Implement waits as event/frame-driven with bounded fallbacks; no unbounded polls.
- [ ] Implement opt-in retries and `--fail-on-skip`.
- [ ] Test timeout/cancel/teardown paths with a fake emulator backend.

### P6.3 — Artifact bundles

- [ ] Implement atomic bundle creation, manifest schema, checksums, and `.complete` marker.
- [ ] Capture screenshots, diffs, logs, state, media, config, versions, host metadata, seed, determinism mode.
- [ ] Implement redaction, path sanitization, size quotas, and retention.
- [ ] Ensure failure/timeout/cancel still produces a bundle.

### P6.4 — Parallel instances and caps

- [ ] Implement bounded `Scheduler` with slot leases and isolated workspace/TMPDIR/socket/artifact dirs.
- [ ] Implement cgroup-v2 detection with rlimit fallback; record the mechanism.
- [ ] Implement per-case watchdog and process-group kill with grace period.
- [ ] Implement disk-budget checks and `artifact_quota_exceeded`.
- [ ] Implement release-safe cleanup in `finally` and on signals.
- [ ] Test 4-way parallelism and forced parent death for leaks.

### P6.5 — Matrix and aggregate report

- [ ] Implement deterministic matrix expansion, stable case ids, per-case overrides, `--max-cases`, fail-fast.
- [ ] Implement aggregate JSON report with summary counts and per-case artifacts.
- [ ] Implement stable exit-code mapping (§5).
- [ ] Test a 2×2 matrix and a skipped prerequisite.

### P6.6 — JUnit/JSON and annotations

- [ ] Implement JUnit XML with testcase per case, skip/failure details, truncated system output, properties.
- [ ] Validate JUnit output against a schema in tests.
- [ ] Implement neutral annotations JSON and GitHub Actions annotations behind `GITHUB_ACTIONS`.
- [ ] Document CI usage with copy-pasteable commands.

### P6.7 — Docker/CI

- [ ] Add `Dockerfile.ci` with pinned base, Hatari build at a recorded commit, EmuTOS checksum, non-root user.
- [ ] Add CI jobs for lint/unit, integration, nightly matrix, Docker, optional corpus.
- [ ] Add timeouts, artifact upload, concurrency cancellation, and license checks.
- [ ] Prove the image contains no proprietary ROMs/AHCC/corpus source.
- [ ] Document how a self-hosted corpus runner mounts external roots read-only.

### P6.8 — Deterministic mode

- [ ] Implement strict/tolerant/off profiles and an option allowlist/denylist.
- [ ] Implement decoded-RGBA hashing and avoid PNG-byte comparisons.
- [ ] Implement repeatability measurement and residual-nondeterminism reporting.
- [ ] Implement the `_vbl`/frame-stepping fallback and label approximate fidelity.
- [ ] Measure strict repeatability for ST/STE/TT/Falcon sample scenarios.

### P6.9 — Record/replay

- [ ] Define the replay bundle schema with version, state, events, checkpoints, trace, fidelity.
- [ ] Implement recording from the server-side input/event log.
- [ ] Implement replay with checkpoint verification and first-divergence reporting.
- [ ] Enforce `exact` only when P7.4/P7.5 capabilities are present; otherwise `approximate`.
- [ ] Test replay of a small scenario, including a deliberately altered checkpoint.

### P6.10 — Fuzzing

- [ ] Implement seeded `input`, `memory`, and `scenario` fuzz modes with a guest-RAM allowlist.
- [ ] Implement crash detection, signatures, corpus storage, dedup, and bounded minimization.
- [ ] Enforce sandbox rules: no host shell, restricted media, no writable corpus, caps, timeout.
- [ ] Add a 30-second smoke fuzz test to integration (opt-in marker).
- [ ] Document that this is guest-program fuzzing, not a security proof of Hatari.

### P6.11 — Coverage [R]/[P]

- [ ] Define `CoverageCollector` and the `unsupported` result contract.
- [ ] Spike profile-based coverage on a known binary; measure overhead and accuracy.
- [ ] Spike trace-based coverage; measure volume and overhead; cap it.
- [ ] Write `doc/investigations/P6-coverage.md` with a GO/NO-GO and the limits of "coverage".
- [ ] Do not block the P6 gate on coverage; no instrumentation patch in P6.

### P6.12 — Attach mode

- [ ] Implement FIFO attach (`--cmd-fifo`) with explicit write-only capability reporting.
- [ ] Implement session relay attach with explicit endpoint/auth and ownership checks.
- [ ] Refuse reply-dependent debugger tools in FIFO mode with `attach_limited` and a hint.
- [ ] Implement screenshot-by-file polling for FIFO attach with a bounded wait.
- [ ] Add an ADR for the opt-in relaxation of the "no attach" security rule.
- [ ] Document that brokered/patched attach is P7, not P6.

### P6.13 — Session manager

- [ ] Implement the registry with file lock, ownership token, PID start time, and `0600` permissions.
- [ ] Implement `session_list`, `session_wait`, and dry-run `session_cleanup`.
- [ ] Reject PID reuse and never kill unowned processes.
- [ ] Integrate with the supervisor on start/stop/crash.
- [ ] Test stale records, forged tokens, and partial writes.

### P6.14 — Corpus scenarios

- [ ] Implement `corpus.py` discovery with env/default roots, prerequisite checks, and skip reasons.
- [ ] Implement read-only staging into per-case workspaces; never expose corpus roots as GEMDOS drives.
- [ ] Add Okami-Shell boot and TTP smoke scenarios.
- [ ] Add selected FreeMiNT component smoke scenarios using prebuilt binaries.
- [ ] Add a GodLib sample/harness smoke scenario.
- [ ] Add baseline generation with provenance and document when baselines are intentionally absent.
- [ ] Document licenses/toolchains and how CI skips absent trees.

### Cross-cutting

- [ ] Add MCP tools and CLI entry points in `tools/automation.py` and `cli.py`.
- [ ] Update `doc/tools-reference.md` and `doc/architecture.md`.
- [ ] Add `tests/e2e/test_ci_flow.py` and a CI example.
- [ ] Run lint/type checks and the full test suite; leave no orphan processes or sockets.

## Acceptance criteria

1. A scenario with `start`, `wait`, `input`, `assert`, `capture`, and
   `teardown` runs from a single CLI call and returns a structured result.
2. A step that hangs produces `step_timeout` with the exact `step_id`, a
   screenshot if possible, and a complete artifact bundle; the process group is
   gone afterward.
3. A 2×2 machine/TOS matrix runs with bounded parallelism and produces JSON,
   JUnit, and a summary; exit codes match the table in §5.
4. Parallel runs use isolated sockets/workspaces and leave no sockets,
   temporary directories, or child processes after success, failure, timeout,
   SIGINT, or forced parent death.
5. Resource caps are recorded and enforced where the host permits; the report
   states when only rlimits were available.
6. A strict scenario passes 20/20 repeats on the same host, or the first
   divergent observable is reported; no claim of exactness is made for
   host-timed input.
7. Record/replay reproduces checkpoints in exact mode only when P7.4/P7.5 are
   present; approximate replay is labeled and still reports divergence.
8. A skipped corpus case names the missing root/ROM/toolchain, and does not
   turn into a pass or an infrastructure failure.
9. Corpus staging never writes into an external corpus tree; a test proves the
   tree's mtimes/hashes are unchanged.
10. The CI image builds, contains no proprietary ROMs/AHCC, and runs the smoke
    scenario as a non-root user.
11. FIFO attach can send input and capture a screenshot by polling, but refuses
    reply-dependent debugger tools with `attach_limited`.
12. Fuzz smoke runs with a fixed seed, produces a reproducible crash signature,
    and stays inside the sandbox/caps.
13. Coverage reports `unsupported` honestly when no backend is available; it
    never fabricates coverage.
14. `scenario_validate` rejects path traversal, undeclared variables, duplicate
    step ids, and unsafe Hatari arguments.

## Test plan

### Unit tests

| Area | Tests |
| --- | --- |
| Schema/loader | `tests/unit/scenario/test_schema.py`: version, unknown keys, required fields, duplicate ids, timeout bounds, variable expansion, invalid paths, arg denylist, JSON/YAML parity. |
| Runner | `test_runner.py`: fake backend, success, failure location, step timeout, scenario timeout, teardown always, cancellation, retry policy, skip vs error. |
| Assertions | `test_assertions.py`: exact/pixel/region screen, memory, console regex/cursor, exit/crash, log, artifact digest, structured diff. |
| Artifacts | `test_artifacts.py`: layout, manifest schema, checksums, atomic finalize, redaction, quota, symlink/path escapes. |
| Scheduler | `test_scheduler.py`: concurrency cap, queue fairness, slot release, cancel-all, per-case isolation, disk budget. |
| Matrix/report | `test_matrix.py`, `test_report.py`: deterministic expansion, stable ids, JSON schema, JUnit schema, exit-code mapping, annotations. |
| Determinism | `test_determinism.py`: profile option allowlist, decoded-pixel hashing, residual nondeterminism manifest, approximate labeling. |
| Replay | `test_replay.py`: schema round-trip, checkpoint divergence, fidelity gating, corrupted bundle handling. |
| Fuzz | `test_fuzz.py`: seed reproducibility, RAM allowlist, signature stability, dedup, minimization bounds. |
| Sessions/attach | `test_sessions.py`, `test_attach.py`: stale PID, PID reuse, forged token, no-kill refusal, FIFO capability refusal, relay auth. |
| Corpus | `test_corpus.py`: discovery, default/env roots, skip reasons, read-only staging, no tree mutation. |

### Integration tests

| Test | Requirement |
| --- | --- |
| `tests/integration/scenario/test_runner_real.py` | Real Hatari + EmuTOS: boot, input, screenshot assertion, console/exit assertion, timeout bundle. |
| `test_parallel.py` | Four instances in parallel with caps; assert isolation and no leaked sockets/processes; kill the parent and verify cleanup. |
| `test_matrix.py` | ST/STE × EmuTOS/TOS-if-available; report and exit codes. |
| `test_replay.py` | Record a short run, replay, verify checkpoints; mutate one checkpoint to prove divergence detection. |
| `test_attach_fifo.py` | Launch Hatari with `--cmd-fifo`; attach; send input; capture via file polling; assert reply-dependent refusal. |
| `test_session_cleanup.py` | Orphan record cleanup, unowned refusal, PID reuse guard. |
| `test_fuzz_smoke.py` | 30 s seeded fuzz with caps; at least one reproducible signature or clean timeout. |
| `test_coverage_spike.py` | Profile/trace spike; `xfail`/skip when the backend is unavailable. |

Integration tests must be marked so they can run without a proprietary ROM.
EmuTOS is the default fixture. Any test requiring real TOS skips with a reason.

### Corpus tests

`tests/corpus/test_okami.py`, `test_freemint.py`, `test_godlib.py` are opt-in
(`-m corpus`) and discover roots by env/default. They skip when the root,
ROM, toolchain, or prebuilt artifact is absent. Each test:

1. verifies the corpus tree hash/mtime before and after;
2. stages only the declared files into the case workspace;
3. runs the scenario under strict or tolerant determinism as declared;
4. asserts the declared observables, not a cross-machine baseline;
5. records prerequisites and license notes in the artifact manifest.

Baselines are generated only by `scripts/update_baselines.py` on the same
Hatari build/profile, reviewed, and stored with version/provenance metadata.
CI never auto-updates baselines.

### End-to-end/CI tests

`tests/e2e/test_ci_flow.py` runs the CLI as CI would: validate scenarios, run a
small matrix, parse JUnit, assert exit codes, inspect the manifest, and verify
that a forced failure still uploads artifacts. The Docker smoke test runs the
same scenario inside the image as a non-root user.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Attach mode is assumed to work against any running Hatari. | P6.12 is impossible as commonly imagined. | Only FIFO and session-relay attach; broker/P7 for full attach; ADR and explicit refusal. |
| `RLIMIT_*` is treated as a security sandbox. | Hostile guest or emulator bug can exceed intended limits. | Document caps as policy; use a container/VM for real isolation. |
| cgroups are unavailable in CI. | Memory/CPU caps are weaker than advertised. | Detect and record; generous defaults; fail closed only when `--strict-caps` is requested. |
| Determinism is overclaimed. | Flaky green/red builds and false confidence. | Strict/tolerant/off profiles; repeatability measurement; decoded-pixel hashes; P7 gating for exact replay. |
| Corpus roots are exposed as GEMDOS drives. | Guest writes mutate third-party source. | Mandatory read-only staging/copy; tree-hash test before/after. |
| Docker bundles TOS/AHCC/corpus. | License violation. | Build-time license checks; no proprietary ROMs/AHCC; mount external roots only. |
| Baseline screenshots are environment-dependent. | Fragile tests. | Same-build generation, provenance, tolerance, optional baselines. |
| State dumps/AVI exceed disk. | Run abort or host disk exhaustion. | Per-case quotas, `RLIMIT_FSIZE` with generous limits, watchdog, retention. |
| Scenario args are an escape hatch. | Arbitrary Hatari options, host file writes, socket/FIFO hijack. | Schema allowlist/denylist; path confinement; no shell. |
| JUnit/exit codes conflate skip/error/failure. | CI cannot distinguish missing corpus from regression. | Explicit statuses, `--fail-on-skip`, exit-code table. |
| Fuzzing triggers emulated host side effects. | Writes to disk images or host files. | Guest-RAM allowlist, devices off, sandbox, no writable media by default. |
| Session manager kills the wrong process. | Data loss/security incident. | Ownership token + PID start time/pidfd; dry-run cleanup; never kill unowned. |
| Session relay adds a local control surface. | Local privilege/credential abuse. | Runtime dir `0700`, token auth, opt-in, no TCP, ownership checks; documented in the attach ADR. |
| P6.11 coverage is treated as a deliverable. | Over-scope and false coverage claims. | [R]/[P], spike only, `unsupported` contract. |
| Roadmap names `--fastfloppy`; the 2.6.1 build exposes `--fastfdc`. | Wrong option, confusing failures. | Use the actual option, validate profiles against `--help`, document the discrepancy. |
| `--rtc-year` fixes only the year. | Date-dependent guests remain nondeterministic. | Document; use tolerant mode or P7.5 frozen clock; don't promise full RTC control. |

## Work log

### Done

- 2026-10-27 — planning subagent: read the roadmap, architecture, tool
  reference, patch policy, AHCC CLI script, and MCP README; authored this plan.
- 2026-10-27 — planning subagent: checked the local Hatari 2.6.1-devel build for
  actual option names (`--fastfdc`, `--rtc-year`, `--cpu-exact`, `--timer-d`)
  and the control-socket direction (`Control_SetSocket()` connects out).
- 2026-10-27 — planning subagent: recorded the attach-mode and corpus-write
  hazards, and the rlimit/determinism limitations.

### Remaining / next steps

This planning-era list is superseded by the P6 implementation log and the final
reconciliation below. The current blockers are: no rlimit/cgroup enforcement,
no Docker/CI file, no fuzzing/coverage/attach/session-manager implementation,
partial replay fidelity, and absent FreeMiNT prebuilt corpus binaries. The
independent adversarial reviews have been run; only final sign-off of the
latest fix pass remains.

### Next

1. Complete the final review sign-off for the latest fixes.
2. Implement P6.4 resource caps and P6.7 CI once the security/ownership review
   is settled.
3. Treat P6.10/P6.11/P6.12/P6.13 as gated follow-ups; keep P6.9 replay labelled
   approximate until P7.4/P7.5 land.

### P6 implementation log (2026-10-27)

#### Done

- Added `src/hatari_mcp/services/scenario.py`: strict scenario v1 schema, single-pass
  `${VAR}` expansion from declared env/vars/matrix only, path confinement and
  Hatari-argument denylist, deterministic matrix expansion and per-case overrides,
  stdio runner (verified against the installed `mcp` 2.x `stdio_client` +
  `ClientSession` API), per-step/scenario deadlines, teardown-always semantics,
  typed failures with `step_index`/`step_id`/phase/last-success, atomic artifact
  bundles, manifests with SHA-256 checksums/versions, `.complete` markers, size
  quota, aggregate JSON report, JUnit XML, neutral annotations, GitHub Actions
  annotations behind `GITHUB_ACTIONS=true`, decoded-RGBA screenshot hashing,
  strict/tolerant/off determinism reporting, repeatability comparison, and a
  partial record/replay implementation with explicit `approximate` fidelity.
  Screen capture uses the no-reply ``screenshot`` shortcut into a
  runner-managed ``--screenshot-dir`` (with an unquoted debugger command as a
  fallback), which is both faster and robust when the debugger channel is busy.
- Added `src/hatari_mcp/corpus.py`: env/default-root discovery for Okami-Shell,
  FreeMiNT (cflib/gemlib/libcmini/mintlib) and GodLib, prerequisite and skip
  reasons, tree signatures, and read-only staging into per-case workspaces.
- Added `src/hatari_mcp/scenario_cli.py` (runnable as `python -m
  hatari_mcp.scenario_cli`): `validate`, `run`, `list-corpus`, `repeat` and
  `replay`, with `--artifacts`, `--max-cases`, `--fail-fast`, `--jobs`,
  `--server-python`, `--server-cmd`, `--json`, `--junit`, `--record`.
- Added `src/hatari_mcp/tools/automation.py` with bounded
  `scenario_validate`, `scenario_run`, `scenario_list_corpus` and
  `scenario_replay` tools. `scenario_run` is wrapped in `asyncio.wait_for`.
- Added example scenarios under `examples/scenarios/` (`boot-capture-stop`,
  `strict-boot`, `matrix-boot`, Okami/FreeMiNT/GodLib corpus smoke scenarios) and
  `examples/scenarios/README.md` documenting the JSON format and CLI.
- Added unit tests for schema, matrix, path/argument safety, runner success,
  assertion failure, step/scenario timeout, teardown-always, cancellation,
  skip, parallel cases, artifacts/checksums/quota/escape, JSON/JUnit/exit codes,
  annotations, determinism, record/replay, automation registration and corpus
  discovery/staging. Added one real-Hatari integration test and corpus
  scenario tests that skip when their tree is unavailable.
- Real scenario verified: `boot-capture-stop` (EmuTOS, ST profile) passes and
  writes `manifest.json`, `checksums.sha256`, `.complete`,
  `screenshots/desktop.bmp` (decoded-RGBA hash), logs and JUnit. The integration
  test also asserts the emulator PID and private socket directory are gone.

#### Core issues found (not edited; reported instead)

1. `screen_frame_capture`/`screen_screenshot` build `screenshot "<path>"` with
   double quotes. Stock Hatari's debugger ignores the quoted form and the
   sentinel never returns, so those tools time out. The scenario engine avoids
   the bug by driving the `screenshot` shortcut into a runner-managed
   `--screenshot-dir`, and only falls back to an unquoted
   `dbg_command screenshot <path>`. A core fix belongs to the owner of
   `tools/frames.py`/`tools/screen.py`.
2. With this build/TOS combination, `--machine ste` (and TT) made the heartbeat
   permanently report `unresponsive` while ST stayed healthy. The example and
   integration scenario therefore use the ST profile. This needs investigation
   by the supervisor/core owner and is not a scenario-engine workaround.
3. A pre-existing/concurrent change in `tools/debug_advanced.py` (mtime 15:38)
   made `tests/unit/test_debug_advanced.py::test_stack_dump_uses_a7_and_big_endian_words`
   fail (`savebin` length is emitted as `0x8` while the test expects `8`).
   That file is outside P6 ownership and was not edited.

#### Remaining / next steps

- P6.4 CPU/RAM/open-file/disk caps: only per-bundle size quota and explicit
  `resource_mechanism` labeling exist; no rlimit/cgroup enforcement yet.
- P6.7 Docker/CI, P6.10 fuzzing, P6.11 coverage, P6.12 attach and P6.13 session
  manager are not implemented in this slice.
- P6.9 replay is still partial on fidelity: host-timed input is labelled
  `approximate`; input events and all recorded screen checkpoints are replayed
  and compared, but state snapshots and traces are not yet wired. Older bundles
  without checkpoint event positions compare their final screen checkpoint.
- YAML is accepted only when the optional PyYAML package is installed; JSON is
  the supported dependency-free format.
- Corpus runtime coverage is partial: discovery/staging/validation are tested;
  FreeMiNT skips locally because the four subprojects have no prebuilt smoke
  binaries, and a manual Okami boot crashed in this environment. No corpus source
  was copied into `mcp/` and no corpus root is exposed as a GEMDOS drive.
- A shared per-run byte budget now exists and is exercised in unit tests, but
  report/JUnit files at the run root are not counted against it, and
  signal-handler-driven cancellation is not yet exercised end-to-end.
- Independent adversarial reviews were run and their findings were fixed in the
  “Adversarial-fix pass” above; the latest fixes are under final review. The
  earlier self-review remains below as history.
- Wiring for `tools/automation.py` was intentionally not applied because
  `server.py` is outside P6 ownership. The owner must add `automation` to the
  `from .tools import (...)` tuple and call `automation.register(server, APP)`
  next to the other `register` calls.

#### Verification commands and results (2026-10-27)

The incremental results in this subsection are historical (kept for
provenance). The authoritative current result is **452 passed, 1 skipped**
(see Final reconciliation below).

```sh
mcp/.venv/bin/python -m pytest mcp/project/tests/unit mcp/project/tests/corpus mcp/project/tests/integration -q
# Historical: 46 passed, 1 skipped (FreeMiNT has no prebuilt smoke binaries)

mcp/.venv/bin/python -m pytest mcp/project/tests -q
# Historical (superseded): 323 passed, 1 skipped, 1 failed
# failure: test_debug_advanced.py::test_stack_dump_uses_a7_and_big_endian_words
#          (outside P6 ownership; debug_advanced.py changed concurrently)

HATARI_TOS=.../etos192us.img mcp/.venv/bin/python -m hatari_mcp.scenario_cli   run mcp/project/examples/scenarios/boot-capture-stop.json --artifacts /tmp/p6-artifacts
# passed=1, failed=0, skipped=0, error=0, cancelled=0

mcp/.venv/bin/python -m hatari_mcp.scenario_cli validate <each example> --json
# ok for all example scenarios

HATARI_TOS=.../etos192us.img mcp/.venv/bin/python -m hatari_mcp.scenario_cli \
  repeat mcp/project/examples/scenarios/boot-capture-stop.json --repeats 1 --artifacts /tmp/p6-repeat
# runs_passed=true, repeatable=true (a 1-run smoke; strict repeatability still needs a longer campaign)

mcp/.venv/bin/python -m hatari_mcp.scenario_cli list-corpus --json
# Okami/GodLib available locally; FreeMiNT skipped with explicit reason
```

No commit was made. The integration test asserts the Hatari PID and socket
directory are gone after the run; a manual `pgrep` found no P6 Hatari process
(the only remaining Hatari process was a pre-existing probe from another task).

#### Implementation self-review

- **Strengths:** strict unknown-key validation; declared-only variable
  expansion; path confinement and unsafe-argument rejection; deterministic case
  ids; teardown in `finally`/cancellation paths; atomic finalization; checksummed
  bundles on failure and skip; honest determinism/replay labels; bounded
  automation tools; tests use a fake backend for fast coverage and one real
  bounded run.
- **Weaknesses:** the engine is one large module rather than the plan's split
  package; scheduling is bounded but simple (no slot registry); resource caps are
  labels plus bundle quota, not enforced limits; replay is final-checkpoint
  only; no Docker/CI file was added because that path was outside the allowed
  ownership list.
- **Security:** scenario steps are MCP tool calls only; there is no shell step;
  `--control-socket`, `--cmd-fifo`, log/config/hard-drive overrides and `--auto`
  are denied; paths must stay under the scenario root, corpus roots, cwd or
  `/tmp`; corpus roots are rejected as GEMDOS workspaces.
- **Determinism:** no VBL-exact claim is made. Strict mode injects the available
  stock options, keeps `vbl_locked: false`, labels fidelity
  `approximate-frame`, records residual nondeterminism, and hashes decoded RGBA
  rather than PNG bytes.

#### Adversarial-fix pass (2026-10-27)

Independent-review findings on the P6 slice were triaged and the Critical/High
items plus the practical Medium items were fixed in `services/scenario.py`,
`corpus.py`, `tests/unit/test_scenario_runner.py`,
`tests/unit/test_scenario_security_fixes.py` and
`examples/scenarios/README.md`.

Fixed:

- **C1 corpus/repo write protection:** `_reject_protected_path` now imports
  `..corpus` (the old `.corpus` import failed silently), fails closed if the
  policy cannot load, rejects artifact roots/workspaces/captured sources inside
  corpus or repository trees, and `_allowed_roots` no longer grants
  `~/Projects/Hatari`, the repository, `HOME` or the cwd. It grants only the
  scenario directory, temp, and the known read-only env roots (`HATARI_TOS`,
  `HATARI_BIN`, corpus roots, ...).
- **C2 emu_start arguments:** step-level `emu_start.args.args` are validated;
  `config.args` and step-level args are merged, re-validated (denylist + path
  confinement) and only then handed to `emu_start`. Unknown `emu_start` keys
  are schema errors.
- **C3 scenario tool trust:** steps/teardown use a conservative per-scenario
  allowlist (`SCENARIO_ALLOWED_TOOLS`). `dbg_command`, `ahcc_*`, `scenario_*`,
  `emu_set_option`/`emu_set_path`, memory/state writers, trace/profile writers
  and host-media mounts are denied. Path-like args (`path`, `command`,
  `include(s)`, `program`, media paths, ...) are checked recursively, and
  `ScenarioEngine` enforces a nesting-depth limit.
- **H1 artifacts:** `ArtifactBundle.create` rejects symlinked artifact roots,
  case directories, attempt paths and staging dirs, confines case dirs under the
  run root, and rejects corpus/repo roots. `copy_file` accepts only resolved
  sources under the case workspace/temp and outside protected trees.
- **H2 replay:** requires `.complete`, a bundle manifest schema, replay schema
  v1, and valid `checksums.sha256`; rejects disallowed event tools; validates
  start args; requires at least one checkpoint; compares every screen checkpoint
  (using recorded event positions when available); reports `unverified` and
  fails when no screen checkpoint can be verified.
- **H3 matrix:** `matrix.max_cases`/`fail_fast` are retained as metadata;
  cardinality is computed with `math.prod` before expansion; a 256-case default
  cap applies to validate/run; duplicate axis values and duplicate sanitized
  case ids are rejected.
- **H4 artifact quota:** a shared `RunBudget` enforces `max_run_bytes`; the
  server log and finalize files go through the bundle quota; manifest sizes and
  checksums are computed from the real files; finalize failures are no longer
  swallowed and turn a passed/skipped case into `error`.
- **H5 environment/secrets:** the MCP child gets only a fixed env allowlist;
  expanded secret values are redacted from `scenario.resolved.json`, failure
  data and manifests.
- **H6 deadlines/teardown:** the scenario deadline starts before staging;
  staging, connect, close and teardown waits are bounded; forced teardown closes
  the stdio transport (which terminates the child process tree); cancelled
  tasks are always awaited.
- **H7 staging symlinks:** staging and corpus copies reject all symlinks with an
  `lstat`/`os.walk(followlinks=False)` check before copying, so host files are
  not followed out of a corpus tree.
- **Medium:** attempts/retry_on and matrix fail_fast are enforced; determinism
  seed/args/residual are consumed and reported; capture.path/hash and
  PNG+RGBA combinations are validated; missing required ROM is an error (not a
  skip); `copy_file` sources are confined; JUnit counts cancelled cases and
  disambiguates duplicate names; invalid assertion regexes are schema errors;
  results are trimmed recursively; stage workspaces may not be inside a corpus
  tree; the BMP subset (uncompressed 24/32-bit BI_RGB) is documented.

Tests and results (2026-10-27, after the fix pass):

```sh
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python -m pytest \
  mcp/project/tests/unit/test_scenario* \
  mcp/project/tests/corpus \
  mcp/project/tests/integration/test_scenario_real.py -q
# Historical: 74 passed, 1 skipped (FreeMiNT has no prebuilt smoke binaries)

cd mcp/project
../.venv/bin/python -m hatari_mcp.scenario_cli run \
  examples/scenarios/boot-capture-stop.json \
  --artifacts /tmp/p6-fix-artifacts --json
# exact requested invocation: skipped=1 (HATARI_TOS is not exported in this shell);
# exit 0, bundle written with status=skipped.

HATARI_TOS=/home/ubuntu/Projects/Hatari/tos/emutos/etos192us.img \
  ../.venv/bin/python -m hatari_mcp.scenario_cli run \
  examples/scenarios/boot-capture-stop.json \
  --artifacts /tmp/p6-fix-artifacts-real --json
# passed=1, failed=0, skipped=0, error=0, cancelled=0; exit 0
```

The real run initially exposed a capture race (Hatari's shortcut wrote a
partially flushed BMP). `_capture_screen_with_backend` now retries until the BMP
decodes successfully or the bounded capture deadline expires.

Honest remaining gaps after this pass:

- Full run-level artifact quota is enforced through bundle writes; the second
  adversarial-fix pass below also counts `report.json`, `junit.xml` and
  `annotations.json` against `max_run_bytes`.
- Platforms without the installed `mcp` stdio transport's process-tree kill
  semantics rely on the bounded close path; there is no separate process-group
  kill in this module.
- Older replay bundles without recorded event positions can only compare their
  final screen checkpoint; `unverified_checkpoints` names unsupported kinds.
- `capture.hash=sha256|none` is validated but decoded-RGBA capture still always
  records the RGBA hash for BMP; only the `rgba`/PNG combination is rejected.
- Determinism `seed` is recorded and reported but P6 still does not seed the
  guest RNG.
- rlimit/cgroup caps, Docker/CI, fuzzing, coverage, attach and the session
  manager remain outside this slice.

#### Second adversarial-fix pass (2026-10-27)

Fixed the remaining P6 bypasses in `services/scenario.py`, `corpus.py`,
`tools/build.py`, `tests/unit/test_scenario_security_fixes.py` and this work log.

- **Arg allowlist:** replaced `HATARI_ARG_DENYLIST` with an exact long-option
  allowlist. `--parse`, `--debug`, every short alias, positional disk/directory
  arguments, attached `=value` forms, media/host-drive options (`-d`,
  `--harddrive`, `--acsi`, `--scsi`, IDE, `--disk-a/b`, cartridge, memstate),
  host output streams/recorders (`--printer`, RS-232/SCC, AVI, trace/log/config
  files) and screenshot-dir/format overrides are rejected. The same validator is
  applied to `config.args`, step-level `emu_start.args`, `determinism.args` and
  replay start args (recorded runner screenshot options are stripped first).
  `emu_shortcut` denies `savemem`, `recanim` and `recsound`.
- **Workspaces:** scenario `config.workspace`, step workspaces and replay start
  workspaces must be fresh/empty directories below the system temp root; corpus
  roots, repository paths, TOS parents and non-empty host dirs are rejected.
  `_start_args` re-validates non-runner workspaces at runtime. `run_prg` and
  `corpus.stage_files` now reject corpus/repository workspaces; `stage_files`
  also requires a fresh/empty workspace below the temp root and rejects symlink
  workspaces.
- **Redaction:** all declared `requires.env`, `env` and `vars` values (regardless
  of name; `AWS_ACCESS_KEY_ID` included) are collected and redacted from every
  UTF-8 text artifact through `ArtifactBundle` writes/copies, including
  `replay/events.jsonl`, `logs/events.jsonl`, `stage.json` and
  `scenario.resolved.json`; aggregate report/JUnit/annotation inputs are redacted
  too.
- **Checksums:** verification now rejects absolute paths, `..`, backslashes,
  duplicate entries, symlinks and entries that escape the bundle. Every regular
  bundle file except `checksums.sha256` must be covered exactly once, and the
  finalizer now checksums `.complete` as well as `manifest.json`.
- **Staging/teardown:** staging copies are chunked, deadline-checked, bounded by
  a per-case disk budget (`DEFAULT_MAX_CASE_BYTES`, configurable on the engine/
  runner) and run off the event loop (`asyncio.to_thread`). Teardown steps, log
  collection, forced stop and final close share one bounded teardown deadline.
- **Run quota:** `report.json`, `junit.xml` and `annotations.json` are serialized,
  reserved against `max_run_bytes` and re-rendered with final accounting;
  finalize metadata was already charged through the bundle quota.

Tests/results (2026-10-27, after this pass):

```sh
mcp/.venv/bin/python -m pytest   mcp/project/tests/unit/test_scenario*   mcp/project/tests/corpus   mcp/project/tests/integration/test_scenario_real.py -q
# Historical: 115 passed, 1 skipped (FreeMiNT has no prebuilt smoke binaries)

cd mcp/project
HATARI_TOS=$HOME/Projects/Hatari/tos/emutos/etos192us.img   ../.venv/bin/python -m hatari_mcp.scenario_cli run   examples/scenarios/boot-capture-stop.json --artifacts /tmp/p6-fix2 --json
# passed=1, failed=0, skipped=0, error=0, cancelled=0; exit 0; run_budget used
# 1494734 bytes. Bundle checksums verify, including manifest.json and .complete.
```

Regression tests now cover each second-pass bypass: parse/debug/short/media/
recorder options at config, step, determinism and replay levels; recording
shortcuts; non-temp/non-fresh workspaces; redaction of a non-secret-looking
declared env value across all text artifacts; checksum escape/symlink/coverage;
staging deadline and budget; one teardown deadline; and run-root CI files counted
in `max_run_bytes`.

Remaining gaps:

- A user-supplied fresh temp workspace is created/reused as given; matrix cases
  that explicitly share one workspace can still contend. The recommended path is
  to omit `workspace` and use the runner-created per-case directory.
- Bundles produced by the earlier finalizer do not checksum `.complete` and are
  now rejected by replay; re-record them.
- `run_prg` now refuses corpus/repository workspaces outright instead of mounting
  them read-only; stage such programs into a private temp workspace first.
- No new P6 Hatari process/socket remained after the real run. A pre-existing
  probe process (`/tmp/probe-*`) from outside this task was left untouched.

#### Final reconciliation (2026-10-27)

- Full suite after the latest fixes: **452 passed, 1 skipped**; the skip is the
  FreeMiNT prebuilt-corpus test (binaries unavailable), and the single warning
  is `asyncio_mode` because `pytest-asyncio` is absent.
- Server registers **102 tools**. The real `boot-capture-stop` scenario passes;
  an AHCC CLI compile produces a 7,074-byte TOS.
- Independent adversarial reviews were run; the latest P6/P7/P8 fixes are under
  final review, not yet signed off. Earlier counts (46/74/115/323) are
  historical and must not be quoted as current.

#### Final P6 security-fix pass (2026-10-27)

Fixed the last P6 escapes in `services/scenario.py` and the P6 tests:

- **Control-channel options:** removed `--bios-intercept` from the Hatari
  allowlist (the XBios_HatariControl escape). Also removed `--natfeats`,
  `--conout`, `--disasm` and `--memconv`. `natfeats` was dropped from scenario
  `config`/`emu_start` keys too, so it cannot be re-enabled outside the argument
  allowlist. The allowlist remains exact-long-option-only; short aliases,
  positional media/drives, `--acsi/--scsi/--ide-*`, `--disk-a/b`, recorder/output
  options and `--parse/--debug` stay denied. Regression coverage now includes
  every short alias plus the removed options; benign `--fast-boot`,
  `--fast-forward`, `--sound`, `--memsize`, `--rtc-year`, `--timer-d`,
  `--fastfdc`, `--cpu-exact`, `--monitor` and `--statusbar` still parse.
- **`run_prg` workspaces:** scenario `run_prg` steps now validate an explicit
  `workspace` with `_validate_workspace_path` and `_reject_protected_path`, and
  at runtime the workspace is forced to the runner-created per-case directory.
  Explicit `/tmp`, protected corpus/repository roots, non-empty host dirs and
  non-runner fresh temp dirs are rejected; the program must resolve inside the
  runner workspace. `/tmp` itself is never mounted read-write.
- **Early secret collection:** declared env/var values are collected before
  `resolve_case`, so a resolution failure (for example a secret interpolated into
  `config.tos`) still redacts the case message and `report.json`, `junit.xml`
  and `annotations.json`.
- **Artifact-name redaction:** artifact/capture path components are redacted
  before the file is created; finalization fails closed if a secret-bearing name
  remains on disk. `manifest.json`, `checksums.sha256` and step artifact fields
  now contain only the redacted path. Covered with a capture path containing
  `${CAPTURE_SECRET}`.
- **Writable media:** `media_insert_floppy`/`media_insert_cartridge` paths are
  rejected at validation time inside corpus/repository roots and, at runtime,
  unless the image is inside the per-case workspace. External images are never
  mounted writable. Tests cover a corpus image and an image outside the runner
  workspace.

Verification (requested subset and real run):

```sh
mcp/.venv/bin/python -m pytest \
  mcp/project/tests/unit/test_scenario* \
  mcp/project/tests/corpus \
  mcp/project/tests/integration/test_scenario_real.py -q
# 157 passed, 1 skipped (FreeMiNT prebuilt binaries unavailable)

HATARI_TOS=$HOME/Projects/Hatari/tos/emutos/etos192us.img \
  ../.venv/bin/python -m hatari_mcp.scenario_cli run \
  examples/scenarios/boot-capture-stop.json \
  --artifacts /tmp/p6-final-review --json
# passed=1, failed=0, skipped=0, error=0; bundle checksums verify.
```

No P6 Hatari process, runtime directory or control socket remained. A
pre-existing `/tmp/probe-*` Hatari process from outside this task was left
untouched.

Remaining gaps: scenario steps can still use the runner workspace only, so
programs must be staged (no direct corpus `run_prg`); the standalone `run_prg`
tool retains its own less-strict workspace policy outside the P6 runner; and
P6.4 caps, P6.7 Docker/CI, P6.10–P6.13 and exact replay remain out of scope.

## Adversarial review

**Provenance:** an earlier self-review could not spawn an independent reviewer;
independent adversarial reviews were subsequently run and their findings are
recorded above. The table below is retained as history; the latest fixes are
under final review.

| # | Finding | Severity | Disposition in this plan |
| --- | --- | --- | --- |
| 1 | "Attach to a running Hatari" is not possible via the control socket: Hatari connects out once; only the listener owns the connection. | High | §11 redefines attach as FIFO + session relay; brokered/patched attach is P7; `attach_limited` refusals; ADR required. |
| 2 | Corpus roots cannot be GEMDOS drives; the guest writes to the host tree. | High | §12 mandates read-only staging; acceptance criterion 9 and corpus tests check tree hashes. |
| 3 | Resource caps via `RLIMIT_*` are not throttles and cgroups may be unavailable. | High | §4 records the mechanism, uses generous defaults, separates policy from security boundary, and adds `--strict-caps`. |
| 4 | Exact determinism is overclaimed; `--rtc-year` fixes only the year and host-timed input is not frame-locked. | High | §8 strict/tolerant/off, residual-nondeterminism manifest, repeatability measurement; exact replay gated on P7.4/P7.5. |
| 5 | PNG byte comparison is fragile; encoders/metadata can differ. | Medium | All hashes are decoded RGBA; §8 and P6.8. |
| 6 | Scenario `args` can redirect sockets/FIFOs/logs or touch host paths. | High | §13 allowlist/denylist and path confinement; `scenario_validate` rejection tests. |
| 7 | Docker image may violate licenses by bundling TOS/AHCC/corpus. | High | §7 forbids them; build-time checks; external roots mounted read-only. |
| 8 | Coverage (P6.11) is research, not a committed deliverable; breakpoint-based basic-block coverage is impractical. | Medium | Marked [R]/[P], `unsupported` contract, spike-only, no gate dependency. |
| 9 | Fuzzing can trigger host side effects through MMIO/DMA/media. | High | Guest-RAM allowlist, devices off, no writable media, sandbox and caps; §10. |
| 10 | PID reuse can make the session manager kill an unrelated process. | High | Ownership token + process start time/pidfd; dry-run cleanup; never kill unowned. |
| 11 | Parallel state dumps can exhaust disk; `RLIMIT_FSIZE` conflicts with large states. | Medium | Per-case disk budgets, watchdog, retention; caps recorded. |
| 12 | Skipped corpus vs failed case is ambiguous in CI. | Medium | Explicit statuses, `--fail-on-skip`, exit-code table. |
| 13 | Committed baselines create cross-version flakiness. | Medium | Same-build generation, provenance, tolerance, baselines optional. |
| 14 | P6 depends on P3.12/P3.13 corpus harness; scenarios may all skip in early CI. | Medium | Hard dependency stated; skips are explicit; corpus job self-hosted/optional. |
| 15 | Roadmap says `--fastfloppy`; local Hatari exposes `--fastfdc`. | Low | Use `--fastfdc`, validate profiles against `--help`, document. |
| 16 | Session relay is a new local control surface and could be abused if it trusts paths or peers. | Medium | Runtime-dir `0700`, token auth, opt-in, no TCP, ownership checks, and an ADR; relay is not a substitute for the P7 broker. |
| 17 | The requested review is not independent. | Process | Recorded here; blocking action in Remaining. |

Changes already reflected: attach redesign, corpus staging, cap semantics,
determinism labeling, decoded-pixel hashing, scenario trust rules, license
constraints, coverage scope, fuzz sandbox, session ownership, and CI status
distinctions.
