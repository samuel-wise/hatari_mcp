# Upstream patches

Changes to the Hatari tree that the MCP server needs but cannot get from
existing command-line options or the control socket.

Policy:

* **Prefer options over patches.** If a capability can be reached with
  `--option`, `hatari-option`, `hatari-debug`, `hatari-event`, or `--parse`,
  do not patch.
* **One patch, one purpose.** Keep each change reviewable and upstreamable.
* **Record the base.** Every patch states the Hatari commit it was generated
  against; regenerate rather than silently rebasing.
* **Upstream first.** Open the upstream discussion/PR before depending on a
  patch in a release. Until it lands, v1 must keep working without it.

Naming: `NNNN-short-description.patch` (e.g. `0001-framed-control-protocol.patch`).

Apply:

```sh
git -C <hatari checkout> am mcp/project/patches/0001-*.patch
```

In the MCP server, patches are optional and negotiated. Detect capability with
a handshake/version probe and fall back to the sentinel scheme described in
`doc/architecture.md` §4.

## Candidates

| Patch | Why | Fallback without it |
| --- | --- | --- |
| Framed control protocol (request id, `OK`/`ERR`, end marker) | Removes sentinel ambiguity and timeouts. | `echo` sentinel. |
| Debugger/breakpoint notifications on the socket | Breakpoint hits arrive without polling stderr. | stderr parsing. |
| Binary framebuffer and RAM readback | Avoids hex/PNG round-trips and huge `memdump` text. | debugger `screenshot`, `savebin`, `memdump`. |
| `--headless` that keeps rendering | Guaranteed no window without SDL driver hacks. | `SDL_VIDEODRIVER=dummy`. |
| Windows control channel | In-process commands on Windows. | Unsupported in v1. |
| Deterministic virtual clock / frame stepping | Reproducible single-stepping of video frames. | `--run-vbls`, breakpoints. |

## Status

No patches are required for v1. This directory is intentionally empty until a
candidate above is promoted to an ADR.
