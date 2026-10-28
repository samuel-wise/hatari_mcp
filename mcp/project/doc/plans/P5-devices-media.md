# P5 — Devices and media

Status: partial — floppy/printer/audio/video and option plumbing implemented; HD/cartridge hot-swap and live stream/media research paths remain
Owner: phase implementer (devices/media)
Last updated: 2026-10-27
Roadmap: P5.1–P5.12

## Goal

Cover the hardware surface that tests, demos, and real programs need:
floppies, hard disks (GEMDOS/ACSI/SCSI/IDE), cartridge, printer, RS232/MIDI/
SCC streams, audio, video modes, CPU/chipset options, config persistence, and
safe media hot-swap.

This phase is deliberately conservative. It uses only mechanisms documented in
`tools-reference.md` and `architecture.md` (plus the generic `emu_set_option`
escape hatch for documented Hatari command-line options). Any capability with
no documented mechanism is marked `[P]` (needs a Hatari patch) or `[R]`
(research/build-dependent) and returns a typed `unsupported` result. It is not
silently emulated, faked, or hidden behind a restart.

Gate (roadmap): a graphical demo requiring a floppy/HD and audio can be
launched, captured, and validated headlessly.

## Dependencies

| Dependency | Why |
| --- | --- |
| P0.2, P0.3, P0.4 | Supervisor lifecycle, socket/stdin channel, demuxer. |
| P1.2 | Typed configuration and restart-required detection. |
| P1.3 | GEMDOS host directory and drive-letter mapping. |
| P1.6 | Determinism basics (fixed RTC, fast boot, timer-D). |
| P1.20 | State save/load around restarts and hot-swap. |
| P1.21, P1.25–P1.27 | Screen capture, path confinement, destructive gates, limits. |
| P2.4, P2.14 | Pixel/capture validation across video modes. |
| P3.1, P3.8 | Workspace/drive mapping and image builders used by media setup. |
| P3.2, P3.10 | `run_prg` and ST/STE/TT/Falcon capability profiles. |
| P4.5 (soft) | CPU/MMU/cache readback for hardware validation. |
| P4.12 (soft) | Crash context when a device test fails. |

External prerequisites: EmuTOS (CI) or user TOS, generated blank floppy/HD
images, host `mtools`/`mkdosfs`/`sfdisk` for P3.8, optional capslib for STX/IPF,
and optional real device nodes for interactive (non-deterministic) stream tests.
No proprietary ROMs or disk images are committed.

## Deliverables and file ownership

All implementation paths are under `mcp/project/src/hatari_mcp/`.

| Roadmap | Deliverable | Primary path | Owner role |
| --- | --- | --- | --- |
| P5.1 | Floppy insert/eject/protect/heads/fast FDC | `media/floppy.py` | media implementer |
| P5.2 | GEMDOS/ACSI/SCSI/IDE configuration | `media/hd.py` | media implementer |
| P5.3 | Cartridge insert/eject | `media/cartridge.py` | media implementer |
| P5.4 | Printer output and reset | `devices/printer.py` | device implementer |
| P5.5 | RS232 file/device streams | `devices/serial.py` | device implementer |
| P5.6 | MIDI file/device streams | `devices/midi.py` | device implementer |
| P5.7 | SCC/LAN streams | `devices/scc.py` | device implementer |
| P5.8 | Sound options and WAV/AVI audio | `devices/audio.py` | audio implementer |
| P5.9 | Video modes and capture validation | `devices/video.py` | video implementer |
| P5.10 | CPU/chipset/hardware options | `devices/hardware.py` | hardware implementer |
| P5.11 | Config read/write/diff | `config_store.py` | config implementer |
| P5.12 | Hot-swap transaction/validation | `media/hotswap.py` | media implementer |
| — | Option catalog with restart/machine metadata | extend `config.py` (P1 restart table; split only if it outgrows one module) | config implementer |
| — | Tool registration/schemas | extend `tools/config.py` (P1 `emu_set_option`/`emu_device`/`emu_set_path`); add `tools/media.py`, `tools/audio.py`, `tools/video.py`; use `tools/devices.py` only for tools not covered by config | tool implementer |

Supporting test ownership:

* `tests/unit/test_floppy.py`, `test_hd.py`, `test_cartridge.py`,
  `test_printer.py`, `test_streams.py`, `test_audio.py`, `test_video.py`,
  `test_hardware.py`, `test_config_store.py`, `test_hotswap.py`.
* `tests/integration/test_media_boot.py`, `test_printer_serial_midi.py`,
  `test_audio_video.py`, `test_hardware_matrix.py`,
  `test_config_persistence.py`, `test_hotswap_validation.py`.
* Fixtures: generated blank 720 KiB/1.44 MiB floppies, 8/16 MiB HD images,
  cartridge image (synthetic/test-licensed), printer/RS232/MIDI/SCC text
  streams, WAV expected header, and video-mode screenshots.

## Design / constraints

### Mechanism allowlist

Use these documented mechanisms:

| Need | Mechanism | Documented in |
| --- | --- | --- |
| Runtime/launch option | `emu_set_option` → `hatari-option <args>` | tools-reference |
| Device toggle | `emu_device` → `hatari-enable/disable/toggle <device>` | tools-reference |
| Path changes | `emu_set_path` → `hatari-path <name> <path>` | tools-reference |
| Floppy/HD/cartridge | `media_*` tools | tools-reference |
| Shortcut recording | `emu_shortcut` (`recsound`, `recanim`) | tools-reference/architecture |
| Screen metadata/capture | `screen_info`, `screen_screenshot` | tools-reference |
| State | `state_save`, `state_load` | tools-reference |
| Config save | `emu_save_config` (**research needed**, see P5.11) | tools-reference |
| Logs/console | `log_read`, `console_read` | tools-reference |
| Escape hatch | `dbg_command` (privileged) | tools-reference |

Actual Hatari option names are verified against
`build-install/bin/hatari --help`; do not use roadmap shorthand where it differs
(e.g. `--cpulevel`, not `--cpu-level`; `--fastfdc`, not `--fastfloppy`).

Anything outside this table, or a feature that requires a runtime change Hatari
does not accept, is `[R]` (research/build-dependent) or `[P]` (patch).

### P5.1 Floppies

* Insert A/B: `media_insert_floppy` → `hatari-option --disk-a/--disk-b <path>`.
* Eject A/B: `media_eject_floppy` → `--disk-a none` / `--disk-b none`
  (documented; do not pass an empty string).
* Write protect: `--protect-floppy on|off|auto`.
* Geometry: `--drive-a-heads 1|2`, `--drive-b-heads 1|2`; drive enable/disable
  via `--drive-a/--drive-b`.
* Fast FDC: `--fastfdc on|off` (can break timing-sensitive software; expose
  with a warning, not as a default).
* Formats: `.st` and `.msa` are the contract. STX/IPF depend on a capslib build
  and are **`[R]`/build-dependent**; probe capability and return
  `unsupported_format` rather than attempting a best-effort mount.
* Validate extension, size, geometry, readability, and write-protect intent;
  never mount a missing/empty path. Runtime application is verified; if Hatari
  rejects it, mark restart-required.

### P5.2 Hard disks

Targets:

| Target | Mechanism | Notes |
| --- | --- | --- |
| GEMDOS host dir | `--harddrive <dir>`, `--gemdos-drive <C-Z|skip>` | Only one GEMDOS directory (see P3.1); `--protect-hd`, `--gemdos-case/time/conv`. |
| ACSI | `--acsi <id>=<file>` (0–7), `--acsi-ver <id>=<1|2>` | Machine/version dependent; validate. |
| SCSI | `--scsi <id>=<file>` (0–7), `--scsi-ver <id>=<1|2>` | TT/Falcon dependent; validate. |
| IDE | `--ide-master <file>`, `--ide-slave <file>`, `--ide-swap <id>=<off|on|auto>` | Falcon/IDE-capable machines. |
| Write protect | `--protect-hd on|off|auto` | For GEMDOS/HD images where supported. |

`media_set_hd(target, path, options)` validates the target/machine combination
before sending. Mounting an HD image generally requires restart; runtime
attempts are allowed only if Hatari accepts the option, and the result states
which path was taken. Missing or unreadable images are a typed error, never a
silent "continue with no disk".

### P5.3 Cartridge

