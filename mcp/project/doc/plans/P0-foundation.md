# P0 — Foundation

Status: partial — supervisor, channels, demuxer, profiles and first tools implemented; CI and investigation-doc acceptance items remain
Owner: phase implementer (core)
Last updated: 2026-10-27
Roadmap: P0.1–P0.10 (including P0.3a), gate in roadmap §5 Phase 0

## Goal

Ship a small, boring, reliable Python MCP server that can own one Hatari child
process, speak MCP v2 over stdio, open a private AF_UNIX control socket, keep
Hatari's stdin/stdout/stderr channels separate, correlate debugger replies with
a sentinel, expose the first lifecycle/logging/debug tools, and pass deterministic
unit tests plus one real-Hatari integration test. P0 adds no new emulator
capability; it makes the process wrapper trustworthy enough to build P1 on.

## Dependencies

* None on earlier phases.
* ADR 0001 (Python 3.11+, `uv`, `ruff`, `pytest`, stdio-only MCP).
* `mcp` Python SDK v2, pinned to `>=2.3,<3`; the high-level server class is
  `mcp.server.mcpserver.MCPServer` (importing `mcp.server.fastmcp` is a v2
  error).
* A local Hatari build at `build-install/bin/hatari` (verified 2.6.1-devel in
  this tree) with `--control-socket`, `--parse`, `--natfeats`, and readline
  support.
* An EmuTOS image supplied through `HATARI_TOS` or the fixture harness. No
  proprietary TOS image is committed or downloaded by CI.
* Linux or another Unix with `AF_UNIX`; Windows is out of scope until P7.

## Deliverables and file ownership

All implementation paths are under `mcp/project/src/hatari_mcp/` unless noted.
A single implementer owns the phase; reviewers own only the review record.

