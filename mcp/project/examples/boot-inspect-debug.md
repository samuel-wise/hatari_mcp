# Example: boot, break, inspect

Goal: boot a headless ST, stop the machine at the VBL handler, inspect
registers and memory, single-step a few instructions, then resume.

All calls are MCP tool calls. Results are abbreviated.

## 1. Start the machine

```json
{"tool": "emu_start", "arguments": {"profile": "st"}}
```

```json
{"ok": true, "pid": 41231, "state": "running", "machine": "st",
 "memsize_kb": 1024, "socket": "/run/user/1000/hatari-mcp-7f3a.sock"}
```

The server launched `hatari --machine st --memsize 1 --sound off
--control-socket ...` with `SDL_VIDEODRIVER=dummy`. Rendering is on, so
screenshots will work; no window was opened.

## 2. Wait for the desktop

```json
{"tool": "screen_screenshot", "arguments": {"path": "/tmp/st-boot.png"}}
```

```json
{"ok": true, "path": "/tmp/st-boot.png", "width": 640, "height": 400}
```

If the desktop is not up yet, `console_read` or `log_read` usually shows boot
progress. Machine state is available from `emu_status`.

## 3. Stop at the VBL handler

Hatari exposes `_vbl` as a builtin debugger symbol.

```json
{"tool": "dbg_breakpoint_set",
 "arguments": {"address": "_vbl", "type": "exec", "once": true}}
```

```json
{"ok": true, "id": 1, "address": "0x00e0f6a4", "type": "exec"}
```

```json
{"tool": "dbg_breakpoint_wait", "arguments": {"timeout_ms": 5000}}
```

```json
{"ok": true, "reason": "breakpoint", "id": 1,
 "pc": "0x00e0f6a4", "cpu": "m68000"}
```

The emulation is now stopped inside the debugger. `emu_pause` was not needed:
the breakpoint did it.

## 4. Inspect

```json
{"tool": "dbg_registers_get", "arguments": {"cpu": "m68k"}}
```

```json
{"ok": true, "pc": "0x00e0f6a4", "sr": "0x2700", "usp": "0x00e12340",
 "d0": "0x00000001", "d1": "0x00000000", "a0": "0x00e0f000",
 "a7": "0x00e0fffe"}
```

```json
{"tool": "dbg_disassemble", "arguments": {"address": "pc", "count": 3}}
```

```json
{"ok": true, "instructions": [
  {"address": "0x00e0f6a4", "opcode": "0x4e73", "text": "rte"},
  {"address": "0x00e0f6a6", "opcode": "0x4e71", "text": "nop"},
  {"address": "0x00e0f6a8", "opcode": "0x4e75", "text": "rts"}
]}
```

```json
{"tool": "memory_read",
 "arguments": {"address": "0x00e0f6a4", "length": 16, "format": "hex"}}
```

```json
{"ok": true, "address": "0x00e0f6a4",
 "hex": "4e73 4e71 4e75 0000 0000 0000 0000 0000",
 "ascii": "N sNqNu..........."}
```

## 5. Step, then resume

```json
{"tool": "dbg_step", "arguments": {"count": 2}}
```

```json
{"ok": true, "pc": "0x00e0f6a8"}
```

```json
{"tool": "dbg_continue"}
```

```json
{"ok": true, "state": "running"}
```

`dbg_continue` does not produce a completion line in the sentinel protocol, so
the tool returns as soon as the command is accepted. Later breakpoint hits
arrive as notifications and via `hatari://server/status`.

## 6. Shut down

```json
{"tool": "emu_stop", "arguments": {"force": false}}
```

```json
{"ok": true, "exit_code": 0, "socket_removed": true}
```

## Notes an agent should know

* `_vbl` fires 50/60 times a second; use `once: true` or a condition, or the
  breakpoint will retrigger immediately after `cont`.
* If a call times out, the machine is probably stopped at a breakpoint; check
  `dbg_status` before retrying.
* Never `memwrite` unless the goal requires it — it is marked destructive.
* TOS ROMs are user-supplied; if `emu_start` fails with `missing_tos`, ask the
  user for a ROM path or point `paths.tos` at an EmuTOS image.