* Insert: `media_insert_cart` → `--cartridge <file>`.
* Eject with no cartridge and no restart is not documented. Return
  `restart_required` (or `[R]` if a documented runtime toggle is found); do not
  invent an empty-path option.
* Validate size/readability and machine support (cartridges execute at boot).
  Mark the tool destructive/privileged: a cartridge is guest code executed
  before the OS. Runtime insert is restart-dependent; hot-swap without restart
  is `[R]`.

### P5.4 Printer

* Enable/disable: `emu_device printer enable|disable|toggle`.
* Output path: `emu_set_path printout <file>` (`--printer <file>` at launch).
* Capture: host-side read of the configured file with a size/tail cap.
* Reset: rotate/truncate the output file with explicit confirmation; this does
  **not** reset the guest's printer state or the emulated printer device. A
  device-node or stdout destination has no reliable readback; report
  `readback_unavailable` instead of reading a device.
* No printer job boundary is exposed by Hatari; return byte counts and raw
  output, not invented page/job metadata.

### P5.5–P5.7 RS232, MIDI, SCC/LAN streams

* File-backed streams at launch:
  `--rs232-in/out`, `--midi-in/out`, `--scc-a-in/out`,
  `--scc-a-lan-in/out`, `--scc-b-in/out`.
* Runtime path changes via `emu_set_path` names documented in
  tools-reference: `rs232in/out`, `midiin/out`, `scca*`, `sccalan*`, `sccb*`.
* Device toggles: `emu_device rs232|midi|scca|sccalan|sccb`.
* Determinism: file-backed streams only. Real device nodes are interactive and
  non-deterministic; allow them only behind an explicit flag, label the result
  `non_deterministic`, and never use them in CI.
* There is no documented per-event injection or socket-backed stream in v1.
  Live/socket-backed streams are `[P]` (Phase 7). A FIFO path may appear to
  work but blocks/changes semantics; treat it as `[R]`, not a supported
  deterministic stream.
* Output readback: host file tail; input replacement while the file is open is
  not guaranteed to take effect. Prefer restarting with the new input file and
  state that explicitly.
* SCC/LAN availability is machine-dependent (TT/Falcon); validate and return
  `machine_unsupported` rather than silently ignoring.

### P5.8 Audio

* Options: `--sound off|6000-50066`, `--sound-buffer-size 0|10-100`,
  `--sound-sync on|off`, `--ym-mixing linear|table|model`; `--mic on|off`
  (Falcon only); `--dsp none|dummy|emu` (Falcon only, coordinated with P5.10).
* Output path: `emu_set_path soundout <file>` (set before recording).
* Recording: `emu_shortcut recsound` toggles recording to the configured
  capture file; the format is determined by the file extension
  (architecture table). Use `.wav` for the supported WAV path. There is no
  per-recording path argument; changing the path may require stop/restart.
* AVI audio: use the P2.8 `recanim`/AVI path; do not invent a separate audio
  track control unless a documented option exists.
* Validate the artifact: exists, non-zero, RIFF/WAVE header for `.wav`,
  expected duration tolerance, and a silence check. A zero-length or silent
  file is a failure, not a pass.
* Audio timing is not bit-exact across hosts; `--sound off` remains the default
  for deterministic screenshot tests. Recording tests assert structure and
  duration, not sample-exact bytes.

### P5.9 Video

Use `screen_info` as the source of truth and `screen_screenshot`/P2.4 for
capture. Options through `emu_set_option`:

* monitor: `--monitor mono|rgb|vga|tv`;
* TOS resolution: `--tos-res {st,tt,tc}{low,med,high}`;
* borders/overscan: `--borders on|off`;
* STe palette: `--spec512 0-512`;
* timing: `--video-timing ws1|ws2|ws3|ws4|random`;
* VDI: `--vdi on|off`, `--vdi-planes 1|2|4|8`,
  `--vdi-width 320-2048`, `--vdi-height 160-1280`;
* scaling/capture: `--force-max`, `--aspect`, `--crop`, `--zoom 1.0-8.0`,
  `--frameskips 0-64`, `--vsync`;
* Videl/Falcon modes are profile/machine-dependent; TT high-res and ST mono are
  separate matrix cases.

