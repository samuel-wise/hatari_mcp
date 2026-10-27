# Test fixtures

Local test data for integration and end-to-end tests. **Nothing here is
committed** except this file; `.gitignore` ignores the entire directory.

Typical contents:

* `emutos.img` — EmuTOS ROM (GPL-2.0-or-later), fetched by
  `scripts/fetch-fixtures.sh` (planned).
* `tos*.img` — proprietary Atari TOS ROMs. Allowed only on a developer
  machine, never in a commit, never in CI cache shared outside the org.
* `blank-*.st`, `hd-*.img` — generated floppy/hard-disk images.
* `*.state` — Hatari memory snapshots produced by tests.
* `*.sym`, `*.map` — symbol files for debugger tests.
* `*.prg` / `*.ttp` — small test programs, built from source in `tests/`.
* `SHA256SUMS` — checksums for downloaded/generated fixtures.

## Rules

1. No ROM, disk image, or state file may be committed. `.gitignore` uses a
   whitelist; do not weaken it.
2. Fixtures are either **generated** (deterministic, from scripts) or
   **fetched** (EmuTOS releases only, with checksum verification).
3. Tests that need a fixture skip with a clear message when it is absent;
   they must not silently pass.
4. Record the origin and license of every fetched file in `SHA256SUMS` or a
   sidecar `.txt`.
5. Keep fixtures small. Prefer a blank 720 KiB floppy and a 16 MiB HD image
   over full distributions.

## Generating images

Existing Hatari tools can create them:

```sh
# blank floppy / HD image
python3 ../tools/atari-hd-image.sh ...
../../tools/zip2st.sh ...
```

`scripts/fetch-fixtures.sh` and `scripts/make-fixtures.sh` will wrap these once
the server skeleton lands. Until then, run the tools manually and verify with
`tests/` expectations.
