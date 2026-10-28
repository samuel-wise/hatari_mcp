# P2 — Graphical test rig

Status: partial — host-timed frame capture and input scripting implemented; frame-lock, absolute mouse, AVI/audio, screen-mode and AHCC GUI coverage remain
Owner: phase implementer (graphics/input)
Last updated: 2026-10-27
Roadmap: P2.1–P2.15, gate in roadmap §5 Phase 2

## Goal

Turn P1 screenshots and input into a deterministic-enough graphical test rig:
stop near a chosen frame, capture raw pixels, checksums, regions, bursts, and
diffs; script and replay timed input; record AVI/audio; position the mouse
absolutely with a documented corner-pin helper; verify screen modes; and drive
AHCC GUI scenarios through the same screenshot/input tools. Where stock Hatari
cannot provide exact frame locking, the plan says so and provides a measured
fallback instead of claiming determinism it does not have.

## Dependencies

* P1 complete: lifecycle, workspace/GEMDOS, autostart, NatFeats/console,
  symbols, breakpoints, execution control, memory/state, screenshot, input,
  safety, AHCC CLI/GUI entry points.
* P0.3a decision and pager guard.
* A patched frame-locked path is **not** required for P2.1–P2.2, P2.4–P2.10,
  P2.12–P2.15; only P2.3 (frame stepping and frame-locked commands) is `[P]`.
* Pillow (`pillow>=10`) for PNG decode/diff. Pin it and canonicalize to RGBA so
  decoder differences do not silently change hashes.
* Optional corpora: Okami-Shell, GodLib, FreeMiNT; discovered by path, read-only,
  skipped when absent. AHCC GUI needs the local AHCCST.PRG and a TOS image.
* No proprietary ROMs in CI; EmuTOS for deterministic tests.

## Deliverables and file ownership

All server paths are under `mcp/project/src/hatari_mcp/`.

| Path | Purpose |
| --- | --- |
| `mcp/project/src/hatari_mcp/frameclock.py` | `VBL`/VBL-vector breakpoint arming, re-arm, frame waits, capture stop points. |
| `mcp/project/src/hatari_mcp/capture.py` | Frame-targeted capture orchestration, burst, manifest, retention. |
| `mcp/project/src/hatari_mcp/pixels.py` | PNG decode to canonical RGBA, regions, probes, checksums. |
| `mcp/project/src/hatari_mcp/image_diff.py` | Exact/per-pixel diff, tolerance, bounding box, diff image. |
| `mcp/project/src/hatari_mcp/video.py` | AVI start/stop/status, bounded `--run-vbls` clips. |
| `mcp/project/src/hatari_mcp/audio.py` | WAV/YM recording via `soundout` + `recsound`. |
| `mcp/project/src/hatari_mcp/input_script.py` | Timed event sequences, record/replay, frame-paced fallback. |
| `mcp/project/src/hatari_mcp/mouse.py` | Corner-pin absolute positioning, click-at, drag. |
| `mcp/project/src/hatari_mcp/screen_modes.py` | Mode matrix, borders/overscan, capture verification. |
| `mcp/project/src/hatari_mcp/ahcc_gui.py` | GUI scenario anchors and helpers layered on P1. |
| `mcp/project/src/hatari_mcp/tools/capture.py` | `frame_*`/`screen_*` frame tools. |
| `mcp/project/src/hatari_mcp/tools/pixels.py` | `screen_get_framebuffer`, probes, hashes, crops, diffs. |
| `mcp/project/src/hatari_mcp/tools/video.py` | `video_record_*`, `audio_record_*`. |
| `mcp/project/src/hatari_mcp/tools/input_script.py` | `input_script_*`, `input_mouse_click_at`, drag. |
| `mcp/project/src/hatari_mcp/tools/ahcc_gui.py` | `ahcc_gui_scenario_*`. |
| `mcp/project/tests/unit/` | Frame math, pixel/diff, script scheduling, retention, mode tables. |
| `mcp/project/tests/integration/` | Frame capture, hashes, input, AVI/audio, modes, GUI. |
| `mcp/project/tests/corpus/` | Recorded frames/baselines and optional corpus scenarios. |
| `mcp/project/pyproject.toml` | Add `pillow>=10` (or a `graphics` extra). |
| `mcp/project/doc/tools-reference.md` | Add the new tools and determinism caveats. |
| `mcp/project/patches/` | Only P2.3 frame-lock/step patch, if pursued. |

