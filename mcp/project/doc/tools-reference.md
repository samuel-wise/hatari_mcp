# MCP tool reference (design)

Status: proposed v1 surface. Names are final unless an ADR changes them.
Implementation notes and caveats are in `doc/architecture.md`; this file is the
contract shown to clients.

Conventions:

* Names are `snake_case`, grouped by prefix: `emu_`, `input_`, `media_`,
  `state_`, `screen_`, `dbg_`, `trace_`.
* Addresses and values accept `0x`/`$` hex, decimal, or debugger expressions
  (e.g. `_vbl`, `pc`, `d0`, symbols loaded via `dbg_symbols_load`).
* Read-only tools set `readOnlyHint`; destructive tools set
  `destructiveHint` and are gated when the server runs with an allowlist.
* Every tool returns `{ ok, ... }`; on failure `{ ok: false, error, detail }`.
  Raw emulator text is always included as `raw` when parsing is best-effort.

## Lifecycle

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `emu_start` | `profile?`, `args?[]`, `tos?`, `display?` | spawn `hatari` with profile + args, `--control-socket` | Idempotent while running. Boot timeout 30 s. |
| `emu_stop` | `force?` | `hatari-debug quit`, then SIGTERM/SIGKILL | Idempotent. |
| `emu_pause` | — | `hatari-stop` | Emulation paused; the control socket remains responsive. If Hatari is already at the debugger prompt, use the stdin channel instead. |
| `emu_resume` | — | `hatari-cont` | |
| `emu_reset` | `kind: soft\|hard\|cold\|warm` | `hatari-debug reset <kind>` | Cold = full machine reset. |
| `emu_status` | — | supervisor state + `hatari-debug info` | Reports pid, uptime, pause state, exit code, profile. |
| `emu_reboot` | `args?[]` | stop + start | Preserves nothing unless a state is saved first. |
| `emu_quit_debugger` | — | `quit` on the stdin channel when at the prompt; `hatari-debug quit` otherwise | Needed when stopped at a breakpoint. |

## Configuration and devices

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `emu_set_option` | `options: string\|string[]` | `hatari-option <args>` | Runtime-applicable options only; Hatari rejects some at runtime. |
| `emu_get_config` | `section?` | `hatari-debug setopt --help` + supervisor config | Returns the merged launch config. |
| `emu_save_config` | `path` | `hatari-option --saveconfig` | Writes a Hatari `.cfg`. |
| `emu_set_path` | `name`, `path` | `hatari-path <name> <path>` | Names: `memauto`, `memsave`, `midiin/out`, `printout`, `soundout`, `rs232in/out`, `scca*`, `sccalan*`, `sccb*`. |
| `emu_device` | `device`, `action: enable\|disable\|toggle` | `hatari-enable/disable/toggle` | Devices: `printer`, `rs232`, `scca`, `sccalan`, `sccb`, `midi` only. |
| `emu_machine` | `machine: st\|megast\|ste\|megaste\|tt\|falcon` | `emu_set_option --machine` or restart | Chipset changes generally require restart. |
| `emu_memory_size` | `mb` | `emu_set_option --memsize`, restart | Valid sizes depend on machine. |

## Input

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `input_key` | `key`, `down: bool` | `hatari-event keydown/up <key>` | Single alphanumeric char or ST scancode 0-255; scancodes are layout-independent. |
| `input_type_text` | `text`, `delay_ms?` | expanded to `keydown/keyup` (with Shift where needed) | Mirrors `tools/hconsole`; punctuation requires shift/scancode mapping. |
| `input_shortcut` | `name` | `hatari-shortcut <name>` | Only `mousegrab`, `coldreset`, `warmreset`, `screenshot`, `bosskey`, `recanim`, `recsound`, `savemem`, `quit`. |
| `input_mouse_move` | `dx`, `dy` | `hatari-event mousemove <dx> <dy>` | Relative, in emulated screen pixels. Absolute positioning needs the corner-pin helper (Phase 2). |
| `input_mouse_button` | `button: left\|right`, `down` | `hatari-event leftdown/leftup/rightdown/rightup` | Use `doubleclick` for a double-click. |
| `input_joystick` | `port`, `x`, `y`, `fire?` | arrow/fire scancodes; requires `--joystick <port>` keyboard emulation | No direct axes/buttons in stock Hatari; direct injection is Phase 7 `[P]`. |
| `input_paste_sequence` | `events: [{key, down, delay_ms?}]` | timed `hatari-event` batch | Server-side delays; not frame-locked until Phase 7. |

## Media

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `media_insert_floppy` | `drive: A\|B`, `path` | `hatari-option --disk-a <path>` / `--disk-b <path>` | Prefer an existing image; `.st`/`.msa`. |
| `media_eject_floppy` | `drive` | `hatari-option --disk-a none` / `--disk-b none` | `none` ejects; the control parser has no quoting, so do not pass an empty string. |
| `media_set_hd` | `target`, `path` | `hatari-option --acsi/--scsi/--ide-*` | ACSI/SCSI/IDE differ per machine. |
| `media_insert_cart` | `path` | `hatari-option --cartridge` | |
| `media_blank_floppy` | `drive`, `path`, `size_kb?` | host tool (`createBlankImage`) + insert | Generation happens server-side. |
| `media_list` | — | `hatari-debug info` + supervisor config | Current mounted images. |

## Screen and observation

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `screen_screenshot` | `path`, `format?` | `hatari-debug screenshot <path>` | Requires rendering (no `--disable-video`). Returns path + dimensions. |
| `screen_get_framebuffer` | `format: png\|rgba`, `scale?` | v1: screenshot to temp file; v2: patch readback | MCP resource/embedded image. |
| `screen_info` | — | `hatari-debug info` | Resolution, refresh, video mode, borders. |
| `console_read` | `lines?` | stdout ring buffer | TOS console output; VT-52 is flattened to text. |
| `console_clear` | — | drop buffer | |
| `log_read` | `level?`, `lines?`, `since?` | stderr ring buffer | Includes debugger output and log messages. |
| `log_set_level` | `level` | `hatari-option --log-level` | |

