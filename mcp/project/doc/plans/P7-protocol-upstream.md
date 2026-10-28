# P7 — Protocol and upstream hardening

Status: partial — protocol libraries and server-side P7.7/P7.8/P7.10 implemented; P7.1 framed transport is unwired and Hatari patches 0001–0008 are not applied; latest fix pass under final review
Owner: P7 implementer (implementation subagent)
Reviewer: independent adversarial reviews completed (round 1 and round 2); latest fixes under final review
Last updated: 2026-10-27
Roadmap: P7.1–P7.10

Depends on: P0.2–P0.4 (supervisor/channel/demuxer), P1.15–P1.17
(breakpoint/step control), P2.4/P2.10 (pixels/input), P4.1–P4.7 (debugger
depth), P5.5–P5.7 (serial/SCC streams), P6.5/P6.8–P6.9 (matrix,
determinism, replay)
Feeds: P8.1, P8.2, P8.3, P8.6, P8.7, P8.12

## Goal

Remove the sentinel and text-parsing hacks without making a patch mandatory.
The server must run against **stock Hatari** with the P0–P6 tool surface and
against an **optional patched build** with framed request/response,
notifications, binary readback, frame-locked input, and a deterministic clock.
The same MCP tool names and result schemas must work in both modes; differences
must be visible as explicit capabilities, never as silent semantic changes.

P7 also hardens the process lifecycle (heartbeat, bounded restart, orphan
cleanup) and adds an optional authenticated HTTP/SSE transport. The debugger is
equivalent to arbitrary code execution; the HTTP transport is therefore a
security feature with a threat model, not a convenience checkbox.

## Dependencies

| Dependency | Need | Hard? | Notes |
| --- | --- | --- | --- |
| P0.3a debugger-input spike | stdin is usable for prompt commands | Hard | If the readline path cannot be made non-TTY-safe, P7.1b's prompt polling is the fix; do not assume it works. |
| P0.4 demuxer | Legacy fallback and notification inference | Hard | The fallback must remain after the framed path lands. |
| P1.15–P1.17 | Breakpoints, step, wait | Hard | Notification/step semantics build on these. |
| P2.4/P2.10 | Pixel access, input scripting | Hard | Binary framebuffer and injection need equivalent observable behavior. |
| P6.5/P6.8–P6.9 | Matrix, determinism, replay | Hard for validation | P7 must be exercised in both stock and patched modes. |
| Hatari source tree + build deps | Patch development | Hard | Pinned commit; SDL2/SDL3, readline/no-readline builds. |
| Windows CI/host | P7.6 verification | Soft (gated) | If unavailable, Windows is documented unsupported and not advertised. |
| No new Python runtime deps | Protocol/HTTP | Policy | Use stdlib (`socket`, `struct`, `asyncio`, `hmac`, `http.server`/ASGI already chosen); do not add a second web framework. |

## Deliverables and file ownership

All new server code lives under `mcp/project/src/hatari_mcp/`. P7 owns the
protocol package and supervisor extensions; it may modify `channel.py`,
`supervisor.py`, and tool adapters through their public interfaces.

### Server-side deliverables (under `src/hatari_mcp/`)

| Path | Purpose |
| --- | --- |
| `src/hatari_mcp/protocol/__init__.py` | Protocol version and exports. |
| `src/hatari_mcp/protocol/frame.py` | 16-byte frame header, length limits, encode/decode, partial-read state machine. |
| `src/hatari_mcp/protocol/codec.py` | JSON control payloads and binary payload metadata; strict type validation. |
| `src/hatari_mcp/protocol/hello.py` | HELLO/HELLO_ACK, version range, capability negotiation, startup banner parsing. |
| `src/hatari_mcp/protocol/client.py` | Async framed client: request ids, future map, deadlines, cancellation, backpressure. |
| `src/hatari_mcp/protocol/notifications.py` | Notification types, sequence numbers, coalescing/drop policy, delivery to MCP events. |
| `src/hatari_mcp/protocol/capabilities.py` | Canonical capability names, feature→fallback matrix, `emu_status` projection. |
| `src/hatari_mcp/protocol/legacy.py` | Sentinel/stdin adapter implementing the same internal interface as the framed client. |
| `src/hatari_mcp/protocol/selector.py` | Auto/legacy/framed selection, probe, strict-mode refusal, diagnostics. |
| `src/hatari_mcp/protocol/errors.py` | Wire error codes mapped to MCP errors and hints. |
| `src/hatari_mcp/channel.py` (modify) | Route commands through the negotiated transport; preserve per-command lock and state machine. |
| `src/hatari_mcp/supervisor.py` (modify) | Heartbeat integration, restart policy hooks, capability reporting. |
| `src/hatari_mcp/supervisor/heartbeat.py` | Process poll + framed PING/PONG, unresponsive state, thresholds. |
| `src/hatari_mcp/supervisor/recovery.py` | Bounded restart, backoff, autosave/restore policy, crash-loop detection, quarantine. |
| `src/hatari_mcp/framebuffer.py` | Native framebuffer metadata + RGBA conversion; no host PNG round-trip. |
| `src/hatari_mcp/input/framelock.py` | Frame/VBL-tagged event queue, absolute mouse, joystick axes/buttons, release-all. |
| `src/hatari_mcp/clock.py` | Pause/step/run-frames, virtual clock, frame boundary semantics, fallback. |
| `src/hatari_mcp/transport/windows_pipe.py` | Named-pipe server/client for Windows, ACLs, overlapped I/O. |
| `src/hatari_mcp/transport/http_sse.py` | Optional MCP HTTP/SSE transport, sessions, event replay, shutdown. |
| `src/hatari_mcp/transport/auth.py` | Bearer token loading, constant-time compare, origin/host checks, audit helpers. |
| `src/hatari_mcp/tools/system.py` | `emu_capabilities`, protocol status, strict-mode diagnostics. |
| `src/hatari_mcp/tools/input.py` (modify) | Frame-locked injection with legacy fallback. |
| `src/hatari_mcp/tools/screen.py` (modify) | Binary framebuffer/RAM readback with legacy fallback. |

### Patch, documentation, and test deliverables (outside `src/`)

| Path | Purpose |
| --- | --- |
| `patches/0001-framed-control-protocol.patch` | Framing, HELLO, request/response, startup banner, length limits. |
| `patches/0002-debugger-prompt-control-poll.patch` | Poll the control channel while at the debugger prompt (non-TTY path first). |
| `patches/0003-control-notifications.patch` | Debugger entry/exit, breakpoint, exception, NatFeats exit, screen size. |
| `patches/0004-binary-memory-framebuffer.patch` | Raw RAM/framebuffer responses with metadata. |
| `patches/0005-frame-stepping-virtual-clock.patch` | `clock.step`, frame boundaries, optional frozen RTC. |
| `patches/0006-direct-input-injection.patch` | Frame-tagged keyboard/mouse/joystick/absolute input. |
| `patches/0007-windows-named-pipe.patch` | Windows control channel. |
| `patches/0008-headless-render.patch` | Optional `--headless` that keeps rendering; only if P7.1 lands. |
| `patches/tests/` | Patch-level tests and build/apply checks. |
| `doc/adr/0002-framed-control-protocol.md` | Wire format, compatibility, security, fallback decision. |
| `doc/investigations/P7-prompt-poll.md` | readline/select experiment and chosen design. |
| `tests/unit/protocol/`, `tests/integration/protocol/`, `tests/e2e/test_protocol_modes.py` | Codec, negotiation, fallback, patched/stock behavior. |

