# 0001 — Language, transport and Hatari integration

* Status: Accepted
* Date: 2026-10-26
* Supersedes: —

## Context

We need an MCP server that can drive every major Hatari function, including
the debugger. Hatari is C and has no stable library API. It already exposes:

1. a Unix-domain control socket (`--control-socket`) with a small text command
   set, including `hatari-debug <cmd>` for the debugger;
2. a one-way command FIFO (`--cmd-fifo`);
3. debugger command files (`--parse`) and a line API whose output goes to
   stderr;
4. command-line options for essentially all configuration.

MCP servers are typically small, long-running process supervisors. The
protocol is JSON-RPC; the official SDKs cover Python and TypeScript best.

## Decision

* **Language:** Python 3.11+ (standard library `socket`, `subprocess`,
  `tomllib`, `asyncio`).
* **MCP SDK:** the official `mcp` Python SDK, stdio transport only.
* **Packaging:** a `src/`-layout package (`hatari_mcp`) with `pyproject.toml`,
  managed by `uv`; `pytest` for tests, `ruff` for lint/format.
* **Integration:** launch `hatari` as a child process with
  `--control-socket <private socket>` and pipes for stdout/stderr.
  * Commands go over the socket.
  * Debugger replies are read from stderr and delimited with an
    `echo __hatari_mcp_done_<uuid>__` sentinel (`doc/architecture.md` §4).
  * No code from Hatari is linked; no GDB stub is required for v1.
* **Headless default:** `SDL_VIDEODRIVER=dummy`, `--sound off`, no dialogs.
  Rendering stays enabled so screenshots and framebuffer reads work.
* **Forward path:** a framed, versioned protocol extension lives in
  `patches/`; the server negotiates it and falls back to the sentinel scheme.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| C server linked into/next to Hatari | Tight coupling, harder to iterate, MCP SDKs are weak in C, complicates GPL boundary, no isolation if the emulator crashes. |
| GDB remote stub | Hatari has no GDB stub; writing one is a separate large project and does not cover chipset/input/media/state operations. |
| TypeScript / Node | Viable (official SDK), but the existing ecosystem around Hatari is Python (`hconsole.py`, `python-ui`) and Python is better for binary parsing and process control. |
| Rust | Strong runtime, but slower to iterate and the MCP SDK is less mature; team familiarity is unclear. |
| Modify Hatari to embed an MCP server | Deep coupling, rebuild required for every tool change, and it pushes a generic protocol into the emulator. |
| Attach to a user-started Hatari | Ambiguous lifecycle, no control over launch flags, harder to isolate tests; may be added later as an explicit `attach` mode. |

## Consequences

* Fast iteration: tools are plain Python, Hatari is a black-box dependency.
* Works with unmodified upstream Hatari today, so the server can ship before
  any patch is accepted.
* v1 is Unix-only because the control socket is. Windows requires either the
  upstream patch or a `--parse`/stdin-driven fallback; tracked in the roadmap.
* Response parsing is text-based and inherently best-effort until the patch
  lands; every tool must tolerate partial output and timeouts.
* Debugger replies contend with log/trace lines on stderr, so the demuxer is
  a core, well-tested component.
* A crash in Hatari kills only the child; the server reports `emulator_gone`
  and can restart.