| Path | Purpose |
| --- | --- |
| `mcp/project/pyproject.toml` | Package metadata, `hatari-mcp` console entry point, dependencies, `ruff`, `pytest`, `uv` settings. |
| `mcp/project/src/hatari_mcp/__init__.py` | Package version and public exports; no side effects on import. |
| `mcp/project/src/hatari_mcp/__main__.py` | `python -m hatari_mcp` entry point that calls `server.main()`. |
| `mcp/project/src/hatari_mcp/server.py` | Builds `MCPServer`, registers tools/resources, installs lifespan cleanup, runs stdio transport. |
| `mcp/project/src/hatari_mcp/app.py` | Typed application context: config, supervisor, path policy, event bus. No global mutable singleton. |
| `mcp/project/src/hatari_mcp/config.py` | Environment/default merging, timeouts, limits, `HATARI_BIN`, state/workspace roots. |
| `mcp/project/src/hatari_mcp/profiles.py` | TOML profile loading and precedence; path validation. |
| `mcp/project/src/hatari_mcp/path_policy.py` | Resolve/confine paths; symlink and traversal checks; allowlist of roots. |
| `mcp/project/src/hatari_mcp/errors.py` | Typed error codes, hints, and exception-to-code mapping. |
| `mcp/project/src/hatari_mcp/result.py` | `{ok, ...}` envelope and `CallToolResult` construction, including `is_error`. |
| `mcp/project/src/hatari_mcp/supervisor.py` | One-child process lifecycle, listener/accept, pipes, wait/cleanup. |
| `mcp/project/src/hatari_mcp/state.py` | Explicit lifecycle/debugger state machine and transition guards. |
| `mcp/project/src/hatari_mcp/channels.py` | Socket/stdin command writers, single command lock, sentinel correlation, prompt probe. |
| `mcp/project/src/hatari_mcp/socketdir.py` | Private `0700` runtime directory, short-path validation, unlink semantics. |
| `mcp/project/src/hatari_mcp/demux.py` | Per-pipe readers, line splitting, classification, sentinel dispatch, state hints. |
| `mcp/project/src/hatari_mcp/ring.py` | Bounded byte/line ring buffers with monotonic sequence cursors. |
| `mcp/project/src/hatari_mcp/events.py` | Async event queue for breakpoint/exception/exit/debugger-entry notifications. |
| `mcp/project/src/hatari_mcp/resources.py` | `hatari://server/status`, `hatari://logs/recent`, documentation resources. |
| `mcp/project/src/hatari_mcp/fixtures.py` | Fixture discovery, checksum verification, skip reasons. |
| `mcp/project/src/hatari_mcp/tools/__init__.py` | Tool registration aggregation; no business logic. |
| `mcp/project/src/hatari_mcp/tools/lifecycle.py` | `emu_start`, `emu_stop`, `emu_pause`, `emu_resume`, `emu_status`. |
| `mcp/project/src/hatari_mcp/tools/logs.py` | `log_read`, `console_read`. |
| `mcp/project/src/hatari_mcp/tools/debug.py` | `dbg_command` privileged escape hatch. |
| `mcp/project/tests/unit/` | Pure tests for config, profiles, demux, sentinel, state, errors, paths. |
| `mcp/project/tests/integration/` | Real-Hatari lifecycle/channel tests; skip cleanly without fixtures. |
| `mcp/project/tests/corpus/` | Recorded stdout/stderr fixtures and expected classifications. |
| `mcp/project/tests/e2e/` | MCP stdio client test. |
| `mcp/project/scripts/fetch-fixtures.sh` | Fetch EmuTOS with pinned SHA256; idempotent. |
| `mcp/project/scripts/make-fixtures.sh` | Generate blank floppy/GEMDOS directory deterministically. |
| `mcp/project/scripts/ci.sh` | Local CI entry point used by the hosted workflow. |
| `ci/mcp.yml` (or the repository's existing CI location) | Lint, unit, integration-with-EmuTOS, no proprietary ROMs. |
| `mcp/project/doc/investigations/debugger-stdin-spike.md` | P0.3a findings and the go/no-go decision. |
| `mcp/project/doc/tools-reference.md` | Update only after tool names/schemas are implemented. |

No third-party source, ROM, disk image, or generated state file is committed.

## Design / constraints

### 1. MCP SDK v2 server

* Use `from mcp.server.mcpserver import MCPServer, Context`; do not import
  `mcp.server.fastmcp`.
* Construct one `MCPServer("hatari-mcp", ...)` in `server.py` and register tools
  via `@mcp.tool(name=..., annotations=ToolAnnotations(...))`.
* Tool return values are dicts/models for success. For the documented
  `{ok:false,error,detail}` contract, return an explicit `CallToolResult` with
  `is_error=True`, a human-readable `content` message, and a structured
  envelope. Do not return a plain error string, which would look successful.
* Raise `ToolError` only for execution failures that are not already represented
  by the envelope; unexpected exceptions are logged with tracebacks and mapped
  to a generic `internal_error` envelope.
* Set `read_only_hint`/`destructive_hint` from the tool metadata table. These
  are hints, not authorization.
* `mcp.run()` (stdio default) is called under `if __name__ == "__main__":`.
  Never write application output to stdout; the SDK owns the wire. Logging goes
  to stderr.
* Resources: `hatari://server/status` (JSON), `hatari://logs/recent` (text),
  `hatari://docs/debugger`, `hatari://docs/keymap`. Resources return strings,
  dicts, or bytes; binary screenshots are a P1 resource.

### 2. Process supervisor

* Exactly one Hatari child per server process in P0. A second `emu_start` while
  running returns the current status with `already_running: true` rather than
  spawning another child.
* Create the private socket directory **before** spawn:
  `$XDG_RUNTIME_DIR/hatari-mcp-XXXX` or `tempfile.mkdtemp(prefix="hatari-mcp-",
  dir=/tmp)`, mode `0700`. Reject or relocate paths near the Linux `sun_path`
  limit (about 108 bytes including the trailing NUL); keep the socket name
  short and never under a deep workspace path.
* Spawn with `asyncio.create_subprocess_exec` (or `Popen` plus a waiter thread):
  `stdin=PIPE`, `stdout=PIPE`, `stderr=PIPE`, `start_new_session=True`, a
  sanitized environment, and no shell. The child's stdout/stderr are never
  inherited by the MCP process.
* Default child environment: `SDL_VIDEODRIVER=dummy`, `SDL_AUDIODRIVER=dummy`,
  `HATARI_TOS`/profile paths as configured. `display=true` removes the dummy
  video override but is not used by CI.
* Launch defaults: `--control-socket <short path>`, `--sound off`,
  `--confirm-quit off`, `--configfile <private generated cfg>`,
  `--log-level info`. Do **not** pass `--disable-video`; rendering must stay on
  for P1 screenshots. Do not pass `--fast-forward on` by default (it changes
  input timing).
* Wait for the listener to accept Hatari's connection with the startup timeout
  (default 30 s). The stock socket is connected during Hatari startup, before
  TOS is fully booted; P0's `emu_start` therefore means "socket accepted and
  emulator process alive", not "desktop ready".
* Stop ladder: if state is `at_prompt`, write `quit` on stdin; otherwise send
  `hatari-debug quit` on the socket. Wait gracefully (5 s), then terminate the
  process group with `SIGTERM` (5 s), then `SIGKILL`. Always close the listener,
  close the accepted socket, unlink the socket, join reader threads, and reap
  the child. `emu_stop` is idempotent.
* Register cleanup in the MCP lifespan and in a `finally`/atexit path so an
  engine disconnect cannot leak a Hatari process or socket. Never leave
  orphaned process groups.
* `status()` reports `pid`, `state`, `since`, `uptime_ms`, `returncode`,
  `profile`, `machine`, `socket`, `program_exit_code` (None in P0),
  `last_error`, and ring-buffer cursors.

### 3. Channels and state machine

Stock Hatari has two non-interchangeable command paths:

| State | Writable channel | Commands |
| --- | --- | --- |
| `running` | control socket | `hatari-debug`, `hatari-event`, `hatari-option`, `hatari-shortcut`, `hatari-stop`, `hatari-cont` |
| `remote_paused` | control socket | same, including `hatari-debug` inspection and `hatari-cont` |
| `at_prompt` | child stdin | raw debugger commands (`cpureg`, `memdump`, `step`, `next`, `cont`, `quit`, ...) |
| `stopping`/`exited`/`crashed` | none | calls fail with `emulator_gone` or `not_running` |

The state machine is explicit:

```
starting -> running          socket accepted
starting -> failed           spawn error, missing TOS, startup timeout
running  -> remote_paused    hatari-stop accepted
remote_paused -> running     hatari-cont accepted
running  -> at_prompt        breakpoint/exception/NF_DEBUGGER line observed
remote_paused -> at_prompt   breakpoint/exception observed (possible)
at_prompt -> running         cont/quit accepted, or "Returning to emulation"
any      -> stopping         emu_stop
stopping -> exited           clean child exit
any      -> crashed          child died unexpectedly or socket closed early
```

Rules:

* One async command lock across both channels. Hatari's debugger is not
  re-entrant and the control parser processes one buffer at a time.
* Every write is newline-terminated and kept under 3.5 KiB so the command plus
  a sentinel fits under `MAX_DEBUG_CMD_LEN` (4096). Reject longer commands with
  `command_too_long`; never split a debugger command across writes.
* While the debugger is at its prompt it blocks in `DebugUI_GetCommand()` on
  **stdin**; `Control_CheckUpdates()` is not called. A socket command written at
  that moment is queued and will not be seen until the debugger exits. Never
  send a socket command when the state is `at_prompt`.
* `step` and `next` return to the prompt; `cont`, `quit`, and `reset` leave it.
  Do not queue a sentinel after a command that leaves the debugger: the stale
  line is consumed at the next entry.
* If the state is unknown, return `debugger_state_unknown` with a hint to use
  `dbg_status`/`emu_status`; never write speculatively to stdin.
* To confirm the prompt, use a nonce probe: write
  `echo __HMCP_PROBE_<uuid>__\n` to stdin and wait for that line on stderr. If
  it appears, the prompt is active. If it does not, the line stays harmless in
  the pipe and is ignored when it eventually appears. A probe is the only safe
  way to disambiguate "breakpoint message seen" from "readline is ready".
* Socket closed/EOF: transition to `crashed` (if the child is still alive) or
  `exited`; fail in-flight calls with `emulator_gone`. Do not attempt an
  implicit restart in P0.

### 4. Demuxer, sentinel, and pager hazard

Hatari output channels are not cleanly framed:

* stdout carries TOS/xconout output (`--conout 2` redirects VT-52 console output
  to the host stdout) and may also carry the readline `> ` prompt;
* stderr carries debugger output, Hatari logs, traces, NatFeats messages, and
  breakpoint/exception notices.

The demuxer therefore:

* runs one reader per pipe with incremental line splitting, CRLF handling,
  bounded partial-line buffers, and a maximum line length;
* classifies lines as `console`, `prompt`, `debugger`, `log`, `trace`,
  `natfeats`, `breakpoint_hit`, `exception`, `sentinel`, `pager_prompt`, or
  `unknown`;
* dispatches sentinel lines to the waiting command without putting them in user
  buffers;
* keeps bounded ring buffers (default 4 MiB per stream, configurable) with
  monotonic sequence numbers and timestamps; `log_read(since=...)` and
  `console_read(since=...)` never block;
* updates the state machine from breakpoint/exception lines and from
  `Returning to emulation...`, but treats that as a hint, not proof;
* publishes `debugger_entered`, `breakpoint_hit`, `exception`, `exited`, and
  `program_exit` events for later phases.

Response correlation for a non-terminal debugger command:

1. choose the channel from the state;
2. for socket: write `hatari-debug <command>\nhatari-debug echo
   __HMCP_<uuid>__\n` in one small write (or two writes that stay ordered);
3. for stdin: write `<command>\necho __HMCP_<uuid>__\n`;
4. collect classified stderr lines until the sentinel line; strip the sentinel
   and echo expansion noise; return the rest as `raw` plus parsed fields;
5. on timeout, stop waiting, mark the command timed out, retain the collected
   lines, and keep the sentinel registered briefly so late output is discarded
   rather than attributed to the next command.

Sentinel limitations are accepted in P0: log/trace lines can interleave, very
large dumps can contain a line that looks like the sentinel (use a UUID and
reject impossible collisions), and there is no error code. Success is inferred
from reaching the sentinel and from the absence of obvious `ERROR`/`Unknown
command` text; the raw text is always returned.

**Pager hazard (verified):** `memdump`, `disasm`, `find`, and `symbols` call
`DebugUI_DoQuitQuery()`, which prints a prompt and reads **stdin** after a page.
This happens even when the command arrived over the control socket, so a paged
command can block the emulator thread forever. P0 must generate a private
Hatari config with very large `nDisasmLines`, `nMemdumpLines`, `nFindLines`, and
`nSymbolLines` values (or otherwise prove paging is disabled) and must cap all
P0 output. The demuxer must classify the pager prompt as a hazard and fail the
call with `pager_blocked` rather than waiting forever. This is a P0 blocker, not
a P4 nicety, because a blocked `fgets(stdin)` also stops socket polling.

### 5. Readline / debugger-input spike (P0.3a)

The plan assumes a stock build may write the readline `> ` prompt to **stdout**
(the TOS console stream). Because the prompt has no newline, a naive line reader
can either hold console output indefinitely or leak `> ` into the console
buffer. The spike must answer:

1. Does a stock readline build accept commands from a pipe when stdin is not a
   TTY, without echoing input into stdout or blocking on terminal setup?
2. Where does the prompt appear (stdout vs stderr), and is it newline-terminated?
3. Does `echo` produce exactly one sentinel line, and does it work on both
   socket and stdin?
4. Does `step`/`next` followed by `echo` return the prompt and consume the
   sentinel on stdin?
5. Does a piped `quit`/`cont` leave the debugger without a stuck prompt?

Decision gate:

* Preferred: ship a minimal patch in `mcp/project/patches/` that uses the
  existing `fgets` branch (or `rl_outstream = stderr`) when stdin is not a TTY.
  This is a small, upstreamable change and makes the channel deterministic.
* Fallback: require/document a no-readline build and detect it via a startup
  capability probe. Do not silently support an ambiguous prompt.
* Last resort: robust prompt scanning in the demuxer, documented as fragile.
  This must not become the default without a measured test.

Record the result and the chosen option in
`doc/investigations/debugger-stdin-spike.md`; update this plan and the roadmap
open question 10.

### 6. Private Hatari configuration

Write a per-session config file under the private runtime directory (or the
workspace `.hatari-mcp/` directory) and pass it with `--configfile`. It must:

* disable confirmation dialogs (`bConfirmQuit = false` where applicable);
* disable paging (`nDisasmLines`, `nMemdumpLines`, `nFindLines`,
  `nSymbolLines` set high, e.g. 100000);
* set a stable number base and avoid host-specific paths;
* never overwrite the user's `~/.config/hatari/hatari.cfg` (no `--saveconfig` by
  default).