## Design / constraints

### 1. Framed control protocol (P7.1) [P]

**Wire frame** (network byte order, 16-byte header):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | magic `HMCP` |
| 4 | 1 | protocol version (1) |
| 5 | 1 | type: `HELLO=1`, `HELLO_ACK=2`, `REQUEST=3`, `OK=4`, `ERR=5`, `NOTIFY=6`, `PING=7`, `PONG=8` |
| 6 | 1 | flags: `BINARY=0x01`, `MORE=0x02` (chunked), `NO_REPLY=0x04` |
| 7 | 1 | reserved, must be zero |
| 8 | 4 | request id (client-chosen, nonzero; echoed) |
| 12 | 4 | payload length |
| 16 | n | payload |

Limits: default maximum payload 8 MiB, absolute 64 MiB; request ids are not
reused until the response arrives; unknown types/versions are rejected with
`ERR protocol`. The decoder is a streaming state machine: partial headers,
partial payloads, coalesced frames, and EOF at any boundary are handled
without unbounded buffering. Length is validated before allocation.

Payloads:

* Control: UTF-8 JSON object `{"op": "...", "params": {...}}`; responses
  `{"ok": true, "result": ...}` or `{"ok": false, "error": {...}}`.
* Binary (`OK` with `BINARY`): `u32 meta_len`, `meta` JSON, then raw bytes.
  `meta` carries `space` (`cpu|dsp|ram|fb`), `address`/`offset`, `length`,
  `format`, `width`, `height`, `stride`, `bpp`, `endian`, `palette`, `vbl`,
  `frame`, and `sha256` for verification. No implicit conversion in the
  emulator; the server converts to RGBA/JSON for MCP.
* Errors: `{"code": "...", "message": "...", "hint": "...", "data": ...}`.
  Stable codes include `bad_request`, `unknown_op`, `unsupported`,
  `no_framebuffer`, `out_of_range`, `busy`, `queue_full`, `protocol`,
  `internal`.

**Handshake.** A patched Hatari writes exactly one startup line to stderr
when the control socket is enabled:

```text
HATARI-MCP-PROTOCOL 1 capabilities=framed,notify,binary,input,clock,frame nonce=<per-launch-hex>
```

The nonce is required: the server generates it per launch, passes it in
`HATARI_MCP_PROTOCOL_NONCE`, and rejects missing or mismatched values. Parsing
is all-or-nothing, and the banner window is bounded rather than closing at the
first socket connect; an invalid first line does not consume the window.

The server sees this line in the demuxer and sends `HELLO` with
`min_version=1`, `max_version=1`, client name/version, and requested
capabilities. `HELLO_ACK` selects a version, returns the capability set, and
reports Hatari version/commit/build flags and limits. The banner is required
before any binary frame is sent; legacy Hatari never receives non-text bytes.

This replaces the architecture's assumed `hatari-debug version` probe: Hatari
2.6.1 has no such debugger command. Do **not** send a binary HELLO to an
unpatched build; it would be parsed as a text command and pollute stderr.

**Framing while running.** The existing `Control_CheckUpdates()` loop reads a
buffer and calls `Control_ProcessBuffer()`. The patch keeps the legacy
newline path for compatibility and adds a framed path selected per
connection. One request is processed at a time; the debugger is not
re-entrant. Responses are written without blocking the emulation loop; a
bounded outbound queue holds responses/notifications. If the queue is full,
large reads fail with `ERR busy` (the client retries/chunks), critical
notifications are preserved, and coalescible notifications are dropped with a
counter.

**Framing while at the debugger prompt (P7.1b).** This is the hard part. The
stock debugger blocks in `DebugUI_GetCommand()` on stdin; the control socket is
not polled. The patch must poll both stdin and the control socket while
preserving readline behaviour:

1. When `stdin` is not a TTY (the MCP case), select the non-readline `fgets`
   path (the P0.3a `!isatty()` fix) and `poll()` on `{control socket, stdin}`.
   Socket frames become debugger commands; stdin lines remain supported. This
   is the primary, testable path.
2. When `stdin` is a TTY and a control client is connected, do not mix
   `select()` with readline's internal buffer. Preferred: a readline callback
   event loop (`rl_callback_handler_install` + `rl_callback_read_char`) that
   also drains the socket. Fallback: disable framed prompt polling in that
   mode and document it. Do not silently lose typed input.

The patch is split so P7.1a (running state) can land without P7.1b (prompt
state). Tests must cover both readline and no-readline builds. If upstream
rejects the callback complexity, ship only the non-TTY path and keep TTY
interactive use on stdin.

**Compatibility.** The legacy newline protocol remains; `hatari-*` commands
still work. A framed connection and a legacy connection are mutually exclusive
per socket. The patch must not change debugger output destinations or the
existing control commands.

### 2. Notifications (P7.2) [P]

Notification payloads have `seq` (monotonic per connection), `kind`, `vbl`,
`frame`, and type-specific fields:

| Kind | Fields | Delivery |
| --- | --- | --- |
| `debugger.entered` | reason (`breakpoint|exception|step|manual`), pc | Before the debugger blocks. |
| `debugger.left` | reason (`cont|step|quit|reset`), pc | After leaving. |
| `breakpoint.hit` | index, address, pc, condition | At the hit, before entry. |
| `exception` | vector, sr, pc, address | At entry. |
| `natfeats.exit` | code | On NF_EXIT. |
| `screen.size` | width, height, bpp, mode | Coalesced; latest wins. |
| `reset` | kind | On reset. |
| `state.loaded` | path hash | On state load. |
| `fatal` | message | Best effort before shutdown. |

Delivery rules: never block the emulator; non-blocking writes; bounded queue;
`debugger.entered`/`breakpoint.hit`/`exception`/`natfeats.exit` are never
silently dropped — on overflow, mark `overflow=true`, keep the connection
usable, and let the server resynchronize via `dbg_status`. `screen.size` and
duplicate state notifications may coalesce. Sequence gaps are visible to the
client and produce a `notification.gap` event, never a silent reordering.
Notifications are outbound only; they cannot inject commands.

The server maps notifications to MCP events/resources with the same names used
by the legacy stderr inference, so P1 tools do not branch on transport. When
legacy inference cannot distinguish a manual debugger entry from a
breakpoint, it reports `reason=unknown`; framed mode reports the truth.

### 3. Binary framebuffer and RAM readback (P7.3) [P]

Operations: `mem.read`, `mem.write` (optional; P1 already has text), `ram.read`,
`fb.meta`, `fb.read`. Parameters are validated (space, address, length,
alignment, max). Reads are chunked with `MORE` and a `sequence`/`total` in the
metadata; the server joins chunks only within its own cap. Byte order is
explicit; DSP and CPU spaces are separate. Reads outside guest address space
return `out_of_range`, never host memory.

Framebuffer: return native pixels plus palette/stride/bpp — not a PNG. If
rendering is disabled (`--disable-video`), return `no_framebuffer` with a hint
to use `SDL_VIDEODRIVER=dummy`. The server converts to RGBA for probes/diffs.
This removes the debugger `screenshot` → file → PNG decode round-trip for
pixel-heavy tools, while `screen_screenshot` remains for artifact files.

Security: no arbitrary host file paths in these ops; no pointer chasing; no
reads of emulator process memory.

### 4. Direct input injection (P7.4) [P]

