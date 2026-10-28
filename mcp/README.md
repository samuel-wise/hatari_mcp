# Hatari MCP Server

A Model Context Protocol (MCP) server that lets an AI agent programmatically
control Hatari — the Atari ST/STE/TT/Falcon emulator — including its debugger.
The current server registers **102 tools** covering lifecycle, logs, debugging,
memory/state, screens, frames, input, scripting, media, devices, build/run,
AHCC, protocol capability reporting, and scenario automation.

The emulator source lives in the repository root. Everything MCP-related lives
under `mcp/`, with the server project contained in `mcp/project/`.

## Directory layout

```
mcp/
├── README.md                  This file.
├── .gitignore                 Build/runtime artifacts for the MCP project.
└── project/                   The MCP server project.
    ├── doc/                   Design and project documentation.
    │   ├── architecture.md    How the server talks to Hatari. Start here.
    │   ├── tools-reference.md Generated reference for all 102 MCP tools.
    │   ├── licensing.md       Proposed licensing analysis (GPLv2+).
    │   ├── adr/               Architecture Decision Records (numbered).
    │   ├── roadmap/           Direction, milestones, and implementation status.
    │   ├── plans/             Implementation plans for individual features.
    │   └── investigations/    Spikes, research notes, and P8 verdicts.
    ├── config/                Example client configs and emulator profiles.
    │   └── profiles/          ST/STE/TT/Falcon launch profiles.
    ├── examples/              End-to-end agent session examples.
    │   └── scenarios/         JSON scenario examples and the scenario schema guide.
    ├── fixtures/              Test ROMs/disks/images (not committed).
    ├── labs/                  Non-shipping P8 research harnesses (see below).
    ├── patches/               Upstream Hatari changes the server may need.
    ├── scripts/               AHCC CLI helper (other helpers planned).
    │   └── ahcc-cli.py        Standalone AHCC 6.0 compile driver.
    ├── src/                   MCP server source code (the deliverable).
    │   └── hatari_mcp/
    │       ├── services/      AHCC and scenario engine services.
    │       ├── protocol/      Framed-protocol codec, banners, selection (not wired).
    │       ├── transport/     Partial HTTP/SSE/auth helpers; `server.py` only runs stdio.
    │       └── tools/         One registration module per tool group.
    ├── tests/                 Automated tests, organized by level.
    │   ├── unit/              Pure logic: builders, parsers, schemas, artifacts.
    │   ├── integration/       Real Hatari runs (scenario integration).
    │   ├── e2e/               MCP stdio protocol/capability modes.
    │   ├── corpus/            External corpus discovery/scenario tests.
    │   └── labs/              Guarded P8 lab tests.
    └── tools/                 Non-shipping utilities (currently the AHCC tree).
        └── ahcc/              AHCC 6.0 distribution used by the CLI/GUI paths.
```

### `doc/`

Documentation for people working on the server, separate from user-facing
docs. Use the subdirectories by intent:

* **`architecture.md`** — the MCP↔Hatari contract: transport, dual command
  channels, response correlation, process lifecycle, scenario path, security
  boundaries, and known limitations.
* **`tools-reference.md`** — the current 102-tool surface, grouped by
  registration module, with arguments, mechanisms and honest limitations.
* **`licensing.md`** — proposed GPLv2+ analysis; the decision needs maintainer
  sign-off and no license metadata is set in `pyproject.toml` yet.
* **`adr/`** — numbered Architecture Decision Records. One decision per file,
  immutable once accepted; supersede rather than rewrite.
* **`roadmap/`** — direction, milestones, and a factual implementation-status
  snapshot; not a promise that every item is shipped.
* **`plans/`** — concrete, reviewable implementation plans for a single feature
  or change, written before coding.
* **`investigations/`** — time-boxed research: how Hatari's debugger, control
  socket, tracing, or input paths actually behave, with findings and verdicts.

### `config/`

