# MCP tool reference

Status: current implementation. The server registers **102 tools**; this page
was generated from the live MCP tool registry (`server._tool_manager`) and
grouped by the registration module. Read `architecture.md` for the
transport and process model.

## Conventions

* Names are `snake_case`, grouped by prefix (`emu_`, `log_`, `console_`, `dbg_`,
  `screen_`, `input_`, `media_`, `ahcc_`, `scenario_`) plus the standalone
  `run_prg` and `workspace_info`. Each subsection names its registration module.
* Arguments with a default are optional; `?` marks a nullable argument.
  Address arguments accept an integer, `0x`/`$` hex, or a debugger expression
  where the tool passes the value through as text.
* Successful calls return `{ "ok": true, ... }`. Guarded failures become MCP
  tool errors whose text is `code: message. Hint: ...`; `extra`, `raw` and
  `detail` are not forwarded to MCP clients and are only available on
  `HatariError` for direct Python callers.
* `raw` and/or `lines` are included whenever a debugger command's text is the
  only source of truth. Treat parsed fields as best-effort.
* General file arguments are resolved by `app.guard_path` (expand + realpath).
  Stricter confinement applies only where stated: scenarios, `run_prg`, AHCC
  output, and the external corpus. The debugger itself can read/write host
  files, so path arguments are not a global sandbox.
* `emu_start` launches one headless Hatari child with `SDL_VIDEODRIVER=dummy`
  unless `display=true`; rendering stays enabled so screenshots work.
* NatFeats is **off by default**. `--natfeats on` lets guest code forge
  diagnostic text via `NF_STDERR`, so opt in only for cooperative programs
  that need `NF_EXIT`.
* Host-timed input and screenshot polling are not VBL/frame-locked. They can be
  accurate for ordinary UI tests but do not guarantee deterministic timing.

## Lifecycle (8)

Source: `tools/lifecycle.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `emu_start` | Start the single headless instance. | `profile?`, `args?[]`, `tos?`, `display=false`, `workspace?`, `autostart?`, `gemdos_drive="C"`, `gemdos_case="off"`, `gemdos_time="atari"`, `gemdos_conv="off"`, `write_protect="off"`, `natfeats=false` | Spawns Hatari with a private control socket and stdio pipes. Optionally maps one host directory as a GEMDOS drive and autostarts a program with `--auto` + `--symload exec`. | Returns after Hatari connects; boot may still be running. One instance per server. `autostart` requires `workspace`. NatFeats opt-in only. |
| `emu_stop` | Stop Hatari. | `force=false` | Graceful debugger quit, then SIGTERM, then SIGKILL. | Idempotent; always attempts socket/runtime cleanup. |
| `emu_status` | Report lifecycle and process state. | — | Reads supervisor state; no debugger command. | Includes pid, state, `program_exit_code`, `exit_reason`, socket, last error/exception, ring cursors, `at_prompt`, `paused`, `unresponsive`. |
| `emu_pause` | Pause emulation. | — | Sends `SIGSTOP` to the Hatari child. | Only from `running`. While paused the debugger and control socket are not serviced; call `emu_resume` first. Not valid at the debugger prompt. |
| `emu_resume` | Resume a paused instance. | — | `SIGCONT` then waits for a control-socket echo acknowledgement; at the prompt sends `cont` on stdin. | Fails with `timeout` if the emulator does not answer after `SIGCONT`. |
| `emu_wait_for_exit` | Wait for exit or timeout. | `timeout_ms=30000` | Waits on the supervisor exit event. | `NF_EXIT` is detected only when started with `natfeats=true`; Pterm/Pterm0 auto-exit is not enabled. |
| `emu_reset` | Reset the machine. | `kind="hard"` | Debugger `reset soft` for `warm`/`soft`, `reset hard` for `cold`/`hard`, on `channel=auto`. | Other values are rejected. Leaves the debugger prompt when applicable. There is no `emu_reboot`; use stop + start. |
| `emu_quit_debugger` | Leave the debugger. | — | `quit` on `channel=auto`; at the prompt this is stdin, never the queued socket. | Destructive to the debug session; the process may exit. |

## Logs and console (2)

Source: `tools/logs.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `log_read` | Read classified Hatari/debugger output. | `lines=200`, `since?`, `kind?` | Bounded stderr ring buffer; entries are classified (log, debugger, trace, NatFeats, breakpoint, etc.). | Returns `entries` and a `cursor`; pass it as `since` for incremental reads. Does not read stdout/TOS console. |
| `console_read` | Read TOS console output. | `lines=200`, `since?` | Bounded stdout ring buffer fed by `--conout 2`. | Returns `entries` and `cursor`. Output is only present when the guest writes to the TOS console. |