`input.inject` carries an ordered array of events, each tagged with an absolute
guest VBL (`at_vbl`) or a relative frame offset (`+n`). Types:

* `key`: ST scancode(s), down/up, optional repeat count; text expansion stays
  in the server, never in the emulator.
* `mouse.abs`: guest screen x/y in the emulated visible area; optional button.
* `mouse.rel`: dx/dy, existing behaviour but frame-tagged.
* `joy`: port, x/y axes (−32768..32767 or normalized), buttons/fire.
* `gamepad`: optional host-gamepad mapping when enabled; otherwise rejected.

Events are queued and applied at the VBL boundary by the emulation loop, not
from the debugger or the control callback. Queue size is bounded;
`queue_full` is returned rather than silently dropping. On reset, state load,
stop, or mode change, the patch releases all injected keys/buttons to avoid a
stuck modifier. Absolute coordinates are clamped; the server documents guest
scaling, borders, and low-resolution doubling. Timestamps use guest frames,
not host wall time, so replay is reproducible. `input.clear` discards pending
events; `input.status` reports queue depth and the last applied VBL.

Host gamepads are read inside the emulator process (SDL) only when explicitly
enabled; the server never reads `/dev/input` directly. This is an input path,
not a general device bridge.

### 5. Frame stepping and deterministic clock (P7.5) [P]

Operations: `clock.state`, `clock.pause`, `clock.resume`,
`clock.step {frames}`, `clock.set {mode: host|virtual, epoch?}`,
`frame.run {frames}`. Semantics:

* `clock.step n` runs exactly `n` VBLs from a stopped state and returns at a
  frame boundary with `vbl`, `frame`, `pc`, and whether a breakpoint/exception
  interrupted it. A short count returns `partial=true`; it never hangs.
* The frame boundary is defined at VBL start. CPU execution for that frame
  completes before the stop. This definition is tested, not assumed.
* Virtual clock freezes the RTC at a configured epoch and removes host wall
  clock from guest-visible time. Stock Hatari exposes only `--rtc-year`; full
  date/time freezing requires this patch.
* Audio: stepping may suppress or truncate host audio; documents that stepping
  is not an audio-quality mode. Video/DMA: the patch must not skip frame
  rendering; screenshots at boundaries remain valid.
* Interaction with remote pause/debugger: `clock.step` is refused while the
  debugger is at its prompt; use `dbg_step` for CPU instructions. `frame.run`
  is distinct from `--run-vbls` (which exits the process).

Fallback without the patch: `_vbl` breakpoint plus `cont`/`run_frames` and
`--run-vbls` per process. It is slower, can overshoot, and cannot freeze the
clock; capabilities report `frame_step=approx`, `clock=host`.

### 6. Windows control channel (P7.6) [P]

The control socket is Unix-only. For Windows, the MCP server creates a named
pipe (`\\.\pipe\hatari-mcp-<random>`) before launch; Hatari connects as a
client, mirroring the Unix direction. Requirements:

* byte-mode pipe with the same HMCP framing; no message-mode assumptions;
* overlapped I/O and a poll/event loop compatible with the emulation loop;
* an ACL restricted to the current user (SDDL) and `PIPE_REJECT_REMOTE_CLIENTS`;
* no unauthenticated remote pipe; no TCP fallback;
* clean disconnect/reconnect semantics matching Unix;
* build integration in CMake and the Windows CI job.

In-process commands (embedding Hatari) are not pursued: ADR 0001 keeps the
server as an external process. If Windows CI cannot be provided, implement the
patch behind a build flag but **do not advertise Windows support**; mark P7.6
`partial/unverified` and keep it out of releases.

### 7. Capability negotiation and fallback (P7.7)

Canonical capabilities: `framed`, `framed_prompt`, `notify`, `binary`,
`fb_native`, `input_frame`, `input_abs`, `input_joy`, `frame_step`,
`clock_virtual`, `windows_pipe`, `headless_render`.

Selection:

| `HATARI_MCP_PROTOCOL` | Behaviour |
| --- | --- |
| `auto` (default) | Use framed when the banner and HELLO succeed; otherwise legacy. |
| `legacy` | Never send frames; sentinel/stdin path only. |
| `framed` | Require framed; fail startup with `protocol_unavailable` if absent. |
| `require-framed` | Alias for CI; also require `framed_prompt` for prompt-state tests. |

The selected mode and full capability set are returned by `emu_capabilities`
and `emu_status`. Tool adapters consult capabilities, not transport type.
Every degraded path is explicit:

| Feature | Framed | Stock fallback | User-visible difference |
| --- | --- | --- | --- |
| Debugger replies | Request/response, ids | `echo` sentinel + stderr | Legacy can time out on `cont`/breakpoint; errors are inferred. |
| Prompt commands | Socket poll | stdin pipe | Legacy needs a second channel and has no reply while running. |
| Breakpoint event | Notification | stderr classification | Legacy reason may be `unknown`, latency higher. |
| Framebuffer | Native bytes | debugger screenshot / savebin file | Legacy costs disk I/O and PNG decode; no sub-frame read. |
| RAM read | Binary chunks | `memdump` hex / `savebin` | Legacy is slower and has a 32 KiB practical cap. |
| Frame step | Exact VBL | `_vbl` breakpoint / `--run-vbls` | Legacy may overshoot; clock not frozen. |
| Input | Frame-tagged/absolute/joy | host-timed relative/keyboard joystick | Legacy replay is approximate. |
| Windows | Named pipe | none | Stock Windows is unsupported. |

Fallback must preserve typed errors and never hang: if the legacy sentinel is
missing, return `debugger_state_unknown` with collected output and a hint. The
legacy adapter remains tested after P7 lands; it is not deleted.

### 8. Crash-safe supervisor (P7.8)

Responsibilities:

* **Heartbeat:** process poll every 500 ms; framed `PING`/`PONG` every 2 s when
  available. Missed pongs move the state to `unresponsive`, not immediately
  dead; after 3 misses, capture what is possible (logs, last screenshot,
  artifact manifest), then terminate the process group.
* **Parent death:** Linux `PR_SET_PDEATHSIG(SIGTERM)`; Windows job object with
  kill-on-close; macOS/BSD fallback is a session-registry check plus a watchdog.
  The server must not leave orphan emulators when it is SIGKILLed.
* **Autosave:** optional periodic state snapshots (seconds or VBLs) written
  atomically to the workspace; off by default because state is large. This is
  the only "state preservation" guaranteed after a crash; a failed process
  cannot be snapshotted after death.
* **Restart:** bounded policy (e.g. 3 restarts per 10 minutes, backoff
  1/2/4 s). Never restart after a user-requested stop. Optional
  `restore_state` loads the last autosave; otherwise restart from the profile.
  If `clock_virtual`/`input_frame` are present, replay the input log from the
  autosave checkpoint; otherwise label the recovery `approximate`.
* **Crash loop:** after the budget, stop and return `crash_loop` with all
  artifacts; do not hide repeated crashes.
* **Orphan cleanup:** integrate with P6.13's registry; ownership token + PID
  start time; dry-run by default; never kill unowned processes.
* **Distinguish causes:** requested stop, guest exit, exception/crash, host
  OOM/kill, protocol failure. The report names the cause rather than guessing
  from exit status alone.

### 9. Upstream process (P7.9)

Patch series (each one purpose, based on a recorded Hatari commit):