## Design / constraints

### 1. Frame clock and targeted capture (P2.1, P2.2, P2.3)

Stock Hatari has no interactive frame-step API. `--run-vbls <n>` exits the
process after N VBLs, so it cannot be used to pause, capture, and continue. P2
therefore builds a frame clock from debugger breakpoints.

* There is **no `_vbl` builtin symbol** in Hatari. The builtin debugger variable
  is `VBL` (a counter); the TOS VBL vector lives at guest address `$70`.
  Examples that use `_vbl` are documentation bugs unless a TOS symbol file
  happens to define it.
* `frameclock.read_vbl()`: `evaluate VBL` on the active channel; parse the
  decimal/hex result.
* `frameclock.arm(target_vbl, marker)`: set a temporary breakpoint
  `VBL = <target> :once :print __HMCP_FRAME_<uuid>__`. The `:print` option makes
  the hit detectable without suppressing the stop. Track the breakpoint ID for
  cleanup.
* Because the breakpoint command itself takes host time and the target counter
  may already have passed, `wait_frames(n)` computes
  `target = current + max(n, 2)` and re-arms a bounded number of times if no hit
  arrives. Return the actual `vbl_at_stop` and `effective_frames`; never claim
  the requested frame count if it was adjusted.
* Optional exact-boundary mode `boundary="vbl_vector"`: set
  `pc = ($70) :once :print ...` to stop at the TOS VBL vector entry. This is
  closer to the start of a frame but depends on a valid vector and on TOS not
  changing it. It is experimental and tested separately.
* `run_frames(n, capture=false, resume=true)`: stop at the target VBL, optionally
  screenshot, then `cont` on stdin if `resume`. This is a stop/go loop, not a
  frame-step instruction. It is slower and perturbs timing; document
  `frame_locked: false` and `overhead_ms`.
* `wait_vbl()` = `wait_frames(1)`; `wait_frames(n)` = stop at a future VBL;
  `wait_for_screen_change(timeout_ms, region?, interval_ms=100)` polls
  screenshots/hashes while running or via stop/capture/resume, returning
  `changed`, `elapsed_ms`, `samples`, `last_hash`, and a best-effort frame count.
  It is explicitly not deterministic.
* **P2.3 `[P]`:** true frame stepping and a frame-locked command queue require
  an upstream patch that polls the control channel from the debugger/emulation
  loop or exposes a frame-step command. Until then:
  * `frame_step(n)` uses the VBL-breakpoint stop/go fallback and is marked
    `patched: false`;
  * frame-locked input replay is approximated by stopping every frame and
    injecting events, which is slow and still not cycle-exact;
  * the patch lives in `patches/`, is optional, and the server detects it.
* Capture stop point: a screenshot at the VBL stop reflects the framebuffer at
  that exact stop, not necessarily a complete rendered frame. The plan defines
  the contract as "pixels at the stop point"; two captures at the same stop
  point with the same state/input must hash identically. Do not promise "the
  final image of frame N" without a measured test.

### 2. Raw pixels, regions, checksums, diffs (P2.4–P2.7)

`screen_get_framebuffer(format="rgba"|"png", region?, scale?)`:

* capture through the P1 debugger `screenshot` path to a private temp file;
* decode with Pillow, convert to canonical 8-bit RGBA (straight alpha, no
  palette), crop the requested region, optionally nearest-neighbor scale with
  integer factors only;
* compute `sha256` over `width, height, "RGBA", pixel_bytes` and `crc32`;
* return `{width, height, mode, region, sha256, crc32, bytes, data_b64?,
  artifact_path?}`. Inline only up to a hard cap (default 256 KiB); otherwise
  return a resource URI/artifact path and a checksum. Never return a 640×400
  RGBA buffer as a megabyte of JSON.
* include the screen mode/monitor, scale, and whether borders are included in
  the hash metadata, so a hash is not compared across incompatible captures.