## Machine configuration and shortcuts (4)

Source: `tools/machine.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `emu_set_option` | Pass raw Hatari options at runtime. | `options` (one line, required) | `hatari-option <options>` over the control socket; no option-type validation. | Fire-and-forget raw pass-through to `Change_ApplyCommandline()`. Restart-only options are not rejected by this tool: Hatari may open a modal reset prompt or, after `--alert-level fatal`, may cold-reset the guest. The server process does not restart. Rejected at the debugger prompt. |
| `emu_set_path` | Set a Hatari stream path. | `name`, `path` | `hatari-path <name> <path>`. | Allowed names: `memauto`, `memsave`, `midiin`, `midiout`, `printout`, `soundout`, `rs232in`, `rs232out`, `sccain`, `sccaout`, `sccalanin`, `sccalanout`, `sccbin`, `sccbout`. `applied` is unknown; verify via the device/log. |
| `emu_device` | Enable/disable/toggle a runtime device. | `name`, `action="toggle"` | `hatari-enable/disable/toggle <device>`. | Devices: `printer`, `rs232`, `scca`, `sccalan`, `sccb`, `midi`. Fire-and-forget and not idempotent. |
| `emu_shortcut` | Invoke a UI shortcut. | `name` | `hatari-shortcut <name>`. | Allowed: `mousegrab`, `coldreset`, `warmreset`, `screenshot`, `bosskey`, `recanim`, `recsound`, `savemem`, `quit`. Destructive names reset or quit. `applied` is unknown. |

## Debugger core (12)

Source: `tools/debug.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `dbg_command` | Raw debugger escape hatch. | `command`, `channel="auto"`, `timeout_ms?` | `hatari-debug <command>`; `auto` uses stdin at the prompt and the socket while running. | Privileged: can write memory, run scripts, and control the emulator. Returns text plus parsed `lines`/`raw`; no structured schema. |
| `dbg_registers_get` | Read the M68k register file. | — | Debugger `cpureg`. | Returns `raw` and best-effort `registers` (d0-d7, a0-a7, pc, sr, ...). |
| `dbg_evaluate` | Evaluate a debugger expression. | `expression` | Debugger `evaluate <expression>`. | One line only. Returns `value`, `hex`, `dec` when parseable, plus raw text. |
| `dbg_disassemble` | Disassemble from an address. | `address`, `count=10` | Numeric addresses become `disasm $start-$end` (`count*16` bytes, capped at 200); non-numeric text is passed as a single address. | Returns the first `count` instruction lines. Hatari takes a range, not a count. |
| `dbg_symbols_load` | Load symbol tables. | `path?`, `prg=false`, `offset?` | `symbols "<path>" [offset]`, or `symbols prg` for the running GEMDOS program. | Supports ELF/a.out/DRI/GST/nm `.sym` inputs. `loaded` is a best-effort text check. |
| `dbg_symbols_list` | List loaded symbols. | `kind="code"`, `filter=""` | `symbols code\|data\|name [filter]`. | Text output plus `kind`/`filter`. No source line numbers in Hatari symbols. |
| `dbg_breakpoint_set` | Set a conditional breakpoint. | `address`, `type="exec"`, `condition?`, `once=true`, `quiet=false`, `count?` | `breakpoint <expr> [:count] [:once] [:quiet]`. `exec` maps to `pc = $address`; `mem`/`reg` require an explicit condition. | No enable/disable: delete and re-add. Returns raw output and the generated expression. |
| `dbg_breakpoint_list` | List breakpoints. | — | Debugger `breakpoint`. | Raw text; no parsed table. |
| `dbg_breakpoint_delete` | Delete a breakpoint. | `index` | `breakpoint all` for `0`, otherwise `breakpoint <1-based index>`. | Returns raw output. |
| `dbg_continue` | Continue emulation. | — | `cont` on `channel=auto`. | At the prompt this uses stdin and leaves it. No completion sentinel is expected. |
| `dbg_step` | Step one CPU instruction. | — | `step` on stdin. | Requires the debugger prompt; otherwise `not_stopped`. Stays at the prompt. |
| `dbg_step_over` | Step over one CPU instruction. | — | `next` on stdin. | Requires the debugger prompt; otherwise `not_stopped`. |