Most monitor/VDI/timing changes are restart-required or only partially applied
at runtime. Detect via typed restart metadata and verify with `screen_info` and
a capture. Never use `--disable-video` when a screenshot/framebuffer is needed;
return a configuration error instead. Unsupported mode/option combinations get
`mode_unsupported`, not a silently different resolution.

### P5.10 Hardware options

Typed `emu_hardware` with machine validation:

| Option | Values | Constraints |
| --- | --- | --- |
| `--machine` | st/megast/ste/megaste/tt/falcon | chipset changes require restart |
| `--cpulevel` | 680x0 | EmuTOS/TOS 2.06+ only, per Hatari help; validate |
| `--cpuclock` | 8/16/32 | machine/CPU dependent |
| `--compatible` | on/off | prefetch compatibility |
| `--data-cache` | on/off | ≥030 only |
| `--cpu-exact` | on/off | timing/performance trade-off |
| `--addr24` | on/off | 24-bit addressing |
| `--fpu` | none/68881/68882/internal | machine/CPU dependent; `--fpu-softfloat` optional |
| `--mmu` | on/off | TT/Falcon/030-class; P4.5 readback |
| `--blitter` | on/off | Hatari help says ST only; verify STE behavior in test |
| `--dsp` | none/dummy/emu | Falcon only |
| `--rtc-year` | 0/1980-2079 | determinism; 0 means host RTC |
| `--timer-d` | on/off | timing/determinism; default documented per profile |

Most of these require restart. The server rejects impossible combinations
before launch/restart, records the effective options, and reads back what the
debugger can expose (`info mmu`, `cpureg`, P4.5). It does not claim to query
options Hatari does not expose.

### P5.11 Config persistence (scope correction)

`tools-reference.md` says `emu_save_config(path)` maps to
`hatari-option --saveconfig`. The actual option is `--saveconfig` with **no
path argument**, and Hatari's help says "Save current Hatari configuration and
exit". Therefore:

* do **not** call `--saveconfig` on the live session: it may terminate the
  emulator and cannot target a caller-supplied path;
* run an `[R]` spike to determine whether
  `hatari --configfile <path> --saveconfig <other args>` writes the named file
  and exits. If yes, implement saving in a **dedicated short-lived Hatari
  process** with the same launch options, never the live session. If no,
  implement a host-side `.cfg` writer for the subset the server controls, or
  return `config_save_unsupported`; do not fake success;
* `emu_get_config` remains the merged supervisor/launch config (not a complete
  runtime dump);
* `emu_config_read(path)` parses a `.cfg` on the host, read-only and confined;
* `emu_config_diff(path)` compares the file with known launch options and
  observable `info` values, and labels unknown/opaque keys;
* `--configfile` is a launch-time input; precedence is
  explicit tool args > profile > `.cfg` > built-in defaults, as documented;
* config files may contain paths; do not leak secrets, and keep writes
  confined.

### P5.12 Hot-swap and validation

Every media/device change is a transaction:

1. **preflight** — path exists/readable, correct extension/format, size and
   geometry, machine support, write-protect intent, confinement, checksum, and
   whether the device is currently in use;
2. **apply** — prefer the documented runtime option; if Hatari rejects it or
   the option is launch-only, require `allow_restart`; if not allowed, return
   `restart_required` with the exact option;
3. **verify** — re-query `media_list`/`info` and the device state; a successful
   command is not proof of a mounted image;
4. **rollback** — restore the previous path/option and verify; if rollback
   fails, stop the session with `media_swap_failed` rather than continuing with
   an unknown device state;
5. **never silently continue** with a missing/unreadable image, a wrong machine,
   or a failed mount. Eject is explicit; an empty path is not an eject.

A restart may be required for HD, cartridge, machine, audio/video, and many
hardware options. State save/load is optional and may not capture mounted
media; after restart the caller must re-apply media explicitly. Hot-swap is
serialized by a run lock; swapping during a run requires an explicit flag and
is rejected otherwise (`busy`).

## Task checklist

### P5.1–P5.3 media

- [ ] P5.1 Implement floppy insert/eject/protect/heads/fast-FDC validation and
      restart handling; probe STX/IPF as `[R]`.
- [ ] P5.2 Implement GEMDOS/ACSI/SCSI/IDE target models with machine checks,
      versions, byte-swap, and write protect.