`screen_probe(points, region?)`: return RGBA/hex for up to N points (default 64,
cap 1024); reject out-of-bounds with a typed error.

`screen_hash(region?)`: same canonicalization; deterministic for a given
emulator state and mode.

`screen_crop(region, format="png"|"rgba")`: capture and crop without exposing a
user-visible file; temp file is deleted. If `save_path` is provided, path-
confined.

`screen_diff(baseline, current, tolerance=0, region?, diff_path?)`:

* baseline/current are artifact paths or inline hashes; decode both to RGBA;
* require identical dimensions unless `allow_resize=true`;
* exact mode: byte equality and hash;
* tolerance mode: per-channel absolute delta <= tolerance; count changed pixels,
  max delta, percentage; bounding box of changed pixels;
* optional diff image with changed pixels highlighted; path-confined;
* return `{equal, changed_pixels, total_pixels, max_delta, bbox,
  diff_sha256, diff_path?}`. No hidden client-side assumption: deterministic
  data is always returned, and the diff image is optional.
* If the client can do image diffing, it can request hashes/raw regions only;
  the helper stays optional.

`screen_burst(frames, interval_frames=1, region?, prefix?)` (P2.6):

* arm/wait/capture/resume for each frame; write a manifest with VBL numbers,
  hashes, timestamps, dimensions, mode, and paths;
* enforce a retention policy (default 100 files / 512 MiB / 24 h) and a
  `--keep`/`cleanup` switch; never fill the workspace silently;
* a burst is slow and perturbs timing; return `frame_locked: false` and
  `overhead_ms`.

### 3. AVI and audio (P2.8)

AVI:

* AVI codec/fps/file are read when recording starts. Set them at launch:
  `--avirecord off --avi-vcodec png --avi-fps <fps> --avi-file <path>`.
* Start/stop with `hatari-shortcut recanim` (or `hatari-option --avirecord
  on/off` after verifying runtime behavior); detect the
  `AVI recording has been started/stopped` log lines. Return path, size,
  duration/fps if parseable, codec, and `was_recording`.
* PNG codec requires a libpng build; detect and fail with a clear
  `unsupported` error instead of producing a broken file. BMP is the fallback.
* Changing file/codec/fps while recording requires stop, reconfigure (restart
  if runtime options are rejected), and start again; do not pretend a live
  change took effect.
* Bounded clips: `--run-vbls N --benchmark --avirecord on --avi-file <path>`
  records a fixed number of frames and exits. This is useful for deterministic
  short clips and does not need interactive capture. It is a separate helper
  from `recanim`.
* AVI includes audio only when sound is enabled and the build supports it; do
  not claim audio in AVI without checking the file/streams.

Audio:

* `--sound off` (P1 default) makes `recsound` a no-op. Audio recording requires a
  restart with a real frequency (e.g. `--sound 44100`); return
  `restart_required` with the exact start arguments if sound is off.
* Set the output path with `hatari-path soundout <path>` **before** toggling
  `hatari-shortcut recsound`. The format comes from the filename extension
  (`.wav` or `.ym`); there is no per-recording path argument in the shortcut.
* Start/stop by toggling `recsound`; verify the file exists, has a plausible
  header/RIFF size, and is closed (no recording in progress) before returning.
* Do not use the UI shortcut's screenshot directory for audio; path policy
  applies.

### 4. Absolute mouse and drag (P2.9)

Stock Hatari only exposes relative `mousemove <dx> <dy>`. Absolute positioning
uses the corner-pin trick documented in `control.c`:

1. send a large negative relative move (e.g. `-10000,-10000`) so the guest
   clamps at the top-left corner;
2. send the target delta `(x, y)`, optionally scaled for the guest screen;
3. verify with a screenshot/probe if requested.

`input_mouse_click_at(x, y, button="left", settle_ms?)` performs pin, move,
button down/up, and returns `{requested, pinned, delta, warnings}`.
`input_mouse_drag(from, to, button, steps?)` performs pin, move, down, stepped
move, up. Warn about guest scaling, borders, overscan, and GEM window offsets;
a target may clamp or land on a different pixel than requested. Direct absolute
mouse is P7 `[P]`; never claim pixel-perfect positioning without a probe.