## Debugger execution helpers (5)

Source: `tools/debug_extra.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `dbg_registers_set` | Write one M68k register. | `register`, `value` | `cpureg REG=value` on stdin. | Requires the prompt. Destructive. A debugger usage error becomes `unsupported_register`/`debugger_error`. |
| `dbg_breakpoint_wait` | Wait for a breakpoint/exception/exit. | `condition`, `timeout_ms=30000` | Sets a temporary `:once` breakpoint, continues, then waits for prompt, exception, exit, or timeout. | The condition must contain a comparison. Returns stop reason, PC, exception, state, duration, lines, warnings. |
| `dbg_memory_find` | Search guest memory for bytes. | `pattern`, `start?`, `length?`, `max_hits=20` | Debugger `find` on stdin; pattern is hex text such as `48 65` or `4865`. | Requires the prompt. Returns parsed hits plus command/range; `max_hits` bounds output. |
| `dbg_memory_save` | Save a guest region to a host file. | `path`, `address`, `length` | Debugger `savebin "<path>" $addr length`. | Length is 1..512 MiB. Returns path, size, `size_matches`, SHA-256, warnings. |
| `dbg_exception_get` | Read the last supervisor exception. | — | Supervisor state only; no debugger command. | Returns `exception`, `has_exception`, `state`, `at_prompt`, `last_error`. |

## Debugger advanced: backtrace, DSP, tracing, diff (17)

Source: `tools/debug_advanced.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `dbg_backtrace` | Walk the A6 frame chain. | `max_frames=16`, `frame_pointer?` | Debugger `backtrace <n> [fp]`. | Best-effort, not a profiler reconstruction. Needs a valid frame chain; names need symbols. |
| `dbg_stack_dump` | Dump stack words. | `address?`, `words=32` | Reads A7 via `cpureg` when address is omitted, then `savebin` and big-endian 32-bit decode. | 1..4096 words. Returns addresses and hex words; bytes come from `savebin`, not `memdump` text. |
| `dbg_profile_start` | Start CPU or DSP profiling. | `cpu=true` | `profile on`, or `dspprofile on` when `cpu=false`. | Stock Hatari has no profiling-state query, so `enabled` is text-derived. DSP requires Falcon DSP emulation. |
| `dbg_profile_stop` | Stop CPU or DSP profiling. | `cpu=true` | `profile off` / `dspprofile off`. | Same query limitation; DSP requires Falcon. |
| `dbg_trace_start` | Enable trace categories. | `what`, `file?` | `trace <categories>` on stderr. | `file` is **not supported**: stock `trace` has no file argument, and `--trace-file` is launch-time only; the tool raises `trace_file_unsupported`. |
| `dbg_trace_stop` | Disable all tracing. | — | `trace none`. | No state query. |
| `dbg_history` | Read CPU/DSP PC history. | `limit=20`, `enable=false`, `processor="both"` | Optionally `history cpu\|dsp\|on <ring>`, then `history <limit>`. | History must be enabled before the event. Stock Hatari prints only entries not shown by an earlier read. |
| `dbg_command_history_clear` | Clear command history. | — | None. | **Unsupported**: stock Hatari has no command-history clear operation; returns `unsupported`. |
| `dbg_dsp_registers` | Read DSP registers. | — | `dspreg`. | Requires a Falcon build with DSP emulation; otherwise a DSP error. |
| `dbg_dsp_memory` | Dump DSP memory. | `address`, `count=32`, `size="l"`, `space="P"` | `dspmemdump P\|X\|Y $addr-$end`. | DSP memory is fixed 24-bit words; `size` is accepted for symmetry and does not change element width. Address <= `0xFFFF`, count <= 4096. |
| `dbg_dsp_disassemble` | Disassemble DSP code. | `address`, `count=10` | `dspdisasm $addr-$end` (3 bytes per instruction). | Requires Falcon DSP emulation; count <= 200. |
| `dbg_dsp_breakpoint_set` | Set a DSP breakpoint. | `condition` | `dspbreak <condition>`. | DSP memory addresses need a P/X/Y space; only R0-R7 may be used for indirect addressing. Falcon only. |
| `dbg_dsp_breakpoint_list` | List DSP breakpoints. | — | `dspbreak`. | Requires Falcon DSP emulation. |
| `dbg_dsp_continue` | Continue DSP emulation. | — | `dspcont`; no sentinel. | Requires Falcon DSP emulation. Leaves the prompt. |
| `dbg_dsp_step` | Step the DSP. | — | `dspstep` on stdin. | Requires the debugger prompt and Falcon DSP emulation. |
| `dbg_state_diff` | Compare a memory region over time. | `address`, `length`, `before?` | `savebin` snapshots; with no `before`, stores a baseline token; otherwise diffs against the token. | Length 1..1 MiB. Returns changed ranges/count and truncation state; snapshot files live under the server temp area. |
| `dbg_breakpoint_actions` | Validate/preview breakpoint actions. | `address`, `type="exec"`, `condition?`, `print_text?`, `info?`, `trace=false`, `once=false`, `quiet=false`, `file?`, `apply=false` | Builds a `breakpoint ...` command with `:print`, `:info`, `:trace`, `:once`, `:quiet`, `:file`. | `:file` is previewed only and never executed. Other actions run only with `apply=true`. |