| Patch | Depends on | Content |
| --- | --- | --- |
| 0001 framed control protocol | — | Frame codec, HELLO/ACK, banner, running-state requests, limits. |
| 0002 prompt control poll | 0001 | Non-TTY poll of socket+stdin; readline TTY strategy. |
| 0003 notifications | 0001 (0002 for prompt entry) | Event emission, queue, sequence, drop policy. |
| 0004 binary readback | 0001 | RAM/framebuffer ops, metadata, chunking. |
| 0005 frame/clock | 0001, 0002 | Step frames, virtual clock, boundary semantics. |
| 0006 direct input | 0001, 0005 | Frame-tagged input, absolute/joy, release-all. |
| 0007 Windows pipe | 0001 | Named pipe, ACL, overlapped I/O, build. |
| 0008 headless render | — | Optional `--headless` that renders without a window; keep only if upstream wants it. |

Process requirements: record base commit and regenerate rather than silently
rebasing; GPL-2.0-or-later headers; no new dependencies; coding style matches
Hatari; add or extend tests under Hatari's `tests/`; build with and without
readline, SDL2 and SDL3, Linux and macOS (Windows for 0007); run the existing
Hatari test suite; open the upstream discussion/PR before depending on a patch
in a release; include release notes and a compatibility matrix against the
pinned stable release, current main, and one older release.

If upstream rejects a patch, it stays optional and maintained on a recorded
base; do not fork Hatari. The stock fallback is the product default.

### 10. Optional HTTP/SSE transport and authentication (P7.10)

stdio remains the default; HTTP is opt-in (`--http 127.0.0.1:0`). Security
requirements:

* bind loopback by default; binding a non-loopback address requires an explicit
  flag, a token, and an explicit `allowed_hosts` list (refused at construction
  otherwise, because every Host header would fail the check);
* require `Authorization: Bearer <token>` on every request and SSE connection;
  load the token from `HATARI_MCP_HTTP_TOKEN` or a `0600` token file, or
  generate an ephemeral token and print it once to stderr (never to stdout);
  compare with `hmac.compare_digest`;
* never accept tokens in query strings or logs; redact headers and arguments;
* check `Origin`/`Host`, require `Content-Type: application/json`, and require a
  custom header for browser-originated requests to reduce CSRF/drive-by risk;
* TLS is terminated by a reverse proxy; do not pretend plain HTTP is safe off
  the loopback interface;
* per-session concurrency, request body/size caps, timeouts, rate limits, a
  bounded SSE replay ring with `Last-Event-ID`, a bounded per-session delivery
  queue (drop-oldest when no reader keeps up), and idle-session expiry/GC;
* one session maps to one emulator/supervisor context; no cross-session
  workspace or socket access;
* audit log of tool calls and protocol transitions (redacted), with an explicit
  note that the debugger is RCE-equivalent;
* graceful shutdown closes SSE streams and stops the emulator.

This does **not** make multi-user hosting safe. Multi-tenant isolation is P8.5
and an explicit non-goal for v1.

## What can be implemented without patching

| Area | Stock implementation | Limitation |
| --- | --- | --- |
| Protocol selection | Version from `hatari --version`; no banner means legacy; environment override | Cannot detect a patched build without the banner. |
| Legacy adapter | Sentinel + stdin state machine (P0/P1) | Timeouts and ambiguous failures remain. |
| Notifications | stderr classification (breakpoint/exception/NatFeats) | No reliable manual-entry reason; latency. |
| Binary-ish readback | `savebin` to a confined temp file, or capped `memdump` parse | Disk I/O, 32 KiB practical cap, slower. |
| Framebuffer | `screenshot` to a file + server-side PNG decode | No sub-frame or raw native read. |
| Frame control | `_vbl` breakpoint, `--run-vbls`, `cont` | Overshoot, no frozen clock. |
| Input | Keyboard/mouse events, keyboard-emulated joystick, corner-pin absolute helper | No frame locking, no true absolute mouse or joystick axes. |
| Crash safety | Heartbeat, parent-death, bounded restart, session cleanup, optional autosave | Cannot snapshot after a hard crash; crash-loop detection is heuristic. |
| HTTP/SSE + auth | Full transport-level implementation | Does not fix emulator protocol limitations; security must be tested. |
| Windows | None | Control socket/FIFO are Unix-only; do not claim support. |

## Task checklist

### P7.1 — Framed protocol [P]

- [ ] Write `doc/adr/0002-framed-control-protocol.md` with the wire format, limits, versioning, and fallback decision.
- [ ] Implement `protocol/frame.py` streaming encoder/decoder with partial/coalesced frames and size limits.
- [ ] Implement `protocol/codec.py` JSON control + binary metadata validation.
- [ ] Implement `protocol/client.py` request-id futures, deadlines, cancellation, and chunk reassembly.
- [ ] Implement `protocol/hello.py` banner parsing, HELLO/ACK, version range, capability parsing.
- [ ] Patch 0001: framed running-state requests, banner, bounded outbound queue, legacy compatibility.
- [ ] Spike and document prompt polling (`doc/investigations/P7-prompt-poll.md`); patch 0002 for the non-TTY path.
- [ ] Test readline and no-readline builds at the prompt.
- [ ] Fuzz the decoder (random/truncated/oversized frames, bad lengths, unknown types).

### P7.2 — Notifications [P]

- [ ] Patch 0003: emit debugger entry/exit, breakpoint, exception, NatFeats exit, screen size, reset/state.
- [ ] Implement `protocol/notifications.py`: sequence, coalescing, overflow marker, gap events.
- [ ] Map framed notifications and legacy inference to one internal event schema.
- [ ] Ensure no blocking writes and no emulator crash on client disconnect/EPIPE.
- [ ] Test ordering, gaps, slow client, and coalescing.

### P7.3 — Binary readback [P]

- [ ] Patch 0004: `mem.read`/`ram.read`/`fb.meta`/`fb.read` with validated ranges and chunking.
- [ ] Implement `framebuffer.py`: native metadata, palette, RGBA conversion, bounds checks.
- [ ] Compare bytes with `savebin` and decoded screenshots; test CPU vs DSP and endianness.
- [ ] Enforce per-response caps and `busy`/`out_of_range` errors; test oversized requests.

### P7.4 — Direct input injection [P]

- [ ] Patch 0006: frame-tagged queue applied at VBL; keyboard, relative/absolute mouse, joystick, optional gamepad.
- [ ] Implement `input/framelock.py`: validation, queue depth, `at_vbl`, `input.clear/status`.
- [ ] Implement release-all on reset/state load/stop; test stuck modifiers.
- [ ] Compare injected input with stock keyboard/mouse paths for observable behavior.
- [ ] Test determinism of a recorded input log in strict mode.

### P7.5 — Frame stepping and clock [P]

- [ ] Patch 0005: `clock.step`, `frame.run`, boundary semantics, optional virtual RTC.
- [ ] Implement `clock.py` with exact frame counts, `partial` results, and breakpoint interruption.
- [ ] Test that `step N` advances exactly N VBLs and that screenshots are stable at boundaries.
- [ ] Measure audio/video side effects; document that stepping is not an audio mode.
- [ ] Implement and label the `_vbl`/`--run-vbls` fallback.

### P7.6 — Windows [P]

- [ ] Patch 0007: named-pipe control channel, ACL, overlapped I/O, CMake integration.
- [ ] Implement `transport/windows_pipe.py` matching the HMCP framing and disconnect semantics.
- [ ] Add Windows CI (build + protocol smoke). If unavailable, mark `unverified` and do not advertise support.
- [ ] Test ACL isolation and remote-client rejection.