### 5. Input scripting, recording, replay (P2.10–P2.12)

`input_script_run(events, mode="host"|"frame", timeout_ms?)`:

* event types: `key_down`, `key_up`, `key_press`, `text`, `mouse_move`,
  `mouse_button`, `mouse_click_at`, `wait_ms`, `wait_frames`, `screenshot`;
* `mode="host"`: schedule by monotonic time, with optional `at_ms`; simple,
  fast, not deterministic;
* `mode="frame"`: stop at each target VBL, inject, resume; slow, approximate,
  and requires the P2.3 patch for exactness;
* validate total events, text length, and timeout; return an execution log with
  timestamps, frame numbers where known, and `exact: false` unless patched and
  measured.

`input_script_record(start/stop)` records server-side events with host and frame
timestamps. `input_script_replay(events)` replays them; exact replay requires
frame-locked delivery and is marked best-effort otherwise. The event log is
JSON, path-confined, and contains no secrets.

Layout-aware mappings: expose `scancode` and `character` modes. Character mode
uses the same keymap/shift rules as P1 `input_type_text`; scancode mode is
layout-independent. Unicode and dead keys are unsupported. Joystick remains the
P1 keyboard-emulation path; direct axes/buttons are P7 `[P]` and are not
implemented here.

### 6. Determinism (P2.13)

A deterministic capture profile sets:

* fixed profile/machine/memory/TOS;
* `--fast-boot on`, `--fastfloppy on`, `--timer-d on`;
* fixed `--rtc-year` (and no host clock dependence where options allow);
* `--sound off` for video-only captures;
* fixed workspace contents and input script;
* no host display (`SDL_VIDEODRIVER=dummy`) and no host gamepad/input;
* a fixed seed where the test program or emulator uses randomness.

Document what remains non-deterministic: host scheduling, SDL/dummy rendering
details, debugger stop overhead, VBL breakpoint placement, TOS timing, input
delivery timing, and any unpatched frame-lock gap. The determinism test is
empirical: capture the same stop point twice in two fresh runs and compare
hashes; if it fails, report the delta rather than claiming success.

### 7. Screen modes (P2.14)

Test and document capture in:

* ST low/medium/high, color and mono monitor;
* STE, TT, and Falcon modes (TT/Falcon only with suitable TOS/EmuTOS);
* borders on/off and overscan;
* ST/STE/TT/Falcon Videl modes where available;
* crop/statusbar options.

`screen_modes.py` owns a table of mode profiles and expected dimensions/color
depth. `screen_info` and screenshot headers are cross-checked. Hashes include
mode/dimensions; never compare a mono hash to a color hash. Falcon/TT tests skip
if the required ROM is absent. Verify that `--disable-video` is never used.

### 8. AHCC GUI scenarios (P2.15)

Build on P1 `ahcc_gui_start`/`ahcc_gui_status` (a primary-supervisor mode
switch, not a second emulator):

* scenario definitions are JSON/Python data, not a new DSL;
* steps use `ahcc_gui_start`, `wait_for_screen_change`, `screen_probe`/
  `screen_hash` anchors, `input_key`/`input_type_text`/`input_mouse_click_at`,
  `screen_screenshot`, and bounded waits;
* stable anchors are pixel probes/region hashes at known coordinates, not OCR;
* scenarios: open a project, compile, read a dialog/error, capture the result,
  and exit; at least one success and one compiler-error path;
* every step has a timeout and a retry limit; no infinite wait for a human;
* record screenshots, hashes, input log, console/log excerpts, and the AHCC
  version in an artifact bundle;
* document fragile timing: GEM redraws, dialog animations, keyboard repeat,
  window placement, and font/ROM differences. Mark the whole scenario
  `best_effort: true`.
* GUI failure must not affect `ahcc_compile`; CLI tests do not depend on GUI.

## Task checklist

### Frame capture

- [ ] P2.1 Implement `frameclock.read_vbl`, `arm`, re-arm, `run_frames`,
      `frame_capture`; test breakpoint cleanup and missed-target behavior.