## Memory and state (5)

Source: `tools/memory.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `dbg_memory_read` | Read exact guest memory. | `address`, `length` | Debugger `savebin` to a temporary runtime file. | Length 1..65536 bytes. Returns `hex`, `base64`, address, actual length. |
| `dbg_memory_write` | Write exact guest memory. | `address`, `data_hex` | Debugger `loadbin` from a temporary file. | `data_hex` must be even-length hex; <=64 KiB. Destructive. |
| `dbg_memory_dump` | Formatted memory dump. | `address`, `count=64`, `width="b"` | Debugger `memdump`. | Count clamped to 1..4096; width `b`/`w`/`l`. Text output only. |
| `dbg_state_save` | Save emulator state. | `path` | Debugger `statesave "<path>"`, 10 s timeout. | Creates parent directories; returns path. Large file. |
| `dbg_state_load` | Restore emulator state. | `path` | Debugger `stateload "<path>"`, 10 s timeout. | Path must exist. Destructive to current machine state. |

## Watchpoints (2)

Source: `tools/memory_extra.py`. Stock Hatari has no watchpoints.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `dbg_watchpoint_set` | Report watchpoint availability. | `address`, `length=2`, `mode="write"` | Validates the request, then returns capability information. | Always `supported=false`, no emulator state change. Length 1..8. Hint explains the limitation. |
| `dbg_watchpoint_clear` | Report watchpoint clear availability. | — | None. | Always `supported=false`, `cleared=false`; explicit no-op. |

## Protocol capabilities (1)

Source: `tools/protocol.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `emu_capabilities` | Report protocol selection and capability matrix. | `mode?` (`auto`, `legacy`, `framed`, `require-framed`) | Reads the startup banner (if any) and the canonical capability matrix. | Stock Hatari is reported honestly as `legacy` or `banner-only`: `framed`, `input_abs`, `input_frame`, `clock_virtual` and `windows_pipe` are unsupported; binary/framebuffer/input-joystick/frame-step are fallbacks. **No framed HELLO/HELLO_ACK handshake is wired**, so `handshake_complete` is false and strict framed mode is unavailable. |