The exact INI keys live under `[Debugger]` and `[Log]`; verify them against
`src/configuration.c` before implementation. A missing or failed config file
must produce `bad_option`, not a partially configured run.

### 7. Profiles and precedence

Load TOML from `mcp/project/config/profiles/` (and a user directory), merge:

1. explicit tool arguments (`emu_start(profile=..., args=[...], tos=...)`);
2. `HATARI_MCP_PROFILE` and its profile file;
3. built-in defaults (headless, sound off, no dialogs, private socket).

`HATARI_BIN` overrides the binary in every case. `HATARI_MCP_FIXTURES`,
`HATARI_TOS`, `HATARI_MCP_WORKSPACE`, and `HATARI_MCP_STATE_DIR` are honored.
Validate the binary exists and is executable, the TOS exists and is readable,
the profile name is unique, and profile paths are absolute after expansion.
Reject unknown keys with a clear `bad_option`; do not silently ignore typos.
P0 does not apply runtime options; it only builds launch arguments.

### 8. Error model and result envelope

Stable codes (extend only with a documented reason):

`emulator_gone`, `not_running`, `already_running`, `startup_timeout`,
`startup_failed`, `missing_tos`, `missing_hatari`, `bad_option`,
`missing_fixture`, `socket_error`, `command_too_long`, `timeout`,
`debugger_state_unknown`, `not_stopped`, `parse_failed`, `pager_blocked`,
`output_too_large`, `invalid_path`, `path_not_allowed`, `internal_error`.