Example configuration, not production secrets. Contains a generic MCP client
entry (`mcp-client.example.json`) and one launch profile per emulated machine
under `profiles/` (ST, STE, TT, Falcon). Profiles pin the arguments and paths
used for reproducible agent sessions and tests.

### `examples/`

Narrative, copy-pasteable agent sessions showing how the tools are meant to be
combined: boot a machine, inspect registers, set a breakpoint, capture a
screenshot, save and restore state. Examples are documentation, not tests.

`examples/scenarios/` holds runnable scenario definitions (JSON schema v1) plus
a README covering the scenario format, matrix, capture/assert steps, strict tool
allowlist, corpus staging, CLI, and replay fidelity.

### `fixtures/`

Local test data: generated floppy/HD images, save states, symbol files, and
EmuTOS/Kickstart binaries. Fetch/generation helpers are planned but not present
yet. Copyrighted TOS ROMs and commercial disk images must never be committed;
`.gitignore` in this directory enforces that. See `fixtures/README.md`.

### `labs/`

Disposable P8 research harnesses for benchmarking, time travel, DWARF parsing
and coverage. This directory is **non-shipping**:

* it is outside `src/`, is not packaged in a wheel, and registers no MCP tool;
* every submodule is gated by `HATARI_MCP_LABS=1`;
* results are raw measurements under `/tmp/hatari-mcp-p8-*`, not features;
* the P8 investigations are mostly INCONCLUSIVE or narrow GO verdicts.

See `labs/README.md`. Do not import lab code from `src/hatari_mcp` without a
promoted plan and ADR.

### `patches/`

Host-side patches against the Hatari tree for capabilities the MCP server needs
but upstream does not yet expose (framed debug protocol, breakpoint
notifications, framebuffer readback). Each patch is named, based on a recorded
commit, and documented with its rationale. Anything that can be done with
existing options must not become a patch.

### `scripts/`

`ahcc-cli.py` is the standalone AHCC 6.0 compile path used by the `ahcc_compile`
tool; the MCP tool runs the script in a separate isolated headless Hatari
instance rather than the supervisor's emulator. Build/launch, lint/test,
fixture-fetch and protocol-conformance helpers are planned but not present yet.

### `src/`

The MCP server implementation: server wiring under `hatari_mcp/server.py`,
process supervision, tool/resource definitions under `tools/`, and the bridge
to Hatari (control socket, debugger stdin/stdout/stderr, input, media, state).
`services/` contains the AHCC compile service and the scenario engine;
`protocol/` contains framed-protocol primitives that are **not wired into the
supervisor** (capability reporting is legacy/banner-only); `transport/` contains
partial HTTP/SSE and auth helpers, but there is no CLI/config wiring and
`server.py` only runs `transport="stdio"`.
This is the only directory that must ship to users.

### `tests/`

Automated tests for `src/`, organized by level: unit tests for pure logic
(parsers, argument builders, scenario schema/artifacts), integration tests
against a running Hatari instance (including the real scenario path), end-to-end
tests that drive the server through the MCP protocol from a client's
perspective, corpus discovery/scenario tests, and guarded lab tests.

### `tools/`

This directory is reserved for small, standalone developer utilities. The only
present content is the `ahcc/` subtree, which holds the AHCC 6.0 distribution
used by the CLI and GUI paths. Utilities such as a manual MCP client, a
debugger-output parser harness or a fixture generator are planned, not present.

## Relationship to the emulator

The server treats the emulator as an external process. Anything the server
needs from Hatari that it does not already expose (e.g. a stable
machine-readable debugger protocol, headless control, framebuffer access)
belongs in `doc/roadmap/` and `patches/` and is implemented upstream, not
hidden inside the MCP layer. The current stock-Hatari path uses the legacy
control socket plus the debugger's stdin/stderr channels; framed protocol
support is implemented only as capability/reporting scaffolding so far.