- [ ] P5.3 Implement cartridge insert and explicit restart-based eject; mark
      hot-swap `[R]`.
- [ ] Add `media_list` verification for all mounts and a typed
      `media_unavailable` path.

### P5.4–P5.7 streams

- [ ] P5.4 Implement printer enable/path/read/reset with device-node
      limitations.
- [ ] P5.5 Implement RS232 file streams and readback; mark live/socket `[P]`.
- [ ] P5.6 Implement MIDI file streams and readback; mark host devices
      non-deterministic.
- [ ] P5.7 Implement SCC/LAN file streams with machine checks; mark live LAN
      `[P]`.
- [ ] Add tests for missing files, wrong machine, and restart-required paths.

### P5.8–P5.10 audio/video/hardware

- [ ] P5.8 Implement sound options, `soundout` + `recsound`, WAV validation,
      and AVI audio coordination.
- [ ] P5.9 Implement the video option matrix, `screen_info` verification, and
      capture-based mode tests.
- [ ] P5.10 Implement typed hardware options with machine/TOS validation and
      restart metadata; read back what P4.5 exposes.
- [ ] Add a machine/capability table shared by media, audio, video, and
      hardware tools.

### P5.11–P5.12 config/hot-swap

- [ ] P5.11 Spike `--saveconfig`; never call it on the live session. Implement
      dedicated-process or host-side save, read, and diff.
- [ ] P5.12 Implement the preflight/apply/verify/rollback transaction, run
      lock, and `restart_required`/`busy` errors.
- [ ] Add failure-injection tests for missing image, bad geometry, wrong
      machine, failed mount, and failed rollback.

### Docs/tests

- [ ] Update `tools-reference.md` with supported/unsupported device paths and
      the config-save correction.
- [ ] Update `architecture.md` with restart-required behavior and stream
      determinism.
- [ ] Add examples for floppy/HD boot, printer/RS232/MIDI/SCC, audio recording,
      video modes, and hot-swap failure.

## Acceptance criteria

1. A generated blank floppy can be inserted, booted, ejected, and re-inserted;
   write protection and geometry are validated; a missing image never mounts.
2. GEMDOS, ACSI, SCSI, and IDE targets are configured only on supporting
   machines; versions/byte-swap/write-protect are reflected in results; HD
   changes report restart requirements honestly.
3. Cartridge insert works; eject is explicit and restart-based or returns
   `unsupported`/`[R]`; cartridge execution is marked privileged.
4. Printer output is captured to a confined file; reset rotates/truncates the
   file and does not claim to reset guest printer state; device-node readback
   is refused.
5. RS232/MIDI/SCC file-backed streams work and are read back; real devices are
   marked non-deterministic; live/socket injection is `[P]`.
6. Audio can be enabled/disabled with the documented options; `recsound`
   produces a valid non-silent WAV when recording is requested; AVI audio is
   delegated to P2.8; no per-recording path is invented.
7. Each video mode in the matrix is applied, verified by `screen_info`, and
   captured; unsupported combinations return `mode_unsupported`.
8. Hardware options validate machine/TOS/CPU combinations and restart needs;
   DSP/mic/blitter constraints are explicit.
9. Config read/diff work on a `.cfg`; save is implemented via the researched
   safe path or returns a typed unsupported result; no live `--saveconfig` that
   can kill the session.
10. Hot-swap validates before applying, verifies after, rolls back on failure,
    and never silently continues with a missing/wrong image; run locks prevent
    concurrent swaps.
11. Every unsupported path returns a typed error with `[P]`/`[R]` and a hint;
    no silent fallback to a different device or resolution.
12. Lint/type checks and tests pass without proprietary ROMs or committed
    images.

## Test plan

### Unit

* Floppy: option builders, extension/size/geometry validation, eject `none`,
  write-protect, STX/IPF capability gate.
* HD: target/machine matrix, ACSI/SCSI IDs, IDE master/slave, byte-swap,
  version, write-protect, missing/unreadable image.
* Cartridge: insert, restart/eject policy, machine support.
* Printer: path confinement, enable toggle, tail cap, reset rotation, device
  node refusal.
* Streams: path/restart policy, machine checks, deterministic vs real-device
  labels, missing-file errors.