Every error includes a short `message`, a `hint` that tells the model what to
try next, and optional `detail`/`raw`. Examples: `missing_tos` -> "Set
HATARI_TOS or profile paths.tos to an EmuTOS/TOS image"; `emulator_gone` ->
"Call emu_status, then emu_start"; `timeout` -> "The machine may be stopped at
a breakpoint; call emu_status". Arguments are validated by Pydantic before the
handler runs; do not manually duplicate type validation.

Timeouts: 2 s for debugger commands, 10 s for state/media operations (P1), 30 s
for boot/startup. All waits are cancellable; no busy polling. On timeout, return
the collected output and do not leave a lock held.

### 9. Security, limits, and cleanup

* stdio only; no TCP listener, no attach to an existing emulator, no shell.
* Private `0700` socket directory; socket unlinked on stop and on process exit.
* `dbg_command` is explicitly privileged (the debugger is arbitrary code
  execution as the invoking user). Mark it destructive/open-world and document
  that it can run host commands through `setopt`/`parse`/`logfile`.
* P0 paths: binary, TOS, profile files, and log artifacts are resolved and
  checked. P1 adds full write confinement.
* Hard limits: command 3.5 KiB, one in-flight command, ring buffers 4 MiB,
  `log_read`/`console_read` max 1000 lines or 256 KiB per call, startup 30 s,
  debugger 2 s.
