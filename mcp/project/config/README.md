# Configuration

Examples only — copy and edit. Nothing here is read automatically.

## MCP client entry

`mcp-client.example.json` is a generic `mcpServers` entry. Most clients
(Claude Desktop, VS Code, opencode, ...) accept this shape, sometimes under a
different top-level key. Adjust:

* `command` / `args` to point at your checkout;
* `HATARI_BIN` to the built emulator (`<repo>/build/src/hatari`);
* `HATARI_MCP_PROFILE` to a profile name from `profiles/`.

The server speaks MCP over stdio; do not add a TCP/HTTP transport.

## Launch profiles

One TOML file per machine in `profiles/`:

```toml
name = "st"
description = "Atari 520ST, 1 MiB, TOS 1.04, headless"

[launch]
args = ["--machine", "st", "--memsize", "1", "--sound", "off"]
env = { SDL_VIDEODRIVER = "dummy" }

[paths]
tos = "~/atari/tos104.img"   # required, never committed
```

Keys:

| Key | Meaning |
| --- | --- |
| `name` | Profile id passed to `emu_start` and `HATARI_MCP_PROFILE`. |
| `description` | Human-readable summary. |
| `launch.args` | Extra Hatari arguments appended after the server's defaults. |
| `launch.env` | Extra environment variables for the child. |
| `launch.display` | `false` (default) forces `SDL_VIDEODRIVER=dummy`; `true` opens a window. |
| `paths.*` | Configured emulator paths; `tos` is required. |
| `server.command_timeout_ms` | Optional per-profile override. |

Profiles must not contain secrets. ROMs and disk images referenced by
`paths.*` are local files and are not part of the repository.

## Precedence

1. explicit tool arguments (`emu_start(profile=..., args=[...])`);
2. `HATARI_MCP_PROFILE` and its profile file;
3. built-in defaults (headless, sound off, no dialogs, private socket).

`HATARI_BIN` overrides the emulator binary in every case.