### P7.7 — Capability negotiation/fallback

- [ ] Implement `protocol/capabilities.py`, `protocol/legacy.py`, `protocol/selector.py`.
- [ ] Implement `HATARI_MCP_PROTOCOL=auto|legacy|framed|require-framed`.
- [ ] Expose `emu_capabilities` and include capabilities in `emu_status`.
- [ ] Build a table-driven test for every capability × mode × expected degraded error.
- [ ] Ensure every tool's result schema is identical in stock and patched modes.

### P7.8 — Crash-safe supervisor

- [ ] Implement `supervisor/heartbeat.py` with process poll + PING/PONG and `unresponsive` state.
- [ ] Implement parent-death handling (Linux `PR_SET_PDEATHSIG`, Windows job object, portable fallback).
- [ ] Implement optional autosave and atomic state writes.
- [ ] Implement bounded restart, backoff, crash-loop detection, and cause classification.
- [ ] Integrate P6 session registry; test SIGKILLed server, killed emulator, and unresponsive emulator.
- [ ] Test that a requested stop never triggers a restart.

### P7.9 — Upstream process

- [ ] Record the Hatari base commit and generate the patch series with `git format-patch`.
- [ ] Add Hatari-side tests for framing, prompt polling, notifications, binary read, frame step, input, Windows.
- [ ] Add build matrix: readline/no-readline, SDL2/SDL3, Linux/macOS; Windows for 0007.
- [ ] Write release notes and the compatibility matrix.
- [ ] Open the upstream discussion/PR via Hatari's documented contribution channel; record the outcome.
- [ ] Define the rebase cadence and supported base range if upstream rejects or delays.

### P7.10 — HTTP/SSE auth

- [ ] Implement `transport/http_sse.py` with JSON-RPC POST and SSE stream, session ids, replay ring.
- [ ] Implement `transport/auth.py` with bearer token loading, constant-time compare, origin/host checks.
- [ ] Bind loopback by default; require an explicit flag for any other bind.
- [ ] Add body/size/timeouts, rate limits, per-session emulator isolation, audit log.
- [ ] Test 401/403, missing/rotated token, SSE reconnect with `Last-Event-ID`, and redaction.
- [ ] Document the RCE threat model and that multi-user hosting is P8.5.

### Cross-cutting

- [ ] Update `doc/architecture.md` §4/§5 and `doc/tools-reference.md`; remove the false `hatari-debug version` probe.
- [ ] Add `tests/unit/protocol/`, `tests/integration/protocol/`, `tests/e2e/test_protocol_modes.py`.
- [ ] Add `tests/patches/` apply/build checks to CI.
- [ ] Run the P6 corpus matrix in both stock and patched modes.
- [ ] Run lint/type checks; no leaked sockets, pipes, or processes.

## Acceptance criteria

1. Stock Hatari 2.6.1-devel passes the P0–P6 smoke suite with the default
   `auto` mode; no binary frame is ever sent to it.
2. The patched build negotiates protocol v1 and reports capabilities; the same
   MCP tool calls work in both modes with identical outer schemas.
3. A breakpoint hit while running produces a notification and a subsequent
   `dbg_status` reflects the stop; a manual debugger entry is distinguishable
   from a breakpoint in framed mode.
4. At the debugger prompt, framed builds accept socket commands for `registers`,
   `memory`, `step`, `next`, and `cont` without requiring stdin; legacy builds
   still work through stdin.
5. Binary `mem.read` is byte-identical to `savebin` for a 1 MiB region; native
   framebuffer bytes decode to the same RGBA as the screenshot path.
6. `clock.step N` advances exactly N VBLs and returns at a frame boundary; a
   breakpoint during the step returns `partial=true` with the reason.
7. A recorded frame-tagged input log replays to identical screen hashes under
   strict mode; stock fallback is labeled approximate.
8. A `SIGKILL` of the server leaves no owned emulator process; an emulator
   killed with `-9` is detected, reported with a cause, and restart is bounded
   by policy.
9. HTTP is off by default; loopback bind + token works; missing/invalid tokens
   return 401; no token appears in logs; non-loopback bind requires explicit
   opt-in; SSE reconnect works with `Last-Event-ID`.
10. Patch series applies cleanly to the pinned base, builds with and without
    readline, and passes Hatari's tests; failure to apply is a CI error, not a
    silent fallback.
11. Windows support is either green in CI or explicitly documented as
    unsupported; no unverified Windows claim ships.
12. `emu_capabilities` accurately describes the running build; every degraded
    behavior has a test.

## Test plan

### Unit tests

| Area | Tests |
| --- | --- |
| Frame codec | `tests/unit/protocol/test_frame.py`: round-trip all types, partial header/payload, coalesced frames, EOF at boundaries, max/oversized lengths, bad magic/version/flags, fuzzed inputs. |
| Codec | `test_codec.py`: JSON types, unknown ops, binary metadata, palette/endian, malformed metadata, integer bounds. |
| Handshake | `test_hello.py`: banner parse, version range, capability set, unknown caps, mismatched versions, missing banner → legacy. |
| Client | `test_client.py`: request ids, out-of-order responses, timeouts, cancellation, duplicate ids, chunk join, `busy` retry. |
| Notifications | `test_notifications.py`: sequence, gaps, coalescing, overflow, slow client, EPIPE. |
| Capabilities | `test_capabilities.py`: feature × mode × fallback table; no silent upgrade/downgrade. |
| Legacy | `test_legacy.py`: recorded stderr sentinel streams, `cont` timeout, prompt-state switch. |
| Supervisor | `test_heartbeat.py`, `test_recovery.py`: pong loss, restart budget, crash loop, requested stop, autosave restore. |
| Input/clock | `test_framelock.py`, `test_clock.py`: VBL tags, queue bounds, release-all, exact step counts, partial step. |
| HTTP/auth | `test_auth.py`, `test_http_sse.py`: token compare, origin/host, 401, body caps, SSE ids, redaction. |
| Windows pipe | `test_windows_pipe.py` (unit-level framing only; integration gated). |

### Integration tests

| Test | Requirement |
| --- | --- |
| `tests/integration/protocol/test_patched_smoke.py` | Build/apply patch, launch, negotiate, run a small scenario. |
| `test_protocol_modes.py` | Same scenario in `legacy`, `auto`, `framed`; compare outer schemas and expected differences. |
| `test_prompt_control.py` | Breakpoint → register/memory/step/next/cont over socket; readline and no-readline builds. |
| `test_notifications.py` | Breakpoint, exception, NatFeats exit, screen size; assert ordering and no drops for critical events. |
| `test_binary_readback.py` | `mem.read` vs `savebin`; `fb.read` vs screenshot RGBA; DSP/CPU spaces; bounds. |
| `test_frame_clock.py` | Exact N-VBL step; breakpoint interruption; RTC freeze; screenshot at boundaries. |
| `test_input_injection.py` | Absolute mouse, joystick axes, frame-tagged replay; release-all after reset. |
| `test_crash_recovery.py` | Kill server/emulator, unresponsive PING, restart budget, no orphans. |
| `test_http_auth.py` | Loopback token flow, 401, SSE reconnect, redaction, non-loopback refusal. |
| `test_patch_build.py` | Apply series to pinned base; build readline/no-readline; run Hatari tests. |

### Corpus tests