* Never commit generated configs, sockets, logs, ROMs, or state.

### 10. Verified stock-Hatari constraints to encode in tests

* Debugger prompt reads stdin; socket is not polled while stopped.
* `hatari-stop` sets a remote-pause flag; the socket remains responsive, but
  this is not the debugger prompt.
* `step`/`next` are debugger commands that return to the prompt; do not send
  them over the socket while the debugger is blocked on stdin.
* `cont`/`quit`/`reset` leave the debugger; do not queue a sentinel after them.
* The control parser assumes whole newline-delimited commands in one read and
  uses a 4096-byte buffer.
* `--disable-video` skips rendering and breaks screenshots; never use it for a
  session that may need screen capture.
* `--auto` takes an Atari path, not a host path. A host program can be started
  by making its directory the GEMDOS HD and passing the basename, or by mapping
  a host path to `C:\...` (P1).
* `--conout 2` sends VT-52 console output to host stdout; readline may also use
  stdout.
* `screenshot` is a debugger command; the UI shortcut uses a configured
  directory and is not a path-controlled substitute.

## Task checklist

### P0.1 — Package and toolchain

- [ ] Add `pyproject.toml` with `requires-python = ">=3.11"`, `mcp>=2.3,<3`,
      `pydantic>=2`, dev extras (`pytest`, `pytest-asyncio`, `ruff`), and
      `[project.scripts] hatari-mcp = "hatari_mcp.server:main"`.
- [ ] Add `src/hatari_mcp/__init__.py`, `__main__.py`, and an import-safe
      `server.py` skeleton.
- [ ] Configure `ruff` (line length 100, target 3.11) and `pytest`
      (`asyncio_mode = "auto"`, test paths).
- [ ] Document the `uv sync` / `uv run hatari-mcp` workflow; verify a clean
      install in a fresh virtual environment.
- [ ] Add a `scripts/ci.sh` that runs lint, unit tests, and integration tests
      when fixtures are present.

### P0.2 — Supervisor

- [ ] Implement `socketdir.py` with `0700`, short-path, ownership, and cleanup
      checks; unit-test the path-length rejection.
- [ ] Implement `supervisor.start()`: validate config, create listener, spawn
      child with pipes and a new session, accept the socket with timeout.
- [ ] Implement `supervisor.stop()` with the `quit` -> SIGTERM -> SIGKILL
      ladder, idempotency, reaping, reader join, and socket unlink.
- [ ] Implement `supervisor.status()` and child-exit detection; transition to
      `exited`/`crashed`.
- [ ] Ensure `emu_stop` during `starting`, `at_prompt`, `remote_paused`, and
      `running` is safe.
- [ ] Add lifespan/atexit cleanup and a test that kills the server and verifies
      no child process or socket remains.

### P0.3 — Dual control channel and state machine

- [ ] Implement `state.py` with the transitions above and a single lock.
- [ ] Implement `channels.py` with socket and stdin writers, newline and length
      validation, and per-command timeouts.
- [ ] Implement sentinel correlation for `hatari-debug` commands on the socket
      and raw debugger commands on stdin.
- [ ] Implement `probe_prompt()` using a harmless `echo` nonce; use it before
      the first stdin command and after ambiguous transitions.
- [ ] Handle closed socket, child exit, and stale sentinels without hanging.
- [ ] Do not send a sentinel after `cont`, `quit`, or `reset`; special-case
      terminal commands.
- [ ] Implement the pager guard: generated config disables paging; detect the
      pager prompt and fail with `pager_blocked`.

### P0.3a — Debugger-input spike

- [ ] Run the spike against the local build with a real EmuTOS image.
- [ ] Record prompt stream, newline behavior, stdin acceptance, echo/sentinel
      behavior, step/next behavior, and `cont`/`quit` behavior.
- [ ] If needed, add the minimal non-TTY readline patch under
      `mcp/project/patches/` and test both patched and unpatched builds.
- [ ] Write `doc/investigations/debugger-stdin-spike.md` with the decision and
      update roadmap open question 10.

### P0.4 — Demuxer

- [ ] Implement per-pipe readers with incremental line splitting and a partial
      line cap.
- [ ] Implement classification rules and fixtures for each class.
- [ ] Implement bounded ring buffers with sequence cursors and `since` reads.
- [ ] Implement sentinel dispatch from stderr to the active waiter.
- [ ] Publish state hints/events without blocking tool handlers.
- [ ] Add a recorded-stream corpus and a replay test.

### P0.5 — Errors, results, timeouts