- [ ] P2.2 Implement `wait_vbl`, `wait_frames`, `wait_for_screen_change` with
      bounded polling and clear non-determinism metadata.
- [ ] P2.3 `[P]` Add the frame-step/frame-locked patch path behind capability
      detection; keep the stop/go fallback and document the difference.

### Pixels and diffs

- [ ] P2.4 Implement canonical RGBA decode, dimensions, probes, checksums, and
      inline-size caps.
- [ ] P2.5 Implement region capture/crop without a user-visible file.
- [ ] P2.6 Implement burst capture, manifest, retention cleanup, and limits.
- [ ] P2.7 Implement exact/tolerance diff, bounding box, diff image, and
      deterministic data return.

### Video and audio

- [ ] P2.8a Implement AVI start/stop/status, codec/fps/file handling, libpng
      detection, and bounded `--run-vbls` clips.
- [ ] P2.8b Implement WAV/YM recording via `soundout`/`recsound`, sound-enabled
      checks, file validation, and restart guidance.

### Input

- [ ] P2.9 Implement corner-pin absolute positioning, click-at, drag, and
      screenshot/probe verification warnings.
- [ ] P2.10 Implement timed event scripts, key repeat, character/scancode modes,
      and bounded execution.
- [ ] P2.11 Document the no-patch joystick path; keep direct axes/buttons out of
      P2 except as a P7 patch reference.
- [ ] P2.12 Implement input record/replay with host/frame timestamps and
      explicit exactness metadata.
- [ ] P2.13 Add a deterministic capture profile and a two-run hash comparison
      test; document residual non-determinism.

### Screen modes and GUI

- [ ] P2.14 Add the screen-mode matrix, border/overscan tests, and expected
      dimensions; skip absent TT/Falcon ROMs.
- [ ] P2.15 Add AHCC GUI scenarios with pixel anchors, bounded waits, artifacts,
      and documented fragility; never block the CLI.

## Acceptance criteria

1. A test can boot a program, wait for a chosen VBL, capture a PNG and canonical
   RGBA pixels, and return dimensions/checksums.
2. Two captures at the same stop point in two fresh runs with identical
   state/input produce identical hashes; if they do not, the result reports the
   difference and does not claim success.
3. `wait_vbl`/`wait_frames` return the actual VBL at stop and do not hang when a
   target is missed; temporary breakpoints are cleaned up.
4. `screen_get_framebuffer` returns inline data only under the cap, otherwise a
   path/resource plus checksum; regions and probes are bounds-checked.
5. `screen_diff` returns exact/tolerance results, changed count, max delta,
   bounding box, and an optional diff image; identical inputs compare equal.
6. Burst capture produces a manifest, respects retention limits, and reports
   timing overhead.
7. AVI start/stop produces a playable file with the configured codec/fps, or a
   typed `unsupported` error; changing config mid-recording is refused or
   restarted explicitly.
8. Audio recording produces a valid WAV/YM file when sound is enabled, and
   returns `restart_required` when P1's `--sound off` is active.
9. Absolute mouse click-at/drag reaches a known target (verified by a pixel
   probe or guest reaction) and returns scaling/clamping warnings.
10. Input scripts run in host and frame modes, record/replay, and honestly report
    `exact: false` unless frame-locked.
11. Screen-mode captures match expected dimensions/color depth for the tested
    ST/STE modes; TT/Falcon tests skip cleanly when ROMs are absent.
12. An AHCC GUI scenario can open/compile/capture with bounded waits, and a GUI
    failure does not break `ahcc_compile`.
13. No `--disable-video`, no direct joystick event, and no screenshot via the UI
    shortcut path.

## Test plan

### Unit tests

* `tests/unit/test_frameclock.py`: VBL parse, target arithmetic, re-arm bounds,
  missed target, cleanup, breakpoint marker parsing.
* `tests/unit/test_pixels.py`: RGBA canonicalization, palette/alpha cases,
  region crop, bounds, checksum stability, inline cap.
* `tests/unit/test_image_diff.py`: exact/tolerance, bbox, dimensions mismatch,
  diff image determinism.
* `tests/unit/test_capture_manifest.py`: naming, retention, cleanup, path
  confinement.