Run the P6 Okami-Shell/FreeMiNT/GodLib scenarios in both legacy and framed
modes. Compare status and observables; framed runs may be stricter (exact
frame/input) but must not change a scenario's declared assertions. Record known
differences (e.g. legacy reason `unknown`, framebuffer timing) in the report
instead of masking them with retries. Corpus prerequisites still skip cleanly.

### End-to-end tests

`tests/e2e/test_protocol_modes.py` drives the MCP stdio server with a real
client in all three modes, asserts `emu_capabilities`, runs a breakpoint/step/
memory/screenshot flow, and checks that failure hints do not mention a channel
the build does not have. HTTP E2E runs only with the opt-in flag and an
ephemeral token.

## Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Prompt polling cannot be made safe with readline. | The central P7.1b promise fails on interactive builds. | Non-TTY `poll()` path first; readline callback option; split patches; document fallback. |
| Upstream rejects the series. | Maintenance burden; no framed path in releases. | Keep patches optional; stock fallback is the product; rebase on recorded bases; no fork. |
| Framed protocol diverges from legacy semantics. | Tools behave differently by build. | One internal event/command interface; table-driven capability tests; outer schema compatibility. |
| Binary readback trusts lengths/addressing. | Emulator memory disclosure or crash. | Validate ranges, caps, spaces; no host pointers; fuzz decoder and ops. |
| Notifications block the emulator or drop critical events. | Hangs or missed breakpoints. | Non-blocking queue, coalescing, overflow marker, resync via `dbg_status`. |
| Slow/absent client fills the outbound queue. | Emulation stalls or memory growth. | Bounded queue, `busy` for large reads, close on overflow; never block the loop. |
| Frame stepping is not cycle-exact. | Replay still diverges. | Define boundary semantics; test; label exactness; `--cpu-exact` option; measure. |
| Virtual clock conflicts with audio/RTC. | Guest timing changes. | Opt-in; document; test; keep host clock default. |
| Windows patch is unverified. | Broken claims and support burden. | Gate on Windows CI; otherwise mark unsupported and don't ship the claim. |
| HTTP transport exposes RCE. | Host compromise. | Off by default; loopback; token; origin checks; TLS proxy; audit; no multi-user. |
| Crash restart hides real bugs. | Flaky green runs. | Bounded restarts, crash-loop error, artifacts, never restart after requested stop. |
| Patch maintenance slows every Hatari release. | Stale patches. | Small one-purpose patches, CI apply/build, recorded bases, compatibility matrix. |
| `parent-death` is Linux-specific. | Orphans on macOS/BSD. | Session registry + watchdog; document; test on each supported OS. |

## Work log

### Done

- 2026-10-27 — planning subagent: read the roadmap, architecture, tool
  reference, patch policy, AHCC CLI, and MCP README.
- 2026-10-27 — planning subagent: verified that Hatari 2.6.1-devel has no
  `hatari-debug version` command, that `Control_SetSocket()` connects out, that
  `Control_CheckUpdates()` is called only from the GUI event loop, and that the
  readline and non-readline prompt paths differ.
- 2026-10-27 — planning subagent: wrote the wire format, fallback matrix,
  stock-only implementation table, patch series, and security constraints.
- 2026-10-27 — P7 implementer: added the `hatari_mcp.protocol` package
  (`__init__`, `frame`, `codec`, `hello`, `client`, `capabilities`, `legacy`,
  `selector`, `errors`). Streaming frame decoder handles partial/coalesced/
  truncated frames, validates lengths before allocation, rejects unknown
  magic/version/type/flags/reserved bytes, and fuzzes cleanly. JSON control,
  binary metadata (`u32 meta_len` + JSON + bytes), sha256/length checks,
  HELLO/ACK, request-id futures, deadlines, cancellation, out-of-order
  responses, duplicate detection, PING/PONG and chunk reassembly are covered.
- 2026-10-27 — P7 implementer: capability negotiation (`auto|legacy|framed|
  require-framed`), truthful stock fallback matrix, stable `emu_capabilities`
  schema, non-raising status projection, and a legacy adapter with explicit
  `unsupported` hints. Strict modes now refuse stock Hatari at startup with
  `protocol_unavailable` after the banner wait. `emu_capabilities` lives in `tools/protocol.py`; the
  existing `emu_status` now includes the same `capabilities` projection via
  `Supervisor.status()` (no `app.py`/`server.py` edit).
- 2026-10-27 — P7 implementer: supervisor hardening: 500 ms process polling,
  sentinel/framed PING/PONG heartbeat, 3-miss `unresponsive` state,
  crash/unresponsive/requested/guest/host-kill classification, Linux
  `PR_SET_PDEATHSIG` in `preexec_fn` plus parent-watch fallback, bounded
  restart/backoff/crash-loop policy (off by default), requested-stop
  protection, and atomic `autosave_state`/`atomic_write_bytes`. Added
  `unresponsive` to the lifecycle state. Breakpoint entry is also recognized
  from the `"You have entered debug mode"` welcome line to avoid false
  unresponsive transitions.
- 2026-10-27 — P7 implementer: optional stdlib HTTP/SSE transport
  (`transport/auth.py`, `transport/http_sse.py`): bearer token (constant-time),
  loopback default with explicit non-loopback opt-in, Host/Origin checks,
  mandatory JSON content type, body caps/timeouts, session ids, bounded SSE
  replay with `Last-Event-ID`, redacted logging, and 401/403 tests. This is a
  partial transport: JSON-RPC POST + SSE only, no full MCP streamable-HTTP
  state machine and no TLS.
- 2026-10-27 — P7 implementer: wrote ADR 0002 and
  `doc/investigations/P7-prompt-poll.md`, including exact `debugui.c` /
  `control.c` / `gui_event.c` observations and the non-TTY `poll()` patch
  outline. No Hatari C source or patch was modified.
- 2026-10-27 — P7 hardening after the first adversarial review:
  - NatFeats now defaults **off** in `app.start`, `emu_start`, and
    `Supervisor.start`. `run_prg` explicitly opts in because its contract needs
    NF_EXIT. With NatFeats on, guest stderr can forge any text, so debugger
    state transitions are not inferred from stderr; a banner is accepted only
    with the per-launch nonce passed through `HATARI_MCP_PROTOCOL_NONCE` (the
    later fix pass widened this to a bounded live-state window and lets a valid
    banner replace an invalid first line). Explicit `natfeats=true` is
    documented as a trust tradeoff.
  - A banner is advisory. Without a real `FramedClient` + HELLO/HELLO_ACK the
    report is `protocol_mode=banner-only` (or `legacy`), `negotiated=false`,
    every `native` value false; `capabilities`/`available` separate usable
    fallbacks. Strict `framed`/`require-framed` refuse even a banner without a
    completed handshake. Banner version must be exactly 1; empty/no-`framed`
    banners never enable framing; invalid banners produce
    `protocol_unavailable` in strict mode and diagnostics in auto.
  - `emu_pause` now uses SIGSTOP/SIGCONT instead of `hatari-stop` (which stops
    the control loop). `REMOTE_PAUSED` is excluded from heartbeat; resume is
    acknowledgement-based (SIGCONT then socket echo) before `RUNNING`; stop
    SIGCONTs a stopped child; debugger commands while paused fail with a
    resume hint instead of timing out. Live pause→status→resume→debug→stop-0
    is tested.
  - Invalid `HATARI_MCP_PROTOCOL` is validated before spawn; any spawn
    exception cleans runtime; stop turns a proc-less STARTING/RUNNING state
    terminal. Early child exit resolves the socket wait immediately, reports
    startup failure, and never schedules a restart during STARTING.
  - `unresponsive` is recoverable: probing continues on a slower cadence and a
    successful probe returns to `RUNNING`. Skipped probes are neither pongs nor
    misses; heartbeat counters reset on state transitions.
  - Chunk reassembly rejects non-pending ids, MORE-without-BINARY, oversized
    `total`/`total_length`, duplicate/gapped sequences, and tracks a running
    byte total; resolved request ids are quarantined against late duplicates.
  - Linux-only `preexec_fn`; macOS/BSD/Windows parent-death is documented as
    unsupported (heartbeat parent watchdog only). Decoder closes on EOF,
    validates request ids/flags before payload, and JSON recursion errors are
  typed. HTTP rejects ambiguous framing and closes early errors; sessions are
  bounded; SSE replay no longer duplicates queued events.