## Screen capture and video info (2)

Source: `tools/screen.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `screen_screenshot` | Capture the screen. | `path?` | Debugger `screenshot "<path>"`. Defaults to the runtime `screen.png`. | Formats: `.png` (requires a libpng build), `.bmp`, `.neo`, `.ximg`/`.img`. Rendering stays enabled under SDL dummy. Returns path, existence, size, raw text. |
| `screen_info` | Report video information. | `which="videl"` | Debugger `info video\|videl\|vdi`. | Raw text; format varies by machine/build. |

## Frame analysis (7)

Source: `tools/frames.py`. BMP paths support standard-library decoding; PNG capture is file-only.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `screen_frame_capture` | Capture a decodable frame. | `path?`, `format="bmp"` | Debugger screenshot; BMP is decoded by the server. | Returns path, format, bytes, SHA-256, dimensions/bpp for BMP. PNG is returned as a file without stdlib pixel decoding. |
| `screen_frame_info` | Inspect an existing/captured BMP. | `path?` | Reads an existing BMP or captures one first. | Returns dimensions, bpp, top-down flag, file size, file SHA-256, RGBA SHA-256. |
| `screen_pixel_get` | Read one pixel. | `x`, `y` | Captures a BMP and reads RGBA at the coordinate. | Returns RGBA/RGB, hex, pixel/frame/file hashes, dimensions. Out-of-range coordinates fail. |
| `screen_region_hash` | Hash a screen region. | `x`, `y`, `width`, `height`, `algorithm="sha256"` | Captures a BMP, extracts canonical RGBA bytes, hashes with `hashlib`. | Any `hashlib.new` algorithm. Returns region hash, region SHA-256, frame hash, byte/pixel counts. |
| `screen_frame_diff` | Diff two BMP frames. | `path_a`, `path_b` | Decodes both, compares RGBA. | Returns changed pixels/percent, mean/max channel delta, bounding box, both hashes, and a diff BMP under `/tmp`. |
| `screen_frames_diff` | Diff against a previous or a short-delay frame. | `previous?` | Captures current; if no previous, captures one, waits 200 ms, captures another. | Diff BMP under `/tmp`. Host-timed, not frame-locked. |
| `screen_wait_stable` | Poll until two frames match. | `timeout_ms=5000`, `interval_ms=100`, `threshold=0` | Repeated BMP captures, exact hash match or max-channel-delta threshold. | Host-timed polling, not VBL-locked. Matching captures do not prove the guest is idle. Returns samples and `vbl_locked=false`. |

## Input: single events (7)

Source: `tools/input.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `input_key` | Press one key. | `key` | `hatari-event keypress <scancode>` with Shift if needed. | Accepts named keys, one printable ASCII character, or an ST scancode `0-255`. Multi-digit numeric strings are treated as scancodes; a single digit is a printable character. |
| `input_text` | Type a string. | `text`, `delay_ms=10` | Expands to keypress/down/up events. | Printable ASCII plus space, newline and tab. Delay clamped to 0..1000 ms. Host-timed. |
| `input_key_down` | Hold a key. | `key` | `hatari-event keydown <scancode>`. | Named key or character; release with `input_key_up`. |
| `input_key_up` | Release a key. | `key` | `hatari-event keyup <scancode>`. | Named key or character. |
| `input_mouse_move` | Move the mouse relatively. | `dx`, `dy` | `hatari-event mousemove dx dy`. | Relative only; stock Hatari has no absolute positioning API. Units are emulated screen pixels. |
| `input_mouse_button` | Press/release/click a button. | `button="left"`, `action="click"` | `leftdown/up`, `rightdown/up`, or Hatari `doubleclick`. | `left` + `click` uses Hatari's `doubleclick` event; use `down`/`up` for a single press/release. No middle button. |
| `input_joystick` | Emulate joystick input indirectly. | `direction`, `action="press"` | Arrow keydown/keyup events, relying on `--joystick <port>` keyboard emulation. | No direct joystick event or axes. The guest must be configured for keyboard/joystick emulation; keyboard mode steals those keys. |