* `tests/unit/test_input_script.py`: event ordering, host/frame scheduling,
  key repeat, validation, record/replay JSON.
* `tests/unit/test_mouse.py`: corner-pin math, scaling/clamping warnings, drag
  steps.
* `tests/unit/test_screen_modes.py`: mode table, expected dimensions, mono/color
  separation.
* `tests/unit/test_video_audio.py`: AVI option builders, libpng detection, WAV
  header checks, sound-off restart error.
* `tests/unit/test_ahcc_gui_anchors.py`: anchor matching, probe tolerances,
  bounded retries.

### Integration tests (real Hatari + EmuTOS)

* `test_frame_capture.py`: boot a test program, wait VBL, capture twice at the
  same stop, compare hashes; capture a burst and verify manifest/retention.
* `test_wait_change.py`: wait_for_screen_change detects a deliberate color
  change and times out cleanly when nothing changes.
* `test_pixels_regions.py`: full frame, crop, probe, checksum; compare a known
  colored region.
* `test_diff.py`: identical and deliberately different captures; tolerance and
  bbox.
* `test_avi.py`: record a short clip via `recanim`; inspect file size/header;
  try PNG codec on a no-libpng build (expect typed error).
* `test_audio.py`: restart with sound, set `soundout`, toggle `recsound`, verify
  WAV/YM; assert sound-off returns `restart_required`.
* `test_absolute_mouse.py`: click a known GEM control and verify a screen
  change/probe; drag with intermediate steps.
* `test_input_replay.py`: record a short input script and replay it; verify the
  observable screen/console change; assert exactness metadata.
* `test_screen_modes.py`: ST low/med/high, mono, borders; TT/Falcon skip when
  ROMs absent.
* `test_ahcc_gui_scenario.py`: launch GUI mode, open/compile a project, capture
  a success and an error dialog; skip without AHCC/TOS.

### Corpus tests

* Store small canonical frame fixtures (PNG + RGBA hash metadata) generated by
  the test programs, never third-party art or ROMs.
* Optional local corpus scenarios, each declaring external root, ROM/toolchain,
  deterministic inputs, expected artifacts, and skip reason:
  * a GodLib demo or test binary with keyboard/mouse input and a baseline frame;
  * an Okami-Shell TTP tool with a screenshot/console assertion;
  * a FreeMiNT/libcmini component only if it builds with the available toolchain.
* Corpus tests must not copy source into the repository. Run read-only from the
  discovered tree and record logs/diffs only.

### Protocol/E2E

* Drive capture/pixel/input tools through MCP stdio and assert result envelopes,
  resource URIs, and artifact paths.
* Assert no stdout pollution from Pillow, AVI, or child processes.
* Assert a failed capture/diff returns `is_error=True` with a typed code.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| No exact frame boundary in stock Hatari | Hashes vary with stop point | VBL/vector stop-point contract; two-run test; P2.3 `[P]` |
| VBL breakpoint target missed due to host latency | Waits hang or skip frames | `+2` margin, bounded re-arm, return actual VBL |
| Breakpoint overhead changes timing | Non-deterministic guest behavior | Document; measure overhead; keep capture intervals short |
| `_vbl` is not a builtin | False breakpoint setup | Use `VBL`/`($70)`; test symbol presence |
| Debugger pager reads stdin | Capture/disasm hangs | P0 pager guard; `pager_blocked` |
| Pillow decode/version drift | Hash instability | Pin Pillow; canonical RGBA; include dimensions/mode |
| RGBA buffers are large | Context blow-up | Inline cap; artifacts/resources; region caps |
| Screen scaling/borders change pixels | False diffs | Metadata in hash; mode-specific tests; no cross-mode compare |
| AVI codec/fps fixed at start | Runtime config silently ignored | Launch-time config; stop/restart; explicit error |
| No libpng | PNG AVI codec fails | Detect; BMP fallback; typed error |
| Audio requires sound on | `recsound` no-op | `restart_required` with exact args |
| WAV/YM path is global config | Wrong file overwritten | Set `soundout` before start; path confinement; confirm closed |
| Absolute mouse clamps/scales | Wrong click target | Corner pin, probe/verify, warnings; P7 `[P]` |
| Joystick only keyboard-emulated | Guest key theft, no analog | Document; no direct claim; P7 patch |
| Frame-locked input unavailable | Replay not exact | Stop-per-frame fallback; `exact:false`; P2.3 `[P]` |
| AHCC GUI anchors fragile | Flaky scenarios | Pixel anchors, bounded retries, best-effort, artifacts |
| Burst/AVI storage growth | Disk exhaustion | Retention, caps, cleanup, manifest |
| TT/Falcon ROMs absent in CI | Coverage gap | EmuTOS/ST matrix; skip cleanly |
| Corpus licenses | Distribution risk | Read-only external roots; no source copying |