- 2026-10-27 — P7 implementer: remaining-findings fix pass (supervisor,
  protocol client, HTTP transport):
  - Requested stop stays clean. Stderr state transitions are gated while
    `_stopping`/STOPPING/terminal, `requested_stop` always classifies as
    `EXITED`, and `_stop_unlocked` normalizes a racing CRASHED back to EXITED
    with no error. Repro start→breakpoint pc+2→at_prompt→`cpureg d0=0x1234`
    →stdin step→resume→stop now ends `exited`/0/`requested_stop`.
  - Parent death uses `PR_SET_PDEATHSIG=SIGKILL` (SIGTERM cannot reach a
    SIGSTOPped child); the heartbeat reparent fallback also sends SIGCONT then
    SIGKILL. Verified a paused child dies when its server is `kill -9`ed.
  - `_start_locked` re-checks `proc.returncode`/exit event/terminal state after
    protocol enforcement and refuses RUNNING with `startup_failed`; `_set_state`
    never revives EXITED/CRASHED/FAILED.
  - Banner handling has a bounded 1.5 s window that also covers auto mode and
    a delayed banner arriving after the socket connects. `_banner_seen` is set
    only after a fully valid nonce-bound parse, so an invalid first banner is
    diagnostic and a later valid banner can replace it. Window expiry records
    `protocol_banner_error`. ADR/plan now document the nonce-required format.
  - Timed-out/cancelled/abandoned framed request ids are quarantined via
    `_release_pending`; late binary/chunk frames for a resolved id are ignored
    instead of closing the connection.
  - HTTP SSE sessions use a bounded drop-oldest queue (`max_queue`), idle
    session expiry/GC (`session_ttl`, touched by POST/SSE activity), and a
    session count accessor. Non-loopback binds now require an explicit
    `allowed_hosts` list at construction, so they no longer 403 every request.

### Verification (this task)

- `mcp/.venv/bin/python -m pytest mcp/project/tests/unit/protocol
  mcp/project/tests/integration/protocol mcp/project/tests/e2e/test_protocol_modes.py -q`
  → **135 passed** after the remaining-findings fix pass. New regressions cover
  late `Returning to emulation` during STOPPING, requested-stop exit
  classification/result, SIGKILL parent-death, post-connect child exit,
  invalid-then-valid and delayed banner windows, timed-out request-id
  quarantine/late chunks, bounded SSE queues, idle-session expiry, and
  non-loopback `allowed_hosts`.
- `mcp/.venv/bin/python -m pytest mcp/project/tests -q` →
  **Historical/superseded: 406 passed, 1 skipped, 5 failed** at the time of this
  pass (corpus FreeMiNT binaries unavailable). All five failures were in P6
  scenario replay/security expectations (`replay_untrusted`/
  `replay_unverified` and bundle validation) and were subsequently fixed. The
  current authoritative result is **452 passed, 1 skipped**; the P7 suites above
  are green.
- Live `/tmp/test_mcp_break.py`: start EmuTOS → breakpoint at pc+2 → `at_prompt`
  → `registers_set d0=0x1234` → stdin `step` → `emu_resume` → stop with
  `returncode=0`.
- Live `/tmp/hmcp_test_pausestop.py`: `running` → pause `remote_paused` →
  status stays `remote_paused` (not unresponsive) → resume `running` → stop
  `exited` / `0`. No P7 runtime directories or sockets remained; one older
  `/tmp/probe-*` Hatari process predates this task and was left untouched
  (unowned processes are never killed by policy).
- Live `/tmp/p7_smoke.py` (remaining-findings pass): start → breakpoint at
  pc+2 → `at_prompt` → stdin `cpureg d0=0x1234` → stdin `step` → resume → stop
  gives `exited`/0/`requested_stop`; the pause→resume→stop flow also gives
  `exited`/0. No P7 Hatari process or new runtime directory remained.
- Live `/tmp/p7_orphan_parent.py`: start → `emu_pause` (child state `T`) →
  external `kill -9` of the server; the stopped Hatari child was gone
  immediately (no stopped orphan).
- New suites: `tests/unit/protocol/` (frame/codec/hello/client/capabilities/
  selector/legacy/auth/http), `tests/integration/protocol/test_live_supervisor.py`
  (real EmuTOS start/stop/SIGSTOP-unresponsive and breakpoint→stdin
  step→cont), `tests/e2e/test_protocol_modes.py` (stdio MCP
  `emu_status`/capabilities and real start/stop). Real-emulator tests skip
  cleanly when `HATARI_BIN`/`HATARI_TOS` are absent.
- Real checks: start/stop leaves no runtime dir or socket; `SIGSTOP` reaches
  `unresponsive` in ~1.2 s with the short test thresholds and recovers/stops;
  a `pc` breakpoint enters the prompt, `cpureg`/`step` use stdin, and `cont`
  returns to `running`.

### Remaining / next steps

- [ ] No framed handshake: the banner window is fixed at 1.5 s and advisory;
      stock Hatari remains `legacy`/`banner-only`, `negotiated=false`, and all
      native capabilities false. Strict modes refuse without a real
      HELLO/HELLO_ACK.
- [ ] Hatari patches 0001–0008 are not applied (no C source modified), so the
      framed client is tested only against a fake transport. Start with 0001
      (framing/banner/HELLO) and 0002a (non-TTY prompt poll).
- [ ] `PR_SET_PDEATHSIG=SIGKILL` is verified on Linux only; macOS/BSD/Windows
      rely on the heartbeat parent watchdog, and Windows named pipes remain
      unimplemented/unadvertised.
- [ ] HTTP is a partial JSON-RPC POST + SSE transport with no TLS and no full
      MCP streamable-HTTP state machine; `allowed_hosts` is an exact Host match.
- [ ] `natfeats=true` remains forgeable via NF_STDERR, including a fake
      `NatFeats: exit(N)`; keep it off by default and document the tradeoff.
- [ ] Heartbeat auto-termination, WAL/autosave scheduling and restore/replay
      policy are still unimplemented; only the atomic snapshot primitive exists.
- [ ] Final review sign-off of the latest fixes is pending.

### Final reconciliation (2026-10-27)

- Full suite after the latest fixes: **452 passed, 1 skipped** (FreeMiNT
  prebuilt corpus binaries unavailable), with one `asyncio_mode` warning because
  `pytest-asyncio` is absent.
- Server registers **102 tools**; `tools/protocol.py` is registered and live
  capabilities report stock `legacy`/`banner-only`, `negotiated=false`,
  `native=false`.
