# Example: mount media, drive the machine, snapshot it

Goal: boot an STE, insert a floppy, run a program from it, capture the screen,
save a state, corrupt something, then restore and prove the restore worked.

## 1. Boot with a floppy already mounted

```json
{"tool": "emu_start",
 "arguments": {"profile": "ste",
               "args": ["--disk-a", "fixtures/demo.st"]}}
```

```json
{"ok": true, "state": "running", "machine": "ste", "memsize_kb": 2048}
```

## 2. Watch the media list

```json
{"tool": "media_list"}
```

```json
{"ok": true, "floppies": {"A": "fixtures/demo.st", "B": null},
 "hd": [], "cartridge": null}
```

## 3. Interact

The boot sector of `demo.st` runs automatically. If a key is needed:

```json
{"tool": "input_key", "arguments": {"key": "space", "down": true}}
```

```json
{"tool": "input_key", "arguments": {"key": "space", "down": false}}
```

For text entry (e.g. a shell prompt) use `input_type_text`; it emits the
`hatari-event text ...` sequence and does not wait for the guest.

## 4. Capture the screen

```json
{"tool": "screen_screenshot", "arguments": {"path": "/tmp/ste-demo.png"}}
```

```json
{"ok": true, "path": "/tmp/ste-demo.png", "width": 640, "height": 400}
```

The client can render the file, or use `hatari://emulator/screen` as an image
resource.

## 5. Save state

```json
{"tool": "state_save", "arguments": {"path": "fixtures/demo.state"}}
```

```json
{"ok": true, "path": "fixtures/demo.state", "bytes": 3145728}
```

## 6. Prove restore works

Read a word, overwrite it, screenshot, then restore.

```json
{"tool": "memory_read", "arguments": {"address": "0x000000", "length": 2}}
```

```json
{"ok": true, "hex": "60 1a"}
```

```json
{"tool": "memory_write",
 "arguments": {"address": "0x000000", "data": "4e71", "format": "hex"}}
```

```json
{"ok": true, "bytes_written": 2}
```

```json
{"tool": "state_load", "arguments": {"path": "fixtures/demo.state"}}
```

```json
{"ok": true, "restored": true}
```

```json
{"tool": "memory_read", "arguments": {"address": "0x000000", "length": 2}}
```

```json
{"ok": true, "hex": "60 1a"}
```

Restore succeeded: the original word is back.

## Notes

* `state_save` writes the whole emulated machine; expect tens of MiB for
  larger configurations. Do not commit states.
* Floppy images in `fixtures/` are local-only. If `--disk-a` points at a
  missing file, Hatari may start without it; check `media_list` afterwards.
* `memory_write` is destructive and can crash the guest. That is fine here
  because a state was saved first — a useful pattern to teach the agent.
* Screenshots require rendering. If the profile sets
  `launch.display = false`, the server still uses the dummy SDL driver rather
  than `--disable-video`, so capture keeps working.