* Audio: option builders, WAV header/duration/silence validator, fixed
  recording path, Falcon-only mic/DSP.
* Video: mode/option matrix, restart metadata, `screen_info` parsing,
  unsupported combinations.
* Hardware: CPU/machine/TOS validation, FPU/MMU/cache/blitter/DSP constraints,
  restart metadata.
* Config: `.cfg` parser, precedence, diff labeling, safe-save refusal.
* Hot-swap: transaction states, rollback, `busy`, `restart_required`,
  checksum and missing-image failures.

### Integration (real Hatari + EmuTOS; optional user TOS)

* Boot from a generated A: floppy and a generated HD; insert/eject/protect;
  verify `media_list`.
* ACSI/SCSI/IDE cases per machine; byte-swap/version where applicable; skip
  unsupported machine combos with a reason.
* Cartridge insert and restart-based eject; wrong-machine skip/error.
* Printer: guest writes a test page; capture output; reset rotates; device
  node readback refused.
* RS232/MIDI/SCC: file-backed guest output appears; input file replacement
  behavior verified; real-device path marked non-deterministic.
* Audio: `soundout` + `recsound` produces a WAV with expected duration and
  non-silence; `--sound off` remains the deterministic default.
* Video: ST mono/color, STe overscan/spec512, TT high-res, Falcon Videl/VDI;
  capture and compare dimensions/hashes within documented tolerance.
* Hardware: CPU level/clock, FPU/MMU/cache, blitter, DSP, RTC, timer-D; verify
  restart and readback; skip unsupported combinations.
* Config: read a `.cfg`, diff against launch options, save through the
  researched safe path; verify no live-session exit.
* Hot-swap: missing/wrong/corrupt image, failed mount, rollback success and
  failure, swap during run, restart-required refusal.

### Corpus / external

* Okami-Shell from a staged floppy/HD copy; screenshot and exit behavior.
* GodLib/FreeMiNT demo samples if P3 corpus builds exist; otherwise skip with
  the corpus reason.
* Audio/video demo artifacts only when the corpus provides a licence-compatible
  sample; never copy media into the repository.

### CI

* EmuTOS only; generated images only; no proprietary TOS or disk images.
* Optional capslib job for STX/IPF; default job asserts graceful
  `unsupported_format`.
* Audio/video tests use a dummy SDL audio driver where possible; skip with a
  clear reason if recording is unavailable.
* Assert no orphan processes, no leaked sockets, and no writes outside the
  artifact root.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| `--saveconfig` takes no path and exits Hatari (help text). | `emu_save_config` as documented could kill the live session or fail to write the requested file. | P5.11 research spike; dedicated short-lived process or host writer; never live-call; typed unsupported fallback. |
| Many device/hardware options are launch-only. | Runtime `hatari-option` may be rejected or partially applied. | Option catalog with restart metadata; verify state; `restart_required` instead of pretending. |
| No documented cartridge eject. | P5.3 eject cannot be hot-swapped. | Restart with no cartridge; mark hot-swap `[R]`; typed error if restart not allowed. |
| HD/cartridge hot-swap may lose state. | Restart destroys guest state; snapshots may not include media. | Explicit `allow_restart`, optional state save, re-apply media, document state loss. |
| STX/IPF depends on capslib. | Format support varies by build. | Capability probe; `unsupported_format`; optional CI job. |
| Printer reset is only file rotation. | Guest printer state is not reset. | Document; separate device toggle; do not claim a guest reset. |
| Streams are file-backed only; live injection is absent. | Tests cannot interact in real time. | Deterministic files; real devices marked non-deterministic; socket/live is `[P]` Phase 7. |
| FIFOs/device nodes can block. | Tool hangs. | Reject/flag unsupported paths; timeouts; no CI use. |
| Audio recording path/format is fixed by config. | Cannot choose a path per recording. | Set `soundout` before starting; document restart; validate extension. |
| Audio/video timing varies by host. | Flaky exact-byte or exact-frame tests. | Structural/duration checks; documented tolerances; `--sound off` for determinism. |
| Video mode changes may silently fall back. | Test validates the wrong mode. | Verify `screen_info` and capture dimensions; `mode_unsupported` on mismatch. |
| Machine/CPU option combinations are constrained. | Options may be ignored or rejected. | Shared capability table; validate before launch; skip unsupported. |
| Config diff cannot reflect all runtime state. | Overstated "diff against running state". | Label known/unknown keys; only claim what supervisor + `info` expose. |
| Device paths can overwrite host files. | Destructive side effects. | Path confinement, explicit write targets, destructive annotations, no device nodes by default. |
| Real hardware/ROM differences. | Tests pass on EmuTOS but fail on TOS. | Separate EmuTOS CI from optional user-TOS matrix; record ROM prerequisites. |

