# Example: boot, break, inspect

Goal: boot a headless ST, stop at the VBL handler, inspect registers and
memory, single-step, then resume.

All calls are MCP tool calls. Results are abbreviated; the contract is
`doc/tools-reference.md`.

## 1. Start the machine

```json
{"tool": "emu_start", "arguments": {"profile": "st"}}
```

```json
{"ok": true, "state": "running", "pid": 41231,
 "socket": "/run/user/1000/hatari-mcp-7f3a.sock", "at_prompt": false}
```

The server launched `hatari` with a private control socket, `SDL_VIDEODRIVER=dummy`,
and the profile's machine/memory arguments. Rendering is on, so screenshots
work; no window was opened. NatFeats stays off unless you pass `natfeats=true`.

## 2. Wait for the desktop

```json
{"tool": "screen_frame_capture", "arguments": {"path": "/tmp/st-boot.bmp"}}
```

```json
{"ok": true, "path": "/tmp/st-boot.bmp", "format": "bmp",
 "width": 640, "height": 400, "sha256": "..."}
```

`screen_frame_capture` prefers BMP so the server can decode dimensions and
pixels with the standard library. Use `screen_screenshot` instead when you only
need PNG/other capture formats. If the desktop is not up yet, `console_read` or
`log_read` usually shows boot progress; `emu_status` reports lifecycle state.

## 3. Stop at the VBL handler

Hatari exposes `_vbl` as a builtin debugger symbol. `dbg_breakpoint_wait`
installs a temporary `:once` breakpoint, continues, and waits for the prompt or
timeout; its condition must contain a comparison.

```json
{"tool": "dbg_breakpoint_wait",
 "arguments": {"condition": "pc = _vbl", "timeout_ms": 5000}}
```

```json
{"ok": true, "reason": "breakpoint", "pc": 14743204,
 "state": "at_prompt", "duration_ms": 87}
```

The emulator is now stopped at the debugger prompt. Do **not** call
`emu_pause` here: pause uses `SIGSTOP`, and the debugger is unavailable while
the process is stopped. The breakpoint already stopped execution.

## 4. Inspect

```json
{"tool": "dbg_registers_get"}
```

```json
{"ok": true, "registers": {"pc": 14743204, "sr": 9984,
 "d0": 1, "d1": 0, "a0": 14708736, "a7": 14712830}, "raw": "..."}
```

```json
{"tool": "dbg_disassemble", "arguments": {"address": "pc", "count": 3}}
```

```json
{"ok": true, "address": "pc", "count": 3,
 "instructions": ["$00e0f6a4 4e73 rte", "$00e0f6a6 4e71 nop", "$00e0f6a8 4e75 rts"]}
```

```json
{"tool": "dbg_memory_read",
 "arguments": {"address": "0x00e0f6a4", "length": 16}}
```

```json
{"ok": true, "address": "$e0f6a4", "length": 16,
 "hex": "4e734e714e7500000000000000000000", "base64": "..."}
```

`dbg_memory_read` uses `savebin`, so the bytes are exact. Add
`dbg_memory_dump(address=..., count=..., width="w")` when you want Hatari's
formatted text instead.

## 5. Step, then resume

```json
{"tool": "dbg_step"}
```

```json
{"ok": true, "raw": "..."}
```

`dbg_step` and `dbg_step_over` require the debugger prompt and stay there.
They step one instruction per call. Then resume:

```json
{"tool": "dbg_continue"}
```

```json
{"ok": true, "state": "running"}
```

`dbg_continue` does not produce a completion sentinel, so the tool returns once
the command is accepted. A later breakpoint is reported through `emu_status`,
`dbg_exception_get`, or `log_read`.

## 6. Shut down

```json
{"tool": "emu_stop", "arguments": {"force": false}}
```

```json
{"ok": true, "state": "exited", "returncode": 0}
```

## Notes an agent should know

* `_vbl` fires 50/60 times a second; use a `:once` breakpoint (as above) or a
  condition, or it retriggers immediately after `dbg_continue`.
* If a call times out, the machine may be stopped at the debugger prompt; check
  `emu_status` (`at_prompt`) before retrying.
* Use `dbg_memory_write` (not a raw `memwrite` shell command) when you intend to
  modify guest memory; it is destructive and can crash the guest.
* `dbg_command` is the raw privileged escape hatch for debugger operations the
  typed tools do not cover.
* TOS ROMs are user-supplied; if `emu_start` fails with `missing_tos`, ask the
  user for a ROM path or set `HATARI_TOS` / the profile's `paths.tos` to an
  EmuTOS image.
* The only MCP resources are `hatari://server/status`, `hatari://logs/recent`
  and `hatari://docs/debugger`; screenshots are files returned by path.
