# P8.1 — Time-travel (snapshot + replay) spike

Status: complete as a static snapshot-cost spike; input replay untested
Verdict: INCONCLUSIVE (snapshot cost and static checkpoints pass; P7.5 input replay missing)
Date: 2026-10-27
Owner: P8 research spike
Review: independent review pending; self-review recorded in the P8 plan
Harness: `mcp/project/labs/time_travel_spike.py`

## Question and hypothesis

Can periodic machine snapshots plus replay provide useful "step backward" at
acceptable cost? This is bounded snapshot + replay, **not** instruction-level
reverse execution. Hypothesis: stock `statesave` is small and fast enough at
25–100 VBL cadence, and replay from a snapshot is deterministic for a
no-input workload.

## Pinned environment

| Item | Value |
| --- | --- |
| Host | Linux 6.17.0-1020-oracle, aarch64 |
| Hatari | v2.6.1-devel (2026-10-27) |
| Machine | ST, 1 MiB, EmuTOS `etos192us.img` |
| EmuTOS SHA-256 | `2241df90967a81b29423e0844a75e1e2faabdc03a87fe7c95647860dcaa995c1` |
| Flags | `--sound off --fast-boot on --timer-d on --rtc-year 1990 --statusbar off --drive-led off` |
| Cadence | 50 VBL (1 s); snapshots at 50…500 VBL |
| Checkpoints | 150, 300, 450 VBL |
| Memory window | 16 KiB at `0x10000` (configurable) |

## Method

Command:

```sh
cd mcp/project
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs time-travel \
  --out /tmp/hatari-mcp-p8-audit/time-travel
```

1. Start ST/EmuTOS headless and wait for each 50-VBL boundary.
2. Save `statesave` at each boundary; record file size and save latency.
3. At 150/300/450 VBL, capture a PNG screenshot and a 16 KiB RAM window at
   `0x10000` as the forward reference.
4. Rewind to each checkpoint by loading the snapshot 50 VBL earlier. Time both
   the command round-trip and the **observable restore completion**: after the
   load command returns, poll the guest VBL counter until it reflects the loaded
   snapshot (within a small window, because emulation continues while the
   debugger answers). Then replay until the recorded VBL and compare the
   screenshot SHA-256 and RAM window.
5. Replay each checkpoint twice from the same snapshot to test replay
   determinism independent of the forward run.

`--statusbar off --drive-led off` is required for screenshot comparison. With
the stock status bar or drive LED enabled, the host overlay changes between
forward and replay captures even though the guest framebuffer is identical.

## Results

Raw data: `/tmp/hatari-mcp-p8-audit/time-travel/time-travel/time_travel.json`.

### Snapshot cost

| Quantity | Value |
| --- | ---: |
| Snapshots | 10 |
| Size median | 126,880 bytes (123.9 KiB) |
| Size min–max | 126,516–126,995 bytes |
| Save median | 46.2 ms |
| Save max | 89.5 ms |
| `stateload` command round-trip median | 15.8 ms |
| Observable restore completion median | 41.2 ms |
| Save + observable load | 87.4 ms |
| Rewind total max (load + 50 VBL replay + captures) | 0.882 s |

Cadence estimates from the measured median size:

| Cadence | Snapshots per 60 s | Disk per 60 s |
| ---: | ---: | ---: |
| 25 VBL | 120 | 14.52 MiB |
| 50 VBL | 60 | 7.26 MiB |
| 100 VBL | 30 | 3.63 MiB |

All estimates are far below the 64 MiB NO-GO threshold.

### Replay correctness (static, no input)

| Checkpoint | Snapshot VBL | Replayed VBL | Screenshot match | RAM window match |
| ---: | ---: | --- | --- | --- |
| 150 | 102 | 150 | yes | yes |
| 300 | 250 | 300 | yes | yes |
| 450 | 401 | 450 | yes | yes |

Replay-from-same-snapshot was deterministic 3/3 for both screenshot and RAM.
The forward reference and the replay also matched at all three checkpoints
after disabling the host status bar and drive LED.

**These checks are static.** The workload injects no guest input and the
screenshot/RAM window does not exercise an input-driven animation. Matching
screenshots therefore do not prove that replay reproduces input timing or
guest behavior.

## GO / NO-GO

- **Snapshot cost: passes** (`go_cost=true`): 126.9 KiB per snapshot, save
  median 46.2 ms, observable restore median 41.2 ms, rewind max 0.882 s.
- **Static checkpoint comparison: passes** (`go_static_checkpoints=true`), but
  it is explicitly insufficient for time-travel correctness.
- **Input replay: untested** (`input_replay_proven=false`). P7.5 is not
  available in this stock build, and no evidence record exists.
- **Overall: NO-GO for time travel; INCONCLUSIVE.** The harness sets `go=false`
  for any run without a validated input-replay evidence record, even when the
  snapshot cost and static checks pass.

### Input-replay evidence contract

A bare `--input-replay-proven` flag is **insufficient** and intentionally keeps
the verdict INCONCLUSIVE. A GO requires `--input-replay-evidence <json>` with:

- a `bundle_path` that exists;
- at least three `checkpoints`, each with a `vbl`, matching screenshot and
  memory SHA-256 hashes, and an explicit match result;
- a non-empty `hashes` object containing at least one SHA-256;
- a positive `event_count`; and
- a matched/passed `replay_status`.

Fabricated counters, a missing bundle, unresolved checkpoints, or a bare
boolean cannot produce a time-travel GO.

The correct promotion wording is: **snapshot cost is acceptable; input-exact
rewind remains untested and conditional on P7.5.**

## Limitations and safety

- No guest input was injected, so input replay fidelity is not measured.
- The RAM window at `0x10000` is mostly static; the screenshot is the stronger
  visual check, but neither is dynamic evidence.
- Low RAM at `0x1000` changes across debugger commands because it contains
  VBL/timer state; it is not a valid exact-match window across command
  boundaries.
- `stateload` does not undo host-side effects (disk writes, media changes).
- Raw state files and images are under `/tmp`, not git. Remove
  `/tmp/hatari-mcp-p8-*` when the investigation is closed.
- No production checkpointing, memory diffing, or reverse execution was added.

## Promotion prerequisites

A promoted time-travel plan needs P7.5 frame-locked input, a deterministic
input-driven workload, a documented input log format and evidence schema,
side-effect isolation/replay policy, a disk budget, and a test corpus with
input-driven scenarios. A new ADR and plan are required.