## Work log

### Done

* 2026-10-27, planning subagent: authored this plan, mapped P2.1–P2.15, and
  verified stock frame/capture/input constraints in `control.c`, `debugui.c`,
  `debugcpu.c`, `breakcond.c`, `vars.c`, `avi_record.c`, `wavFormat.c`,
  `shortcut.c`, `joy.c`, and `options.c`.
* 2026-10-27, planning subagent: corrected the `_vbl` assumption to `VBL`/`$70`
  and documented the VBL-breakpoint stop-point contract.
* 2026-10-27, planning subagent: separated the no-patch capture/diff/input
  scope from the P2.3 `[P]` frame-lock work.
* 2026-10-27, frames/scripting implementer: added `tools/frames.py` with
  stdlib BMP decode (uncompressed 24/32-bit, bottom-up and top-down rows, row
  padding, BGR order, all-zero 32-bit alpha treated as opaque), frame
  capture/info, pixel/region hashes, exact frame diff with bbox and mean/max
  channel delta, a BMP diff image under `/tmp`, and bounded polling stability.
* 2026-10-27, frames/scripting implementer: added `tools/scripting.py` with
  host-timed key/keydown/keyup/text/mouse_move/mouse_button sequences, reverse
  key-combo release, JSON save/load/replay, upfront event validation, per-event
  results, and an explicit `unsupported` error for absolute mouse coordinates.
* 2026-10-27, frames/scripting implementer: added
  `tests/unit/test_frames_scripting.py`; `py_compile` is clean and all 12 tests
  pass with `mcp/.venv/bin/python`.
* 2026-10-27, frames/scripting implementer: registration in `server.py` and
  `doc/tools-reference.md` were left untouched per file ownership; both are
  required before the tools are exposed to MCP clients.

* 2026-10-27, reconciliation: host-timed frame capture, BMP subset decode,
  pixel/region hashes, exact diff, stability polling and JSON input
  scripting/record/replay are implemented and registered. The real
  boot-capture-stop scenario passes; no exact frame-lock or absolute-mouse
  claim is made.

### Remaining / next steps

* No stock frame-lock/frame-step mechanism: P2.3 remains `[P]` (Hatari patch),
  so captures and replay are host-timed/approximate.
* No stock absolute mouse positioning; `input_mouse_click_at` stays an
  explicit `unsupported` result until a corner-pin/patch design is proven.
* P2's AVI/audio recording, screen-mode matrix and AHCC GUI modules are not
  implemented; P5 only exposes audio/video device toggles, and AVI/audio
  capture depends on the local libpng/sound build.
* Two-run hash stability has not been measured long enough to publish a
  "deterministic" claim.
* The BMP path is intentionally limited to uncompressed 24/32-bit BI_RGB; no
  PNG/NEO/XIMG frame path is claimed here.

### Next

1. Measure multi-run hash/stability and record the honest fidelity label.
2. Decide whether the P2.3 frame-lock patch is worth pursuing; otherwise keep
   the measured fallback and its replay impact.
3. Add AVI/audio, screen-mode and best-effort AHCC GUI coverage with explicit
   skips for missing ROMs/build features.
4. Keep `doc/tools-reference.md` and the determinism caveats in sync.

## Adversarial review

The requested independent review via the Task tool with `subagent_type:
"general"` could not be performed: the Task tool is not exposed in this session
(only shell, file, search, web, and Firecrawl tools are available). This is a
recorded process gap. A structured adversarial self-review was performed against
the roadmap and Hatari source; findings were incorporated below.

