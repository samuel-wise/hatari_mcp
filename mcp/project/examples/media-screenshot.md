# Example: mount media, drive the machine, snapshot it

Goal: boot an STE, insert a floppy at runtime, drive the machine, capture the
screen, save a state, write memory, then restore and prove the restore worked.

All calls are MCP tool calls. Results are abbreviated; the contract is
`doc/tools-reference.md`.

## 1. Boot the machine

```json
{"tool": "emu_start", "arguments": {"profile": "ste"}}
```

```json
{"ok": true, "state": "running", "pid": 41231}
```

## 2. Insert media at runtime

```json
{"tool": "media_insert_floppy",
 "arguments": {"drive": "A", "path": "fixtures/demo.st"}}
```

```json
{"ok": true, "drive": "A", "path": "/.../fixtures/demo.st",
 "option": "--disk-a /.../fixtures/demo.st"}
```

Floppies can be hot-swapped. Hard disks and cartridges are **not** live
hot-swaps: `media_mount_hd` / `media_insert_cartridge` return
`restart_required` with the exact option to pass to a new `emu_start`.

Check media-related state without changing it:

```json
{"tool": "media_status"}
```

```json
{"ok": true, "gemdos": ["..."], "acia": ["..."], "video": ["..."]}
```

## 3. Interact

Floppy boot sectors may run automatically. For a single key:

```json
{"tool": "input_key", "arguments": {"key": "space"}}
```

To hold and release a key separately:

```json
{"tool": "input_key_down", "arguments": {"key": "space"}}
{"tool": "input_key_up", "arguments": {"key": "space"}}
```

For text entry use `input_text`; it expands printable ASCII into key events and
returns after sending them, without waiting for the guest:

```json
{"tool": "input_text", "arguments": {"text": "dir", "delay_ms": 10}}
```

Mouse motion is relative only (`input_mouse_move`). Absolute positioning is not
available on stock Hatari.

## 4. Capture the screen

```json
{"tool": "screen_frame_capture", "arguments": {"path": "/tmp/ste-demo.bmp"}}
```

```json
{"ok": true, "path": "/tmp/ste-demo.bmp", "format": "bmp",
 "width": 640, "height": 400, "sha256": "..."}
```

Use `screen_pixel_get`, `screen_region_hash`, or `screen_frame_diff` for
pixel-level checks. PNG capture is file-only (`screen_screenshot` or
`screen_frame_capture(format="png")`); BMP is needed for stdlib pixel decoding.

## 5. Save state

```json
{"tool": "dbg_state_save", "arguments": {"path": "/tmp/demo.state"}}
```

```json
{"ok": true, "path": "/tmp/demo.state"}
```

## 6. Prove restore works

Read a word, overwrite it, then restore and verify.

```json
{"tool": "dbg_memory_read",
 "arguments": {"address": "0x000000", "length": 2}}
```

```json
{"ok": true, "address": "$0", "length": 2, "hex": "601a", "base64": "YBo="}
```

```json
{"tool": "dbg_memory_write",
 "arguments": {"address": "0x000000", "data_hex": "4e71"}}
```

```json
{"ok": true, "address": "$0", "length": 2, "raw": "..."}
```

```json
{"tool": "dbg_state_load", "arguments": {"path": "/tmp/demo.state"}}
```

```json
{"ok": true, "path": "/tmp/demo.state"}
```

```json
{"tool": "dbg_memory_read",
 "arguments": {"address": "0x000000", "length": 2}}
```

```json
{"ok": true, "address": "$0", "length": 2, "hex": "601a", "base64": "YBo="}
```

Restore succeeded: the original word is back.

## Notes

* `dbg_state_save` writes the whole emulated machine; expect tens of MiB for
  larger configurations. Do not commit states.
* Floppy images in `fixtures/` are local-only. `media_insert_floppy` validates
  the file before sending the option; check `log_read` if Hatari rejects it.
* `dbg_memory_write` is destructive and can crash the guest. That is fine here
  because a state was saved first — a useful pattern to teach the agent.
* Screenshots require rendering. The server uses `SDL_VIDEODRIVER=dummy`
  instead of `--disable-video`, so capture keeps working headlessly.
* `input_text`, `input_sequence`, `input_key_combo` and `input_mouse_click` are
  host-timed, not VBL/frame-locked; `input_mouse_click` clicks the current
  pointer position and rejects absolute `x`/`y`.