## Work log

### Done

* 2026-10-27 — phase implementer: read roadmap P5, architecture,
  tools-reference, machine profiles, and Hatari `--help`. Verified real option
  names and constraints, the single GEMDOS-directory limitation, and that
  `--saveconfig` takes no path and exits Hatari.
* 2026-10-27 — phase implementer: wrote this plan; no code or non-plan files
  were created.
* 2026-10-27 — devices/media implementer: created `tools/media.py`,
  `tools/devices.py`, and `tests/unit/test_media_options.py`. Verified against
  `src/options.c`, `src/floppy.c`, `src/control.c`, `src/shortcut.c`,
  `src/sound.c`, `src/avi_record.c`, and `src/debug/debugInfo.c`:
  `--disk-a/--disk-b`, `--acsi/--scsi <id>=<file>`, `--ide-master/--ide-slave`,
  `--cartridge`, `--avi-file/--avi-vcodec/--avi-fps`, and the actual
  `hatari-path` names (`midiin/midiout`, `scca*`, `sccalan*`, `sccb*`).
  `none` disables ACSI/SCSI/IDE images, but `--cartridge none` is rejected
  (CHECK_FILE with no enable flag). `szYMCaptureFileName` has no CLI option;
  `hatari-path soundout` is the runtime mechanism.
* 2026-10-27 — devices/media implementer: runtime-tested with EmuTOS:
  floppy insert/eject, printer toggle, printout/soundout paths, `recsound` WAV
  capture (with `--sound 44100`), and `recanim` BMP AVI capture. Verified
  non-zero growing artifacts. HD/cartridge tools return `restart_required`
  with the exact option. New media/device unit tests: 18 passed; full project
  suite: 165 passed. `ruff` is not installed in the project venv, so lint could
  not be run; `compileall`/AST parse passed.
* 2026-10-27 — devices/media implementer: found a core runtime-config defect:
  `socketdir.CONFIG_TEXT` sets `nAlertDlgLogLevel = 4` (LOG_TODO), so Hatari
  opens modal SDL dialogs for LOG_INFO/LOG_WARN messages. This blocks
  `recsound`/`recanim` and can block floppy insertion. Media tools defensively
  send `hatari-option --alert-level fatal`; the core config should use
  LOG_ERROR/LOG_FATAL. `server.py` still needs to register `tools.media` and
  `tools.devices` (not edited by this implementer).

* 2026-10-27, reconciliation: `tools/media.py` and `tools/devices.py` are
  registered and the implemented subset is runtime-tested with EmuTOS:
  floppy insert/eject, printer, WAV/AVI capture and option plumbing. HD and
  cartridge changes return `restart_required`; audio/video toggles are
  available.

### Remaining / next steps

* No stock hot-swap for HD or cartridge: both are restart-only, so P5.12's
  transactional hot-swap remains `[R]/[P]`.
* Live RS232/MIDI/SCC streams require an upstream/patched mechanism and remain
  `[P]`; STX/IPF support remains `[R]` and blocked on capslib.
* `--saveconfig` takes no path and exits Hatari, so P5.11 is limited to
  read/diff; a dedicated-process save path is still unproven.
* P3 image builders and the P2 capture-mode matrix are still needed for full
  media validation; Atari-mint corpus toolchains are absent.
* Audio/video capture depends on the local SDL/libpng/sound build and real
  sound hardware is not available for interactive stream tests.

### Next

1. Keep restart-only HD/cartridge behavior explicit; do not call it hot-swap.
2. Finish the `--saveconfig` dedicated-process spike and the video-mode matrix
   with clean skips for absent TT/Falcon ROMs.
3. Add image-builder and capture-matrix coverage when P2/P3 support lands.