- [ ] Implement `errors.py` and `result.py`; map `OSError`, `TimeoutError`,
      process exit, and parse failures to stable codes.
- [ ] Add a `@tool_guard` wrapper that logs unexpected exceptions and returns a
      generic envelope without leaking internals.
- [ ] Ensure every awaited operation has a timeout and releases the command
      lock on cancellation.
- [ ] Add tests for each error code and hint.

### P0.6 — Profiles and precedence

- [ ] Implement TOML profile loading and validation.
- [ ] Implement precedence exactly as in `config/README.md`.
- [ ] Reject unknown keys, missing TOS, non-executable Hatari, and duplicate
      profile names with `bad_option`/`missing_tos`.
- [ ] Add unit tests for env/profile/argument combinations.

### P0.7 — Fixtures and CI harness

- [ ] Implement `fixtures.py` discovery via `HATARI_MCP_FIXTURES`,
      `HATARI_TOS`, and `tests/fixtures`.
- [ ] Add `fetch-fixtures.sh` with a pinned EmuTOS URL and SHA256; make it
      idempotent and network-optional for unit-only runs.
- [ ] Add `make-fixtures.sh` for a blank floppy and a GEMDOS workspace
      directory; verify checksums.
- [ ] Ensure integration tests skip with a clear reason when fixtures are
      absent, and fail (not skip) when a fixture is present but corrupt.
- [ ] Confirm `.gitignore` prevents ROMs, images, states, and sockets.

### P0.8 — First tools

- [ ] `emu_start(profile?, args?, tos?, display?)`: idempotent, validates,
      starts, returns status; no false "desktop ready" claim.
- [ ] `emu_stop(force?)`: graceful ladder, idempotent, returns exit code and
      `socket_removed`.
- [ ] `emu_pause`: `hatari-stop`; reject at prompt with a useful hint.
- [ ] `emu_resume`: `hatari-cont`; handle running/paused idempotently.
- [ ] `emu_status`: state, pid, uptime, profile, returncode, last error.
- [ ] `log_read(level?, lines?, since?)`: stderr ring, classified entries.
- [ ] `console_read(lines?, since?)`: stdout ring, VT-52 flattened.
- [ ] `dbg_command(command, channel="auto", timeout_ms?)`: active-channel
      routing, privileged annotation, terminal-command handling.
- [ ] Register resources and add tool descriptions with failure hints.

### P0.9 — CI

- [ ] Add lint and unit jobs on Python 3.11 and 3.12.
- [ ] Add an integration job with EmuTOS and a built Hatari; do not commit
      ROMs; cache only permitted fixtures.
- [ ] Add a cleanup assertion that checks for leaked processes/sockets.
- [ ] Add a no-network unit path so contributors without fixtures can run CI.
- [ ] Make CI fail on warnings that indicate stdout pollution (MCP wire
      corruption) or unhandled tasks.

### P0.10 — Documentation and examples

- [ ] Write tool descriptions and one copy-pasteable example for start/pause/
      inspect/stop.
- [ ] Document every error code and hint.
- [ ] Add `hatari://docs/*` resources.
- [ ] Update `doc/tools-reference.md` only for implemented P0 tools.
- [ ] Update `mcp/README.md` if the package layout or CI commands change.

## Acceptance criteria

1. `uv sync` and `uv run hatari-mcp` work from a clean checkout; `ruff` and
   `pytest` pass.
2. The server speaks MCP v2 over stdio, lists the P0 tools/resources, and never
   writes non-MCP data to stdout.
3. `emu_start` launches the configured Hatari headless with a private socket,
   `SDL_VIDEODRIVER=dummy`, no dialogs, and rendering enabled.
4. `emu_pause`/`emu_resume` work and the control socket remains responsive while
   remotely paused.
5. `dbg_command("info")` returns raw, classified debugger text through the
   sentinel path; `dbg_command` at the debugger prompt uses stdin, not the
   socket.
6. `log_read` and `console_read` return bounded, classified, non-blocking
   results with working `since` cursors.
7. `emu_stop` is idempotent and removes the socket; no child process, process
   group, reader thread, or socket file is left after stop or server exit.
8. Missing TOS, missing binary, bad profile, startup timeout, emulator crash,
   command timeout, and closed socket return the documented typed errors with
   hints; no call hangs.
9. The P0.3a spike has a recorded decision; the chosen implementation is tested
   on the supported build.
10. A debugger command that would page is either impossible (paging disabled) or
    fails fast with `pager_blocked`; it never blocks the emulator on stdin.
11. CI runs lint, unit, and one real-Hatari integration test without a
    proprietary ROM.
12. No secrets, ROMs, state files, or sockets are committed.

## Test plan

### Unit tests (no emulator)

`tests/unit/`:

* `test_config.py`: env/default/profile precedence, `HATARI_BIN`, timeout
  defaults, invalid values.