## Input scripting (6)

Source: `tools/scripting.py`. All execution is host-timed, not frame-locked.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `input_sequence` | Run key/mouse events in order. | `events[]`, `interval_ms=50`, `stop_on_error=true` | `hatari-event` per event. | Event kinds: `key`, `keydown`, `keyup`, `text`, `mouse_move`, `mouse_button`. Returns a per-event log. |
| `input_key_combo` | Hold a key combination. | `keys[]`, `hold_ms=50` | Keydowns in order, hold, releases in reverse. | At most 16 keys. Releases are attempted in `finally` on failure. |
| `input_mouse_click` | Click at the current position. | `x?`, `y?`, `button="left"` | Emits a mouse click. | **Absolute x/y is rejected** with `unsupported`; no honest absolute position is tracked. |
| `input_replay` | Replay a JSON event list. | `path`, `interval_ms=50` | Loads and validates the file, then runs `input_sequence`. | Path is resolved; events use the same schema as `input_sequence`. |
| `input_sequence_save` | Save a reusable event list. | `path`, `events[]` | Validates and writes indented JSON. | Returns count, bytes, SHA-256. Creates parent directories. |
| `input_sequence_load` | Validate without executing. | `path` | Reads and validates JSON. | Returns normalized events and the frame-locked=false note. |

## Media: floppy, HD, cartridge (7)

Source: `tools/media.py`.

The restart tools raise `restart_required`. The generated option is stored in
`HatariError.extra`, but `guarded` reduces the error to `ToolError(str(exc))`
for MCP clients, so only direct Python callers can read the option.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `media_insert_floppy` | Insert a floppy at runtime. | `drive` (`A`/`B`), `path` | Validates `.st`/`.msa`, then `hatari-option --disk-a/--disk-b`. | Runtime hot-swap. Modal alerts are disabled first. |
| `media_eject_floppy` | Eject a floppy. | `drive` | `hatari-option --disk-a none` / `--disk-b none`. | Runtime eject; `none` is Hatari's literal eject value. |
| `media_mount_hd` | Prepare an HD image mount. | `kind`, `path`, `id?` | Validates an ACSI/SCSI (id 0-7) or IDE master/slave image and builds the exact option. | **Never touches the live session.** Returns `restart_required`; the option is only visible to direct Python callers (see note). Hatari applies HD changes with a cold reset. |
| `media_unmount_hd` | Prepare an HD detach. | `kind`, `id?` | Builds the detach option. | **Restart-only**, returns `restart_required`; not a live hot-swap. |
| `media_insert_cartridge` | Prepare a cartridge insert. | `path` | Validates the ROM and builds `--cartridge`. | **Restart-only**, `restart_required`. Cartridge code runs before TOS and is privileged guest code. |
| `media_eject_cartridge` | Prepare a cartridge eject. | — | None. | **Unsupported at runtime**: `--cartridge none` is rejected and there is no control command. Returns `restart_required`; restart without `--cartridge`. |
| `media_status` | Report media-related info. | — | Debugger `info gemdos`, `info acia`, `info video`. | Read-only raw line sections; does not change media. |

## Devices, streams and recording (7)