| Finding | Severity | Adjustment |
| --- | --- | --- |
| `_vbl` is not a builtin; `VBL` is the counter and `$70` is the TOS vector. | High | Frame clock uses `VBL` and `($70)`; tests both; docs corrected. |
| VBL breakpoint arming races the emulation clock; `n=1` can miss. | High | `+2` margin, bounded re-arm, return actual VBL, never hang. |
| A screenshot at a VBL stop is not necessarily the final rendered frame. | High | Contract is "pixels at the stop point"; two-run hash test; no stronger claim. |
| The debugger pager reads stdin, so disasm/capture helpers can deadlock. | High | P0 pager guard; fail fast; cap outputs. |
| `--disable-video` breaks screenshots. | High | Never used; capture tests assert. |
| P2.3 frame stepping is `[P]`; the fallback is not exact. | High | Stop/go fallback marked `frame_locked:false`; patch optional. |
| Absolute mouse is only a corner-pin heuristic. | Medium | Probe/verify + warnings; P7 `[P]`. |
| Joystick has no direct event in stock Hatari. | Medium | P1 keyboard-emulation path only; no direct claim. |
| AVI codec/fps/file are fixed when recording starts. | Medium | Launch-time config; stop/restart; typed error. |
| `--sound off` makes `recsound` a no-op. | Medium | `restart_required` with sound args. |
| `recsound` has no per-recording path argument. | Medium | `soundout` before toggle; path confinement. |
| Pillow version differences can change decode output. | Medium | Pin Pillow; canonical RGBA; hash metadata. |
| Large RGBA returns can blow the model context. | Medium | Inline cap; artifact/resource; region caps. |
| AHCC GUI is a mode switch, not a second instance; GUI timing is fragile. | Medium | P1 decision; pixel anchors; bounded retries; CLI independent. |
| Burst/AVI tests can exhaust disk. | Medium | Retention/caps/cleanup and explicit limits. |
| TT/Falcon modes need ROMs not available in CI. | Low | Skip cleanly; ST/STE matrix mandatory. |
| `wait_for_screen_change` is polling and not frame-exact. | Low | Bounded polling, sample metadata, no determinism claim. |

No roadmap item was dropped. P2.11's direct joystick axes/buttons remain a P7
`[P]` item and are not implemented in P2; P2 keeps the keyboard-emulation path
documented and tested.

### Frames/scripting implementation review (2026-10-27)

The Task tool is still not exposed in this session, so no independent reviewer
could be spawned. The following is a structured self-review of the new
`tools/frames.py` and `tools/scripting.py`.

| Finding | Severity | Adjustment / status |
| --- | --- | --- |
| `server.py` is not registered by the implementation because it is outside the owned file set. | High | `frames.register` and `scripting.register` exist and were smoke-tested against a real `MCPServer`; core wiring is still required. |
| `app.guard_path` only resolves paths and has no allowed-root check, so script save/load can read or write arbitrary host files. | High | Used `guard_path` as instructed; this remains a core path-policy gap to fix in `app.py`. |
| Pixel tools decode only uncompressed 24/32-bit BMP. PNG capture is file-only. | Medium | Default is BMP; PNG returns a note and no fabricated dimensions. |
| `screen_wait_stable` is screenshot polling, not VBL-locked. | Medium | Result sets `vbl_locked: false` and explains that equal captures do not prove idleness. |
| Input execution is host wall-clock timed. | Medium | Results set `host_timed: true` and `frame_locked: false`. |
| Absolute mouse positioning is unavailable in stock Hatari. | Medium | `input_mouse_click` rejects x/y with `unsupported`; it never invents a position. |
| Diff images under `/tmp` have no retention cleanup. | Low | Each result returns the path and hash; callers/tests must remove them. Burst retention is separate work. |
| 32-bit BI_RGB BMPs with all-zero alpha are made opaque. | Low | Documented decoder rule in the module; preserved nonzero alpha. |
| Single-letter key mappings are inherited from P1 `input.py`. | Medium | Consistency is intentional; the P1 table needs independent verification for letters outside the tested subset. |