## Adversarial review

**Review method:** the requested `Task` tool (`subagent_type: general`) is not
available in this environment, so an independent subagent review could not be
run. The implementer performed the adversarial self-review below, including
runtime probes against EmuTOS. It should still be replaced by an independent
review before the phase is accepted.

| Finding | Why it matters | Incorporated as |
| --- | --- | --- |
| `--saveconfig` has no path argument and its help says it saves and exits. | The documented `emu_save_config(path)` mapping could kill the live emulator and still not write the requested file. | P5.11 explicitly forbids live use; research dedicated process or host writer; typed unsupported fallback. |
| The P1 plan currently promises a path-confined `emu_save_config(path)`. | Implementation would inherit an unsafe/incorrect contract. | P5.11 records the correction and requires P1/P5 to update the tool schema and docs before implementation. |
| Stock Hatari has one GEMDOS host directory. | P5.2 cannot provide multiple GEMDOS mounts. | Reuses P3.1's single-rw-root model; other mounts are images/staged copies; direct multi-mount `[P]`. |
| Cartridge eject has no documented runtime mechanism. | P5.3 "insert/eject" would otherwise be overstated. | Insert supported; eject requires restart; hot-swap `[R]`; explicit error. |
| Many options are launch-only despite `emu_set_option`. | Runtime changes may be rejected or partially applied. | Restart metadata, verification, and `restart_required` errors. |
| STX/IPF needs capslib. | A stock build cannot mount them. | Capability probe; `unsupported_format`; optional CI branch. |
| Printer reset is not a guest reset. | The tool could claim more than it does. | Reset rotates the output file only; documented limitation. |
| RS232/MIDI/SCC are file-backed, not live injection. | Interactive tests would be impossible. | Deterministic file streams; real devices non-deterministic; socket/live `[P]`. |
| `recsound` uses the configured capture path/extension. | Per-recording path APIs would be false. | Set `soundout` first; no per-recording path; WAV validation. |
| Video options can silently fall back. | A test could validate the wrong mode. | Verify `screen_info`/capture and fail `mode_unsupported`. |
| Hot-swap can destroy state and snapshots may not include media. | A restart could lose the guest and mounted images. | Transaction, explicit restart, re-apply media, typed state-loss warning. |
| Real device nodes/FIFOs can block or write host files. | Hangs and host side effects. | Reject/flag; confinement; timeouts; no CI use. |
| Full config diff is not observable. | "Diff against running state" is too strong. | Diff only known launch options + `info`; label unknown keys. |
| Generated runtime config sets alert dialogs to LOG_TODO. | `recsound`, `recanim`, and some floppy warnings open blocking modal dialogs in the headless session. | Media tools send `--alert-level fatal`; report that core `socketdir.py` should lower the alert level. |
| HD changes require a cold reset and Hatari may prompt through `DlgAlert_Query`. | Sending `--acsi/--scsi/--ide-*` to a live headless session can block in a reset dialog. | `media_mount_hd`/`media_unmount_hd` validate and return `restart_required` + the exact option instead of applying live. |
| `--cartridge none` is rejected. | An invented eject command would fail or be misleading. | `media_eject_cartridge` returns `restart_required` and explains that the option uses CHECK_FILE with no enable pointer. |
| No CLI option exists for `szYMCaptureFileName`. | `media_set_audio_capture` cannot be implemented as a launch option. | Uses `hatari-path soundout`, validates `.wav`/`.ym`, and reports the limitation. |
| `recsound`/`recanim` are toggles with no state query. | A blind stop could start recording, and a blind start could stop it. | Recording state is tracked per supervisor generation; stop refuses when state is unknown. |
| A fully zeroed `.st` image triggers Hatari's Pacifist/Makedisk modal. | Live floppy insertion can hang before any command returns. | `.st` boot-sector preflight rejects zeroed/non-executable sectors; alert-level workaround also applies. |
| `hatari-option` splits on spaces and treats backslash as an escape. | Unescaped image paths would be split into multiple options. | Only whitespace is backslash-escaped; literal backslashes are preserved. |
| AVI codec/fps are read at recording start. | Changing them mid-recording would be a false promise. | Tool rejects a tracked active recording and sets options before `recanim`; results include launch args. |