* `test_profiles.py`: TOML parsing, unknown keys, duplicate names, missing TOS.
* `test_paths.py`: traversal, symlink escape, short socket path, private mode.
* `test_state_machine.py`: every allowed/forbidden transition; unknown state.
* `test_demux.py`: line splitting (partial, CRLF, long), classification of
  console/debugger/log/trace/NatFeats/breakpoint/exception/pager/sentinel,
  ring bounds and `since`.
* `test_sentinel.py`: token extraction, collision rejection, timeout cleanup,
  no sentinel for terminal commands.
* `test_ring.py`: byte/line caps, sequence monotonicity, concurrent reads.
* `test_errors.py`: code/hint mapping, exception wrapping, no internal leakage.
* `test_result.py`: success and failure envelope shape, `is_error` behavior.
* `test_socketdir.py`: `0700`, cleanup, rejection of overlong paths.

### Integration tests (real Hatari, EmuTOS)

`tests/integration/`; skip with a clear reason if `HATARI_BIN` or `HATARI_TOS`
is absent:

* `test_start_stop.py`: start headless, confirm socket accepted, status,
  stop, socket removed, no child.
* `test_channels.py`: `hatari-debug info` over socket; breakpoint/exception
  entry and a stdin command; verify the socket is not used at the prompt.
* `test_logs.py`: TOS console on stdout, debugger/log on stderr; `console_read`
  does not contain debugger output.
* `test_failures.py`: missing TOS, bad option, nonexistent binary, startup
  timeout (fake child that never connects), crash (fake child that exits).
* `test_cleanup.py`: SIGTERM the server; verify child group and socket cleanup.
* `test_pager_guard.py`: a deliberately paging command fails fast or is
  prevented by config.

Use short timeouts and a per-test private runtime directory. Never share a
socket or child between tests.

### Corpus tests (recorded streams)

`tests/corpus/`:

* Record real stdout/stderr from: boot with EmuTOS, `hatari-debug info`,
  `cpureg`, a breakpoint hit, an exception, a NatFeats `NF_STDERR` line, an
  `NF_EXIT`, a pager prompt, and a closed socket.
* Store only text/JSON, never ROMs or copyrighted data.
* Replay each stream through the demuxer and assert classifications, state
  hints, sentinel dispatch, and ring cursors.
* Add a regression test for a line containing a sentinel-like string.

### Protocol/E2E tests

* Launch the server as a subprocess and use the official MCP client (or a raw
  JSON-RPC stdio client if the v2 test client is unstable) to `initialize`,
  `tools/list`, call `emu_status`, and read `hatari://server/status`.
* Assert stdout contains only MCP frames and logs are on stderr.
* Assert a failed tool returns `is_error=True` with a structured envelope.

### CI matrix

* Lint/unit: Python 3.11 and 3.12, no emulator, no network.
* Integration: Python 3.12, locally built Hatari, fetched EmuTOS with checksum,
  generated workspace; no proprietary ROM.
* Optional nightly: patched and unpatched Hatari to keep the P0.3a fallback
  honest.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Readline writes `> ` to stdout without a newline | Console/demux corruption; ambiguous prompt | P0.3a spike; prefer minimal `!isatty()`/`rl_outstream` patch; test both builds |
| Debugger prompt blocks on stdin | Socket commands queue indefinitely | State machine + probe; never write socket at prompt |
| Debugger pager reads stdin from a socket command | Emulator thread hangs; all sockets stop | Private config disables paging; `pager_blocked`; cap output |
| Control parser assumes whole commands in one read | Split/merged commands misparse | Keep writes small and ordered; one command in flight; tests |
| No response framing on the control socket | Sentinels can interleave with logs | Classified demuxer; UUIDs; raw passthrough; P7 framed protocol |
| AF_UNIX path length / permissions | Startup fails or insecure socket | Short runtime directory; `0700`; length checks; unlink |
| Child process leaks on crash/server exit | Orphans and stale sockets | Process group, lifespan cleanup, SIGTERM/SIGKILL, cleanup tests |
| MCP SDK v2 API churn | Import/runtime breakage | Pin `>=2.3,<3`; import test; avoid private APIs |
| No proprietary ROM in CI | Integration coverage gap | EmuTOS fixture; skip cleanly; optional local TOS |
| Fixture download unavailable | Flaky CI | Cache by checksum; unit-only path; clear skip |
| `--auto` host-path confusion | Autostart fails or wrong drive | Treat as Atari path; map in P1; test with GEMDOS HD |
| Tool errors look successful | Model retries incorrectly | Explicit `CallToolResult(is_error=True)` with envelope |

## Work log

### Done

* 2026-10-27, planning subagent: authored this plan after reading the roadmap,
  architecture, tools reference, `scripts/ahcc-cli.py`, `mcp/README.md`, and
  the relevant Hatari sources (`control.c`, `debugui.c`, `debugcpu.c`,
  `breakcond.c`, `natfeats.c`, `options.c`, `inffile.c`).