Source: `tools/devices.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `media_device` | Enable/disable/toggle a file-backed device. | `name`, `action="toggle"` | `hatari-enable/disable/toggle`. | Same device set as `emu_device`: `printer`, `rs232`, `scca`, `sccalan`, `sccb`, `midi`. Fire-and-forget. |
| `media_set_output` | Set a stream input/output path. | `name`, `path` | `hatari-path`. | Output paths are created; input paths must already be readable. An already-open stream may keep the old file until toggled/restarted. |
| `media_set_audio_capture` | Set the YM/WAV capture filename. | `path` | `hatari-path soundout` (there is no CLI option for `szYMCaptureFileName`). | Extension must be `.wav` or `.ym`; it selects the format. Sound must be enabled (bundled profiles use `--sound off`). |
| `media_record_audio_start` | Start YM/WAV capture. | — | `recsound` shortcut (a toggle). | Captures to the configured soundout path; there is no per-recording path or state query. Sends `recsound` blindly, so it may stop an externally started recording. Refuses only if this server already tracks an active recording. |
| `media_record_audio_stop` | Stop audio capture. | — | `recsound` toggle. | Only stops a recording this server tracked; refuses `recording_state_unknown` when the state is unknown, including externally started captures. |
| `media_record_video_start` | Start AVI capture. | `path?`, `codec="png"`, `fps=25` | Sets `--avi-file`, `--avi-vcodec`, `--avi-fps`, then `recanim`. | Codec `png` (needs libpng) or `bmp`; fps 1..100. Codec/fps are read at start and cannot change while recording. No state query; sends `recanim` blindly, so it may stop an externally started recording. Verification is an existing file larger than 8192 bytes after 200 ms. |
| `media_record_video_stop` | Stop AVI capture. | — | `recanim` toggle. | Only stops a recording this server tracked; refuses when the state is unknown, including external recordings. |

## Build, AHCC and program runs (6)

Source: `tools/build.py`, `services/ahcc.py`, `scripts/ahcc-cli.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `ahcc_compile` | Compile one C file with AHCC 6.0. | `source`, `output?`, `tos?`, `includes?[]`, `defines?[]`, `cflags?[]`, `libs?[]`, `timeout_ms=120000`, `keep=false` | Runs `scripts/ahcc-cli.py` in a separate isolated headless Hatari instance; the MCP supervisor is not used. | `.c` only. Explicit and default output must resolve under `mcp/` or `/tmp`; `HATARI_MCP_BUILD_DIR` only selects the default directory within those roots. Returns returncode/stdout/stderr/output/size/timed_out/duration; errors `build_timeout`, `build_failed`, `no_output`. |
| `ahcc_status` | Discover the AHCC toolchain. | — | Checks repo/env paths. | Reports script/AHCC/Hatari/`hatari-prg-args`/TOS/GUI paths and availability flags. |
| `ahcc_gui_start` | Start the AHCC GEM shell. | — | Copies `AHCCST.BIN` to a writable temp workspace and autostarts `AHCCST.PRG` with `display=true`. | Requires `DISPLAY` or `WAYLAND_DISPLAY`; otherwise `unsupported_headless`. Refuses to disturb a running Hatari instance. |
| `ahcc_gui_status` | Report AHCC GUI state. | — | Supervisor status plus a best-effort screenshot. | Skips the screenshot at the debugger prompt and never sends debugger commands while stopped. |
| `run_prg` | Run a PRG/TOS and wait. | `program`, `workspace?`, `args?[]`, `timeout_ms=30000`, `wait=true`, `display=false`, `tos?` | Starts the supervisor with the program's parent (or `workspace`) mapped as `C:`, autostarts, and enables NatFeats. | One GEMDOS directory. Protected corpus/repository roots are rejected as a workspace. Only workspaces under the system temp root (normally `/tmp`) are mounted writable; every other allowed workspace gets `--protect-hd on`, regardless of OS permissions. Only `NF_EXIT` is detected; non-NatFeats programs report `exit_source="timeout"` instead of a fake code. Guest args are limited to 126 bytes. |
| `workspace_info` | Inspect a workspace without modifying it. | `path` | Bounded, symlink-aware scan. | Reports `C:` mapping, policy writability (`writable` is the `mcp/`+temp write allowlist, not OS permissions), bounded file count, and up to 100 `.PRG`/`.TOS` candidates with guest paths. |