## State and memory

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `state_save` | `path?` | `hatari-debug statesave <path>` | Memory snapshot; large. |
| `state_load` | `path` | `hatari-debug stateload <path>` | Restores CPU/chipset/memory. |
| `state_export` | `path` | host: copy current snapshot | |
| `memory_read` | `address`, `length`, `format: hex\|base64` | `hatari-debug memdump` or `savebin` | v1 hex parse; v2 binary readback. 32 KiB cap per call. |
| `memory_write` | `address`, `data`, `format` | `hatari-debug memwrite` | Destructive. |
| `memory_fill` | `address`, `length`, `byte` | `memwrite` loop / patch | |
| `memory_load_binary` | `address`, `path` | `hatari-debug loadbin <file> <addr>` | |
| `memory_save_binary` | `address`, `length`, `path` | `hatari-debug savebin` | |
| `memory_find` | `address`, `length`, `pattern` | `hatari-debug find` | |
| `memory_dump_struct` | `address`, `name` | `hatari-debug struct` | Requires a loaded struct definition. |
| `state_annotate` | `text` | server-side bookmark | Not an emulator feature; helps agents. |

## Debugger: session and execution

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `dbg_status` | — | `hatari-debug info` + stderr state | Whether CPU/DSP is stopped, at what PC. |
| `dbg_continue` | `cpu?` | `cont` / `dspcont` on the stdin channel at the prompt | Leaves the debugger; no sentinel arrives, so await the run state. |
| `dbg_step` | `cpu?`, `count?` | `step` / `dspstep` on stdin | Stays at the prompt; `count` is looped server-side. |
| `dbg_step_over` | `cpu?` | `next` / `dspnext` on stdin | Stays at the prompt. |
| `dbg_run_until` | `address`, `timeout_ms?` | temp breakpoint + `cont` | Removes the breakpoint on hit/timeout. |
| `dbg_command` | `command` | raw debugger command on the active channel | Escape hatch; documented as privileged. |
| `dbg_echo` | `text` | `echo` | Used internally for sentinels. |

## Debugger: breakpoints and watchpoints

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `dbg_breakpoint_set` | `address`, `type: exec\|mem\|reg`, `condition?`, `once?`, `cpu?` | `breakpoint` / `dspbreak` | Conditions use debugger expression syntax. |
| `dbg_breakpoint_list` | — | `breakpoint` (no args) | |
| `dbg_breakpoint_delete` | `id\|all` | `breakpoint <index>` / `breakpoint all` | No enable/disable; delete and re-add. |
| `dbg_watchpoint_set` | `address`, `width`, `mode: r\|w\|rw` | `breakpoint` on memory | Emulated via memory breakpoints. |
| `dbg_breakpoint_wait` | `timeout_ms` | event stream | Blocks until a breakpoint/exception/entry is observed. |

## Debugger: registers and code

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `dbg_registers_get` | `cpu?` | `cpureg` / `dspreg` | Parsed into named fields (d0-d7, a0-a7, pc, sr, ...). |
| `dbg_registers_set` | `registers` | `cpureg <reg>=<value>` | Destructive. |
| `dbg_disassemble` | `address`, `count` | `disasm` / `dspdisasm` | Returns `{address, opcode, text}`. |
| `dbg_evaluate` | `expression` | `evaluate` | Supports registers, symbols, `_vbl`, etc. |
| `dbg_backtrace` | `limit?` | `backtrace` | CPU; `bt` needs stack frames/symbols. |
| `dbg_history` | `cpu?`, `count?`, `enable?` | `history` | Must be enabled before it is useful. |
| `dbg_symbols_load` | `path`, `name?`, `prg?` | `symbols` | Embedded ELF/DRI/GST/a.out tables or nm `.sym`; `prg` sets the base. |
| `dbg_symbols_list` | `filter?` | `symbols` / `info symbols` | |

## Debugger: tracing and profiling

| Tool | Arguments | Mechanism | Notes |
| --- | --- | --- | --- |
| `dbg_trace_set` | `sets: string[]` | `hatari-debug trace` | `trace help` lists sets (`cpu_disasm`, `video_hbl`, `dma`, ...). |
| `dbg_trace_get` | — | `hatari-debug trace` | Active sets. |
| `dbg_trace_file` | `path?` | `--trace-file` / `logfile` | |
| `dbg_profile_start` | `cpu?`, `address?` | `profile` | |
| `dbg_profile_read` | `cpu?` | `profile` | Text histogram; parse to `{address, symbol, count}`. |
| `dbg_profile_stop` | `cpu?` | `profile off` | |

## Resources

| URI | Content |
| --- | --- |
| `hatari://docs/debugger` | `doc/debugger.html` (bundled) |
| `hatari://docs/keymap` | `doc/keymap-sample.txt` |
| `hatari://server/status` | Live supervisor state |
| `hatari://emulator/registers` | Last known CPU/DSP register snapshot |
| `hatari://emulator/screen` | Latest screenshot (image/png) |
| `hatari://logs/recent` | Bounded log tail |

## Deliberately excluded from v1

* Arbitrary host shell execution. The debugger can be abused for this; the
  server exposes `dbg_command` as an explicitly privileged escape hatch
  instead of pretending otherwise.
* Network-facing transports (SSE/HTTP). Run over stdio only.
* Multi-instance orchestration. One child per server process; clients can
  spawn several servers.