- Live verified: start/stop, breakpoint → `at_prompt` → stdin register
  set/step → resume → clean stop; SIGSTOP pause → resume → clean stop; AHCC CLI
  compile (7,074-byte TOS); real boot-capture-stop scenario. The earlier
  `406 passed / 5 failed` run is superseded and retained only as history.

### Self-review (independent review subsequently completed)

- Scope discipline: only `protocol/**`, `transport/**`, `tools/protocol.py`,
  `supervisor.py`, `state.py`, new tests/docs were changed. `server.py`,
  `app.py`, `pyproject.toml` and other tool modules were not edited.
- Compatibility: stock Hatari defaults to `legacy`; no binary byte is sent
  without the banner. `emu_status` and `emu_capabilities` share one schema.
- Safety: the heartbeat skips while the debugger prompt is active and while a
  tool holds the command lock; requested stop sets `_requested_stop` before
  acquiring the lifecycle lock. Restart is disabled by default.
- Known weaknesses: automatic restart is unit-tested with a fake
  `_start_locked`, not against repeated real crashes; the HTTP transport is
  partial by design; the legacy matrix labels `headless_render` as fallback
  because SDL dummy already renders, not because a native `--headless` exists.
- The early P6 replay/security failures noted above were subsequently fixed;
  the current full suite is 452 passed, 1 skipped.

### Next

1. Implement patch 0001 + banner/HELLO against the pinned base; run the P0–P6
   smoke suite in both modes.
2. Patch 0002a non-TTY prompt polling; then notifications and binary readback.
3. Frame/clock and input injection; then Windows (gated) and HTTP completion.

## Adversarial review

**Provenance:** the earlier self-review could not spawn an independent reviewer.
Independent adversarial reviews were subsequently run (rounds 1 and 2); their
Critical/High findings are recorded below and in the work log. The table is
retained as history; the latest fixes are under final review.

| # | Finding | Severity | Disposition in this plan |
| --- | --- | --- | --- |
| 1 | Architecture assumes `hatari-debug version` can probe capabilities; that command does not exist in Hatari 2.6.1. | High | §1 uses a startup banner plus HELLO; §7 selection; architecture update is a required deliverable. Never send binary to legacy. |
| 2 | Polling the socket at the prompt conflicts with readline's buffered stdin. | High | §1b splits running/prompt patches, prioritizes the non-TTY `poll()` path, proposes `rl_callback_*` for TTY, and allows a documented TTY fallback. |
| 3 | "One channel works in every state" hides a state-machine complexity: breakpoint entry occurs while a response is outstanding. | High | Request/response ids plus notifications; prompt-state commands are separate; handlers await events, not impossible replies; §7 fallback. |
| 4 | Notifications can stall emulation or drop breakpoint events. | High | Bounded non-blocking queue, coalescing only for screen/state, overflow marker, resync via `dbg_status`. |
| 5 | Binary readback can disclose host/emulator memory or exhaust memory. | High | Validated guest spaces/ranges, length caps, chunking, no host pointers; fuzzed decoder and ops. |
| 6 | Frame stepping is assumed cycle-exact; it is not. | High | Explicit VBL boundary semantics, `partial` results, measurement, `--cpu-exact` opt-in, fidelity labels. |
| 7 | Windows support is planned without CI. | High | Gated: implement behind a flag, require Windows CI, otherwise mark unsupported and do not advertise. |
| 8 | HTTP/SSE auth is treated as sufficient for safety. | High | Off by default, loopback, token, origin checks, TLS proxy, audit; multi-user isolation remains P8.5 and out of scope. |
| 9 | Crash restart can mask regressions and loop forever. | High | Bounded budget, backoff, crash-loop error, no restart after requested stop, artifacts. |
| 10 | Autosave "state preservation" is oversold; a hard-killed emulator cannot be snapshotted. | Medium | §8 guarantees only the last periodic snapshot; recovery without P7.5 is labeled approximate. |
| 11 | Upstream may reject the debugger-loop patch; the plan assumes it lands. | High | Patches optional; stock path remains default; split series; rebase policy; no fork. |
| 12 | Capability fallback can silently change semantics. | Medium | Capability-gated adapters, identical outer schemas, degraded-behavior table, table-driven tests. |
| 13 | Parent-death signals are Linux-only. | Medium | Portable session registry/watchdog; documented per-OS behavior; tests. |
| 14 | `--headless` is listed as a patch but is not required for P7's core. | Low | Kept as optional 0008; only if upstream wants it; SDL dummy remains the fallback. |
| 15 | The requested review is not independent. | Process | Recorded here; blocking action in Remaining. |

Changes already reflected: banner-based negotiation instead of a nonexistent
version command; split running/prompt patches with readline constraints;
notification overflow policy; binary read bounds; frame-step honesty; Windows
gating; HTTP threat model; bounded recovery; and an explicit stock-only feature
table.


### Adversarial review round 2 (Critical/High fixes)

| # | Finding | Severity | Fix in this revision |
| --- | --- | --- | --- |
| C1 | Guest `NF_STDERR` can forge the banner and debugger/NatFeats state; `--natfeats on` was default. | Critical | NatFeats defaults off; `run_prg` opts in; a banner is accepted only after a fully valid per-launch nonce parse inside a bounded live-state window (an invalid first line is diagnostic and does not consume the window); stderr state inference is disabled whenever NatFeats is on, and is ignored while stopping/terminal. Explicit NatFeats remains a documented trust tradeoff. |
| C2 | `hatari-stop` stops the control loop, so pause made the socket/debugger/heartbeat fail. | Critical | Pause/resume use SIGSTOP/SIGCONT; REMOTE_PAUSED is outside heartbeat; resume probes before RUNNING; stop SIGCONTs; debugger refuses with a resume hint; live test added. |
| H1 | Invalid protocol env leaked a child and left STARTING. | High | Env validated before spawn; all spawn exceptions clean runtime; stop turns proc-less STARTING/RUNNING terminal. |
| H2 | Banner alone was reported as framed/native/negotiated. | High | Banner is advisory: `banner-only`/`legacy`, `negotiated=false`, all native false; banner cleared on stop/crash; native requires a real handshake. |
| H3 | `unresponsive` was terminal. | High | Slower-cadence probing continues and a successful probe recovers to RUNNING. |
| H4 | Unsolicited/unbounded binary chunk reassembly. | High | Non-pending ids rejected, total/chunk count and running bytes capped, MORE requires BINARY, duplicate/gap/over-limit rejected. |
| H5 | Banner version/empty semantics were permissive. | High | Version must equal 1; `framed` must be advertised; unsupported version is a protocol error; no native from a banner. |
| H6 | Early exit waited the startup timeout and could restart during STARTING. | High | Socket future resolves false on exit; startup fails immediately; restarts suppressed while the launch was STARTING. |
| H7 | `preexec_fn` was passed on every POSIX platform and claimed portable. | High | Passed only on Linux; macOS/BSD/Windows parent death documented unsupported; heartbeat parent-pid watchdog remains. |
| M | Request-id reuse, exit/stderr race, HTTP framing/session/replay, autosave quoting, heartbeat counter resets, capabilities split, frame-id validation, JSON recursion, decoder EOF, MORE-on-non-binary. | Medium | All addressed with tests as described in the Work log. |

Honest remaining gaps: no patched Hatari/HELLO handshake, so `framed` is never
selected and all native flags stay false; NF_STDERR remains forgeable when
explicitly enabled; HTTP is still a partial transport; non-Linux parent-death is
watchdog-only; Windows named pipes remain unimplemented.
