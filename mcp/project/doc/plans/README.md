# Implementation plans

Status: living documents.
Last updated: 2026-10-27.
Source of truth: `../roadmap/README.md`.

One plan per roadmap phase. Plans are the hand-off contract between the
orchestrator and implementation subagents: a subagent must update its plan's
work log and next steps before reporting completion, and an independent
reviewer must record findings in the review section.

| Phase | Plan | Theme |
| --- | --- | --- |
| P0 | `P0-foundation.md` | Process supervisor, channels, demuxer, profiles, CI |
| P1 | `P1-mvp-headless-loop.md` | Headless compile/debug loop, AHCC CLI/GUI entry points |
| P2 | `P2-graphical-rig.md` | Deterministic capture, pixels, input scripting |
| P3 | `P3-build-run-ahcc.md` | run_prg, AHCC matrix, external corpus harness |
| P4 | `P4-debugger-depth.md` | Watchpoints, DSP, profiling, crash analysis |
| P5 | `P5-devices-media.md` | Disks, cartridge, serial/MIDI/SCC, audio/video |
| P6 | `P6-automation-ci.md` | Scenarios, artifacts, parallel matrix, CI |
| P7 | `P7-protocol-upstream.md` | Framed protocol, notifications, upstream patch |
| P8 | `P8-stretch.md` | Time travel, source debugging, research spikes |

## Conventions

- Checkboxes track implementation, not intention.
- `[P]` means the item needs a Hatari patch; `[R]` means research is required.
- Work logs use UTC dates and name the subagent/reviewer role, not a person.
- If a plan changes scope, record the reason; do not silently drop roadmap
  items. Move dropped work to "Remaining / next steps" with a blocker.
- All paths in results and tests must be confined to configured roots.
- Temporary files go under `/tmp`, never under `mcp/project` unless they are
  deliberate fixtures.
- Never commit third-party source or proprietary ROMs.

## Plan template

```markdown
# Pn — Title

Status: planned | in progress | complete | partial
Owner: phase implementer
Last updated: YYYY-MM-DD
Roadmap: Pn.*

## Goal
## Dependencies
## Deliverables and file ownership
## Design / constraints
## Task checklist
## Acceptance criteria
## Test plan
## Risks and limitations
## Work log
### Done
### Remaining / next steps
## Adversarial review
```

## Status snapshot

Plans exist for all phases, but **no phase is fully complete**. Do not infer
completion from checked boxes or from the fact that a plan exists.

| Phase | Status (authoritative details in the plan header and Work log) |
| --- | --- |
| P0 | partial — core supervisor/channels/demux/profiles; CI and P0.3a note remain |
| P1 | partial — MVP loop live-verified; CI and residual stock limits remain |
| P2 | partial — host-timed frames/input; frame-lock, absolute mouse, AVI/audio, GUI remain |
| P3 | partial — AHCC/run/corpus; Pterm auto-exit and native corpus toolchains remain |
| P4 | partial — advanced subset; stock watchpoints, DSP parity, trace files, source mapping remain |
| P5 | partial — media/devices subset; HD/cartridge hot-swap and live streams remain |
| P6 | partial — scenarios/artifacts/matrix; caps, Docker, fuzzing, coverage, attach, session manager remain |
| P7 | partial — server protocol/capabilities; framed handshake and Hatari patches remain |
| P8 | partial/INCONCLUSIVE — spikes only; latest fixes under final review |

The current full suite is **452 passed, 1 skipped** (FreeMiNT prebuilt corpus
binaries unavailable); the server registers 102 tools. Per-plan work logs carry
the exact remaining blockers; `../roadmap/README.md` is the higher-level
summary.
