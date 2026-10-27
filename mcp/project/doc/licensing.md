# Licensing

Status: proposed, needs a maintainer sign-off before the first release.
This is an engineering analysis, not legal advice.

## The emulator

Hatari is distributed under the GNU General Public License, **version 2 or
(at your option) any later version** (see `gpl.txt` and the headers in
`src/`). Any change made to Hatari itself — including everything in
`patches/` — is therefore GPL-2.0-or-later and must be contributed back under
the same terms when distributed.

## The MCP server

The server lives in `src/` and does not link Hatari code. It launches the
`hatari` binary as a child process and communicates over:

* a Unix-domain socket (`--control-socket`),
* the child's stdout/stderr pipes.

Under the usual FSF interpretation, two programs that communicate only through
pipes/sockets, each with its own address space, are separate works
("arm's-length"). That would allow the server to use any license, including a
permissive one.

However:

1. The server is useless without Hatari and will be distributed alongside it
   in this repository.
2. `patches/` is unambiguously GPL-2.0-or-later.
3. Mixing licenses in one repository invites accidental derivative work when
   the bridge starts parsing Hatari internals or sharing headers.

**Decision (proposed): license the entire `mcp/` tree under
GPL-2.0-or-later**, matching the emulator. This is the lowest-risk option and
keeps the project simpler than a split-license argument. If a permissive
license is later required for adoption, an ADR can revisit it — but only while
the bridge remains strictly process-separated.

## Third-party dependencies

* MCP Python SDK — MIT (GPL-compatible).
* Python standard library — PSF license (GPL-compatible).
* Any test ROMs (EmuTOS) — GPL-2.0-or-later; keep the license file alongside
  the binary in `fixtures/` and never commit Atari TOS ROMs, which are
  proprietary.

Dependency licenses must be checked before adding them; a `scripts/` check
should fail the build on a GPL-incompatible dependency (e.g. a
non-GPL-compatible copyleft library).

## Required work before release

* [ ] Add `mcp/LICENSE` (copy of `gpl.txt`, or an SPDX pointer).
* [ ] Add SPDX headers to every source file:
      `SPDX-License-Identifier: GPL-2.0-or-later`.
* [ ] Add a `NOTICE`/section in `mcp/README.md` stating the license.
* [ ] State in the README that the server is not affiliated with or endorsed
      by the Hatari project.
* [ ] Confirm the upstream Hatari project is comfortable hosting this under
      `mcp/`; otherwise keep it as a separate repository.