* 2026-10-27, planning subagent: verified the MCP SDK v2 import path and
  `MCPServer`/`Context`/`ToolAnnotations` surface against the v2 documentation.
* 2026-10-27, planning subagent: recorded stock-Hatari constraints, including
  the pager-stdin hazard and the readline stdout prompt, as P0 blockers.

* 2026-10-27, reconciliation: P0.2/P0.3/P0.4/P0.6 process supervisor,
  dual channel and stdin prompt path, demuxer/sentinel, private socket
  directory, profiles and the first lifecycle/log/debug tools are implemented.
  Live start/stop, SIGSTOP pause/resume, breakpoint at prompt, stdin register
  set/step/resume and clean stop were verified; the final full suite is
  452 passed, 1 skipped.

### Remaining / next steps

* P0.9 CI acceptance is unmet: there is no MCP `scripts/ci.sh` or CI job that
  runs lint, unit tests and a real-Hatari integration test.
* P0.3a's required investigation note (`doc/investigations/debugger-stdin-spike.md`)
  is still absent even though the chosen stdin/prompt path is exercised live.
* Keep the documented stock-Hatari limits visible: no framed handshake in P0;
  paging is handled by the private config/fail-fast guard rather than a patch.

### Next

1. Add the MCP CI job (lint + unit + fixture-gated real-Hatari integration) and
   record the first green run.
2. Write the P0.3a investigation note from the verified prompt/stdin behavior
   and close roadmap open question 10.
3. Leave the core supervisor/channel surface stable for P1–P8; do not add
   protocol-native claims without the P7 handshake.

## Adversarial review

The requested independent review via the Task tool with `subagent_type:
"general"` could not be performed: the Task tool is not exposed in this session
(the available tools are shell, file, search, web, and Firecrawl tools only).
This is recorded as a process gap rather than silently skipped. A structured
adversarial self-review was performed against the roadmap, architecture,
tools reference, and Hatari source; findings were incorporated below.

| Finding | Severity | Adjustment |
| --- | --- | --- |
| The docs/example treat `_vbl` as a builtin debugger symbol; Hatari's builtin is the `VBL` counter and the TOS vector at `$70`. | High (P2, but affects assumptions) | P2 uses `VBL`/`($70)` and a spike; P0 does not depend on `_vbl`. |
| The debugger pager (`DebugUI_DoQuitQuery`) reads stdin even for socket commands, so paging can deadlock the emulator and stop socket polling. | High | Added private-config paging disable, `pager_blocked`, and a P0 test. |
| Readline may write the prompt to stdout, which is also the TOS console; a line reader can corrupt or stall. | High | P0.3a is a blocking spike; patch/no-readline fallback is required. |
| `step`/`next` over the socket can leave a sentinel unread and block at the prompt. | High | Step/next are only allowed at `at_prompt` on stdin in P1; P0 exposes raw `dbg_command` with routing rules. |
| `--auto` does not accept an arbitrary host path; it expects an Atari path. | High | P1 maps workspace files to guest paths or uses the GEMDOS HD basename form. |
| `cont`/`quit`/`reset` never reach a sentinel. | High | Explicit terminal-command handling; no sentinel queued. |
| MCP v2 renamed `FastMCP`; importing `mcp.server.fastmcp` fails. | High | Pin `mcp>=2.3,<3`; use `mcp.server.mcpserver.MCPServer`. |
| Returning a plain error string looks successful to MCP clients. | Medium | Explicit `CallToolResult(is_error=True)` with a structured envelope. |
| `--disable-video` would silently break P1 screenshots. | Medium | Encoded as a never-use constraint and a config test. |
| AF_UNIX paths are length-limited and a deep workspace path can fail. | Medium | Short private runtime directory; length checks. |
| `NF_STDERR` writes raw bytes without a newline and can be confused with logs. | Medium (P1) | P0 only classifies; P1 buffers/attributes with a documented caveat. |
| The control parser buffers 4096 bytes and assumes whole commands. | Medium | 3.5 KiB limit, ordered writes, one in-flight command. |
| CI cannot use proprietary TOS. | Medium | EmuTOS fixture plus clean skips; no committed ROMs. |
| A second `emu_start` could spawn a second child. | Medium | Idempotent start; one-child rule and test. |

Cross-plan note: `P4-debugger-depth.md` §P4.10 currently says Hatari's debugger
has no built-in pager. That is incomplete: `DebugUI_DoQuitQuery()` is an
internal page/continue prompt and reads **stdin** after a page, even when the
command arrived on the socket. P0/P1/P2 treat this as a deadlock hazard and
disable paging in the private config. The P4 owner should reconcile its wording
and failure code (`pager_blocked`/`pager_or_prompt_blocked`) with this finding;
P0 does not depend on P4.

No finding required dropping a roadmap item. The P0 scope remains the roadmap
P0.1–P0.10 gate, with P0.3a and the pager guard treated as hard prerequisites.
