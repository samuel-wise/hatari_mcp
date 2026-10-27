# Hatari MCP Server

A Model Context Protocol (MCP) server that lets an AI agent programmatically
control Hatari — the Atari ST/STE/TT/Falcon emulator — including its debugger.

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
    │   ├── tools-reference.md MCP tool surface (names, args, mechanisms).
    │   ├── licensing.md       GPLv2+ status and derivative-work analysis.
    │   ├── adr/               Architecture Decision Records (numbered).
    │   ├── roadmap/           Long-term direction, milestones, priorities.
    │   ├── plans/             Implementation plans for individual features.
    │   ├── investigations/    Spikes, research notes, experiments.
    │   └── reviews/           Design/code review records and decisions.
    ├── config/                Example client configs and emulator profiles.
    │   └── profiles/          ST/STE/TT/Falcon launch profiles.
    ├── examples/              End-to-end agent session examples.
    ├── fixtures/              Test ROMs/disks/images (not committed).
    ├── patches/               Upstream Hatari changes the server may need.
    ├── scripts/               Developer and CI helper scripts.
    ├── src/                   MCP server source code (the deliverable).
    ├── tests/                 Unit, integration and end-to-end tests.
    └── tools/                 Standalone operator utilities (not shipped).
```

### `doc/`

Documentation for people working on the server, separate from user-facing
docs. Use the subdirectories by intent:

* **`architecture.md`** — the MCP↔Hatari contract: transports, command
  channels, response correlation, process lifecycle, security boundaries.
* **`tools-reference.md`** — the proposed MCP tool surface, including which
  Hatari mechanism each tool maps to and its limitations.
* **`licensing.md`** — why the server is GPLv2+ and what that means for
  distribution.
* **`adr/`** — numbered Architecture Decision Records. One decision per file,
  immutable once accepted; supersede rather than rewrite.
* **`roadmap/`** — where the project is going: capability coverage, milestones,
  and the order in which emulator features get exposed.
* **`plans/`** — concrete, reviewable implementation plans for a single feature
  or change, written before coding.
* **`investigations/`** — time-boxed research: how Hatari's debugger, control
  socket, tracing, or input paths actually behave, with findings and options.
* **`reviews/`** — records of design reviews, code reviews and the decisions
  they produced (including rejected alternatives).

### `config/`

Example configuration, not production secrets. Contains a generic MCP client
entry (`mcp-client.example.json`) and one launch profile per emulated machine
under `profiles/` (ST, STE, TT, Falcon). Profiles pin the arguments and paths
used for reproducible agent sessions and tests.

### `examples/`

Narrative, copy-pasteable agent sessions showing how the tools are meant to be
combined: boot a machine, inspect registers, set a breakpoint, capture a
screenshot, save and restore state. Examples are documentation, not tests.

### `fixtures/`

Local test data: generated floppy/HD images, save states, symbol files, and
EmuTOS/Kickstart binaries fetched at setup time. Copyrighted TOS ROMs and
commercial disk images must never be committed; `.gitignore` in this directory
enforces that. See `fixtures/README.md`.

### `patches/`

Host-side patches against the Hatari tree for capabilities the MCP server needs
but upstream does not yet expose (framed debug protocol, breakpoint
notifications, framebuffer readback). Each patch is named, based on a recorded
commit, and documented with its rationale. Anything that can be done with
existing options must not become a patch.

### `scripts/`

Executable automation that supports development: building/launching the server,
running linters and tests, fetching fixtures, and driving protocol conformance
checks. Scripts should be idempotent and safe to run in CI.

### `src/`

The MCP server implementation: transport setup, tool/resource definitions, and
the bridge to Hatari (process control, control socket, debugger stream parsing,
input, media, state). This is the only directory that must ship to users.

### `tests/`

Automated tests for `src/`, organized by level: unit tests for pure logic
(parsers, argument builders), integration tests against a running Hatari
instance, and end-to-end tests that drive the server through the MCP protocol
from a client's perspective.

### `tools/`

Small, standalone developer utilities that are useful but are not part of the
server's runtime. Examples: a manual MCP client for poking the server, a
debugger-output parser harness, or a fixture generator.

## Relationship to the emulator

The server treats the emulator as an external process. Anything the server
needs from Hatari that it does not already expose (e.g. a stable
machine-readable debugger protocol, headless control, framebuffer access)
belongs in `doc/roadmap/` and `patches/` and is implemented upstream, not
hidden inside the MCP layer.
