# 0002 — Framed control protocol (HMCP v1) and legacy fallback

* Status: Proposed for upstream; server-side primitives accepted
* Date: 2026-10-27
* Supersedes: —
* Related: `0001-language-and-transport.md`, `P7-protocol-upstream.md`

## Context

The v1 server drives stock Hatari through the control socket plus a sentinel
on stderr. That path cannot distinguish a debugger reply from interleaved log
lines, cannot correlate responses except by an echo marker, cannot carry
binary data, and cannot poll the socket while the debugger is blocked at its
prompt. The P7 plan proposes an optional upstream extension while keeping
stock Hatari as the product default.

The server must never send binary frames to an unpatched build: the control
socket parses newline-delimited text and would treat frame bytes as a debugger
command. Detection therefore needs an explicit, cheap positive signal before
any frame is written.

## Decision

### Wire format

HMCP v1 uses a 16-byte network-byte-order header followed by an optional
payload:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | magic `HMCP` |
| 4 | 1 | protocol version (1) |
| 5 | 1 | type: `HELLO=1`, `HELLO_ACK=2`, `REQUEST=3`, `OK=4`, `ERR=5`, `NOTIFY=6`, `PING=7`, `PONG=8` |
| 6 | 1 | flags: `BINARY=0x01`, `MORE=0x02`, `NO_REPLY=0x04` |
| 7 | 1 | reserved, must be zero |
| 8 | 4 | request id (nonzero for request/response frames, echoed) |
| 12 | 4 | payload length |
| 16 | n | payload |

Limits and validation:

* default maximum payload 8 MiB, absolute 64 MiB; the configured limit is
  echoed in `HELLO_ACK.limits`;
* the decoder validates magic, version, type, flags, reserved byte and length
  before allocating or waiting for the payload; an oversized declared length
  is rejected immediately;
* partial headers, partial payloads, coalesced frames and EOF at any boundary
  are handled by a streaming state machine; EOF mid-frame is
  `truncated_frame`;
* unknown types, versions, flags and request-id misuse are `bad_frame`;
* request ids are not reused until a response or timeout, and a duplicate or
  late response is ignored and counted, never delivered twice.

Payloads:

* control: UTF-8 JSON `{"op": "...", "params": {...}}`; responses
  `{"ok": true, "result": ...}` or `{"ok": false, "error": {...}}`;
* binary: `u32 meta_len`, JSON metadata, raw bytes. Metadata is validated
  before use (`space`, `address`/`offset`, `length`, `format`, `width`,
  `height`, `stride`, `bpp`, `endian`, `palette`, `vbl`, `frame`, `sha256`);
  chunked responses use `sequence`, `total` and `total_length`, are bounded by
  the client's reassembly cap, and are rejected on gaps or digest mismatch;
* errors use stable `code`, `message`, `hint`, `data` fields. Server code maps
  wire codes to its public error vocabulary; unknown codes become `internal`.

### Handshake and versioning

A patched Hatari writes exactly one startup line to stderr:

```text
HATARI-MCP-PROTOCOL 1 capabilities=framed,notify,binary,input,clock,frame nonce=<per-launch-hex>
```

The nonce is required. The server generates a fresh value per launch, exports
it as `HATARI_MCP_PROTOCOL_NONCE`, and Hatari must echo it exactly; a missing or
mismatched nonce is rejected so guest stderr cannot forge the banner. The
banner is accepted only within a short bounded startup window, and only after
a full successful parse (version, capabilities, nonce). An invalid first line
is recorded as a diagnostic but does not consume the window, so a later valid
nonce-bound banner may replace it. (For standalone parser tests only, the
helper can be called without an expected nonce; the supervisor never does.)

Only after that banner may the server send `HELLO`. The `HELLO` carries a
client version range and requested capabilities; `HELLO_ACK` selects a version
in the intersection, returns the supported capability set, and reports Hatari
version/commit/build flags and limits. Banner capabilities are advisory until
`HELLO_ACK`; unknown capability names are reported, never silently enabled.

Version changes are negotiated, not inferred. Additive JSON fields and unknown
capability names are forward-compatible; frame header changes require a new
version. The server accepts only v1 and refuses a mismatched selection.

### Selection and fallback

`HATARI_MCP_PROTOCOL` selects the mode:

| Value | Behaviour |
| --- | --- |
| `auto` (default) | Framed only after a valid banner (and, when connected, a successful HELLO); otherwise legacy. |
| `legacy` | Never send frames, even if a banner is present. |
| `framed` | Require the framed protocol; startup fails with `protocol_unavailable` if absent. |
| `require-framed` | Also require `framed_prompt` for prompt-state CI. |

The legacy sentinel/stdin adapter remains a first-class implementation of the
same request interface. It is not deleted when the patch lands; stock Hatari
is the product default. Every degraded feature is named in
`emu_capabilities`: for stock Hatari `framed`, `framed_prompt`,
`input_frame`, `input_abs`, `clock_virtual` and `windows_pipe` are
unsupported; `notify`, `binary`, `fb_native`, `input_joy`, `frame_step` and
`headless_render` are explicit fallbacks. The outer result schema is identical
in both modes.

### Compatibility and upstream process

The patch keeps the newline control commands and `hatari-debug` behaviour. A
framed connection and a legacy connection are mutually exclusive per socket.
The patch series is optional and maintained against a recorded Hatari commit;
stock Hatari and the sentinel fallback remain supported without it. Prompt
polling is a separate patch (see `doc/investigations/P7-prompt-poll.md`) and
does not gate the running-state framed path.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| JSON lines only | No binary framing, ambiguous message boundaries, larger memory copies, still needs a separate prompt-poll design. |
| GDB remote protocol | Hatari has no GDB stub; implementing one is a larger, unrelated project and does not cover input/media/state. |
| Version probe via `hatari-debug version` | The command does not exist in Hatari 2.6.1; a banner plus HELLO replaces it. |
| Send frames speculatively and fall back on parse errors | Corrupts stock debugging and violates the no-binary-to-stock rule. |
| Always require the patch | Blocks the stock product and contradicts the P7 goal. |

## Consequences

* Stock Hatari continues to work; no binary byte ever reaches an unpatched
  control socket.
* Framed users gain request ids, deadlines, typed errors, chunked binary reads
  and notifications; legacy users keep the documented approximations.
* Two transports must stay tested. Capability-gated adapters and identical
  outer schemas prevent silent semantic changes.
* The decoder and metadata parser are a security boundary: they are fuzzed and
  length-checked before allocation. Binary reads are still guest-memory reads
  and must validate address spaces and caps.
* Framing does not fix the prompt-state block by itself. Until the separate
  prompt-poll patch is accepted, prompt commands remain on stdin even in
  framed mode and `framed_prompt` is reported honestly.