## Scenario automation (4)

Source: `tools/automation.py`, `services/scenario.py`, `corpus.py`, `scenario_cli.py`.

| Tool | Purpose | Key args | Mechanism | Returns / limitations |
| --- | --- | --- | --- | --- |
| `scenario_validate` | Validate a scenario without running it. | `path`, `max_cases?` | Schema v1 validation. | Checks unknown keys, duplicate step ids, `${VAR}` declarations, path confinement, unsafe Hatari options, matrix size, and deterministic case ids. YAML requires optional PyYAML. |
| `scenario_run` | Run a scenario and its matrix. | `path`, `artifacts?`, `max_cases=32`, `fail_fast=true`, `jobs=1`, `timeout_ms=120000`, `record=false` | Spawns an MCP stdio server per case and executes allowlisted tools; writes an atomic artifact bundle with manifest/checksums, `report.json`, and `junit.xml`. | Jobs 1..8; timeout 1 s..1 h. Steps use a strict tool allowlist (no `dbg_command`, AHCC, raw writers, trace/profile writers, or `emu_set_option`). Teardown always runs. External corpus is read-only; this is a real integration path, not a mock. |
| `scenario_list_corpus` | Discover external corpora. | — | Checks `OKAMI_ROOT`, `FREEMINT_ROOT`, `GODLIB_ROOT` or default paths. | Reports availability, prerequisites, and skip reasons for Okami-Shell, FreeMiNT and GodLib. Never copies or writes into the corpus. |
| `scenario_replay` | Replay a recorded bundle. | `bundle`, `artifacts?`, `timeout_ms=120000` | Recreates a recorded `replay/` run and compares the final decoded-RGBA screen checkpoint. | Fidelity is always **approximate**: there is no frame-locked input or virtual clock without P7.4/P7.5. |

## MCP resources

Source: `server.py`.

| URI | Content |
| --- | --- |
| `hatari://server/status` | Live lifecycle/process/debugger status as JSON. |
| `hatari://logs/recent` | Last 100 classified stderr entries as text. |
| `hatari://docs/debugger` | Markdown notes on the stock debugger channel and constraints. |

## Deliberately unsupported or fallback-only

These are current, honest constraints rather than planned tool names:

* **Watchpoints** — stock Hatari has none; the two tools report `supported=false`.
* **Framed protocol** — the `protocol/` package implements framing, banners and
  HELLO messages, but the supervisor does not wire a HELLO/HELLO_ACK handshake.
  `emu_capabilities` therefore reports `legacy` or `banner-only`, never native
  framed mode.
* **Absolute mouse** — no stock API; `input_mouse_click(x, y)` is rejected.
* **Direct joystick/analog input** — only keyboard-emulated arrows.
* **DSP** — CPU-side DSP tools require a Falcon build with DSP emulation.
* **Hard disks and cartridges** — mount/insert tools raise
  `restart_required`; they never hot-swap the live machine. The generated
  option is only visible to direct Python callers in `HatariError.extra`.
  There is no runtime cartridge eject.
* **AVI/audio recording** — `recsound`/`recanim` are blind toggles with no
  state query. Start tools may stop an externally started recording; stop
  tools only act on recordings this server tracked and refuse when the state
  is unknown.
* **Config read/save, reboot, command-history clear** — not exposed. Use
  profiles/`emu_start` args, stop + start, and re-enable history respectively.
* **Frame-locked/deterministic input and capture** — host-timed only; the
  scenario runner labels replay fidelity approximate.
* **P8 lab features** — `labs/` is non-shipping and gated by
  `HATARI_MCP_LABS=1`; the investigation verdicts are mostly
  INCONCLUSIVE/narrow, not production capabilities.
