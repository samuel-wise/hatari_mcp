# Atari ST game launcher

A host-side Tkinter application that browses the local Atari ST TOSEC corpus,
launches one game in Hatari through the MCP server, and learns the
machine/TOS configuration that actually produced a game frame. The Hatari
window is shown by default, so both the Tk launcher and the emulator need an
X display; use xvfb-run only for headless automation. Success is detected in two stages:
the initial boot frame is recorded as a baseline, then the frame must change
from it and the CPU PC must leave TOS ROM (0xE00000-0xFFFFFF) before the new
frame is accepted. If the MCP cannot report the PC, the launcher falls back to
a longer stable-frame check and records that limitation in the evidence.
Cracked loaders and GEM-desktop boot disks that hand control back to TOS while
waiting for a key are accepted once game code has run and the post-boot screen
has stayed stable for two seconds; the evidence records `mode: loader`.

**Turbo boot** runs the boot phase at 32 MHz with fast-forward enabled, then
restores 8 MHz and normal speed as soon as the boot screen settles (or after a
10-second cap), so desktop loaders can be clicked at normal speed while the
game continues booting. Disk inserts get a short turbo burst too, then return to
8 MHz. **Releases are grouped by title**, and every variant from the filename
(`[cr BOSS]`, `[a]`, `[tr fr partial]`, ...) is shown in its own column; a
multi-disk release stays one catalog row with a **Disks** count, and its disks
are chosen in the details pane. While a game is running, the details pane can
insert another disk in place without restarting Hatari. **Box art** comes from
local sidecars, the optional KairoST catalog, the built-in Libretro index, or a
configured URL template, and is cached under `state/cache/art` for offline
reuse. Missing genres are looked up on demand from MobyGames when an API key is
configured, otherwise Wikipedia, and cached.

It is example program 1 in `examples/programs` and only uses the shared
`common` package plus the Python standard library (Tkinter included). No
PyGame, Qt or requests are required. PNG/GIF artwork works with stock Tk;
JPEG and WebP (including KairoST box art) need Pillow:

```bash
mcp/.venv/bin/python -m pip install 'pillow>=10'
# or install the optional extra: pip install -e '.[launcher]'
```

## Setup

Use the project virtualenv from the repository root:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
mcp/.venv/bin/python --version          # Python 3.12
mcp/.venv/bin/python -c 'import tkinter'  # Tkinter must be installed
```

Hatari, TOS images, game folders and catalog lookups are configurable with
**Settings…** in the toolbar. The dialog is prefilled from
`game_launcher/defaults.json`; saved overrides live under `paths` in
`config.json`. Hatari is validated as an existing executable, and TOS, game and
catalog paths accept multiple folders. Settings apply immediately and reload the
catalog; a changed Hatari binary is used by the next Hatari process.

At least one valid TOS image must be present in a configured TOS folder (or
`HATARI_TOS`). Images must be 192, 256 or 512 KiB; intermediate build artifacts
such as the 194,708-byte `emutos.img` are rejected. ST games default to TOS 1.04,
with 1.02/1.00/EmuTOS fallbacks; a valid `etos192us.img` is the last-resort
EmuTOS. The shipped defaults are:

```text
Hatari:  ~/Projects/Hatari/hatari_mcp/build-install/bin/hatari
TOS:     ~/Projects/Hatari/tos
Games:   ~/Projects/Hatari/atari_st/archive.org/download/Atari_ST_TOSEC_2012_04_23/Games
Catalog: ~/Projects/Hatari/atari_st/KairoST/catalog
```

Edit `defaults.json` to change the system defaults. Saved settings take
precedence; otherwise environment overrides (`HATARI_BIN`, `HATARI_TOS_DIR`,
`HATARI_TOS`, `HATARI_MCP_GAMES`, `HATARI_MCP_GAME_ROOT`, `HATARI_GAMES_ROOT`,
`HATARI_MCP_KAIROST_CATALOG`, `KAIROST_CATALOG`) apply, then `defaults.json`.
`--games` still overrides the catalog roots for that run. Add or remove disk
image folders with **Settings… → Disk image directories**.

## Run

The wrapper script is the simplest option. It selects the project virtualenv,
sets `PYTHONPATH` and `HATARI_MCP_REPO`, checks `DISPLAY`, and forwards options:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project/examples/programs/game_launcher
./run.sh
# or from anywhere:
/host/path/mcp/project/examples/programs/game_launcher/run.sh --page-size 100
```

From `examples/programs` without the wrapper:

```bash
cd mcp/project/examples/programs
../../../.venv/bin/python -m game_launcher
```

From the repository root with an explicit `PYTHONPATH`:

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp
PYTHONPATH=mcp/project/examples/programs \
  mcp/.venv/bin/python -m game_launcher --page-size 100
```

Useful options:

```bash
python -m game_launcher --games /path/to/games --config /tmp/launcher/config.json
python -m game_launcher --machine ste       # default launch-machine override
python -m game_launcher --state /tmp/launcher-state
```

For headless launcher UI and Hatari (no visible windows):

```bash
cd mcp/project/examples/programs
xvfb-run -a ../../../.venv/bin/python -m game_launcher
```

### Headless GUI smoke

```bash
cd mcp/project/examples/programs
xvfb-run -a ../../../.venv/bin/python -m game_launcher.smoke_gui \
  --games /path/to/small-corpus --config /tmp/launcher-config.json \
  --state /tmp/launcher-state --timeout 8
# prints gui_smoke: entries=... and exits 0 when the window and catalog worked
```

## UI

* **Settings…** — toolbar button for the Hatari binary, TOS/disk/catalog
  folders, online enrichment and turbo boot toggles, and the metadata/box-art
  URL templates. Online enrichment and turbo boot are both on by default; turn
  either off here for fully offline or always-8-MHz operation.
* **Catalog** — paginated, searchable list with prev/next paging. Titles with
  several releases collapse under one parent row; each release row shows the
  **variant** tags from its filename in the column immediately after the title.
  Multi-disk releases stay one aggregate row with a **Disks** count instead of
  one child row per disk, so the catalog is not padded with duplicates. Rows are
  colour-coded: green = last launch succeeded, red = never succeeded, orange =
  succeeded before but the latest attempt failed, black = never tested. A
  multi-disk release is green if any of its disks has a working launch, so a
  failed extra disk does not turn a working release red; a title with any
  successful variant is green. The
  scan runs on a daemon thread (`Catalog.start_scan`) so Tk stays responsive on
  ~13k files. Search uses `Catalog.search`; page slicing happens in the launcher.
* **Filters** — machine, genre and year decade. Filters are populated from the
  scanned catalog and sidecar metadata.
* **Details** — square art area on the left (portrait KairoST/web art fits at
  full height; no frame or background box when art is present), with a single
  column of fields on the right: year, genre, publisher, developer, machine,
  TOS and resolution. The long description spans the width below the art. Only
  per-field provenance is shown in a compact provenance box. The launch
  controls, **TOS** selector, **Disk** selector, **Insert disk** and the long
  **File** field span the width below the description.
* **Launch as** — `Auto` or an explicit `st`, `ste`, `tt`, `falcon` override.
* **TOS** — `Auto (profile)` or an explicit TOS/EmuTOS image. The list contains
  every valid image discovered by the launcher; **Browse…** accepts any other
  192/256/512 KiB image. The choice is saved per game and passed as
  `requested_tos`, overriding learned and automatic selection. Changing the TOS
  of a running game stops Hatari and starts a fresh instance.
* **Launch path** — optional program to start after the boot disk. Enter a
  TOSEC-style guest path such as `RUNME.PRG`, `FOLDER/RUNME.TOS` or a full
  `A:\FOLDER\RUNME.TOS`; relative paths are anchored to boot drive A:. Saves in
  the game record and is passed to Hatari as `--auto` for a fresh boot, so a
  disk that only reaches GEM desktop can start its executable automatically.
  Requires TOS 1.04 or newer. Disk inserts intentionally do not autostart.
* **Launch / Stop** — all MCP calls run through `common.ui.TaskRunner`; the Tk
  thread never blocks on Hatari. Launching another disk of the same running
  group inserts it instead of restarting. Insert stays available after a launch
  that timed out on an interactive loader, as long as Hatari is still running.
* **Turbo boot** — configured in **Settings…** (on by default). Boots at
  32 MHz with fast-forward and restores 8 MHz/normal speed once a changed boot
  frame is stable for 1 second (or after 10 seconds at most), rather than
  waiting for full game detection. Each disk insert runs a short 2-second
  32 MHz burst to speed loading, then restores normal speed.
* **Status/log** — hidden by default; use **Show status** in the toolbar to
  expand it and **Hide status** to collapse it. It shows scan progress, MCP
  state, launch/swap/restart explanation, boot result, disk insert, failure
  reason and evidence path. MCP state is polled every 1.5 seconds, so closing
  Hatari outside the launcher disables **Stop** automatically. **Clear** inside
  the status pane clears the log; the search/filter controls no longer have a
  Clear button.
* **Online enrichment / art** — configured in **Settings…** (on by default).
  A metadata URL enables JSON enrichment; the box-art URL template supplies
  missing art. With no metadata URL the launcher still fetches box art from the
  configured template or Libretro. Missing genres are looked up on selection
  (MobyGames via API key, otherwise Wikipedia) and cached in
  `state/cache/genres.json`; turn online enrichment off for no network access.

## Configuration

State lives under `examples/programs/state` unless
`HATARI_MCP_EXAMPLES_STATE` or `--state` is set. `Config` writes atomically.

```json
{
  "version": 1,
  "game_dirs": ["/path/to/games"],
  "paths": {
    "hatari_bin": "/path/to/hatari",
    "tos_dirs": ["/path/to/tos"],
    "game_dirs": ["/path/to/games"],
    "catalog_dirs": ["/path/to/KairoST/catalog"]
  },
  "ui": {
    "online_enrichment": true,
    "boot_turbo": true,
    "status_visible": false
  },
  "games": {
    "<catalog-id>": {
    "machine": "st",
    "profile": "st",
      "tos": "/home/ubuntu/Projects/Hatari/tos/tos1x/roms/tos104us.img",
      "observed_resolution": [320, 200],
      "status": "success",
      "last_success": 1760000000,
      "last_success_config": {
        "machine": "ste",
        "profile": "ste",
        "tos": "/home/ubuntu/Projects/Hatari/tos/tos1x/roms/tos104us.img",
        "args": [],
        "observed_resolution": [320, 200]
      },
      "launcher": {
        "action": "start",
        "profile_source": "filename",
        "tos_source": "auto:tos104",
        "metadata_provenance": {"publisher": "sidecar:/path/Game.zip.json"},
        "art_source": "sidecar:/path/Game.png",
        "evidence_path": ".../state/run/<id>/evidence.json"
      },
      "last_result": {
        "ok": true,
        "status": "success",
        "resolution": [320, 200],
        "evidence_path": ".../state/run/<id>/evidence.json"
      }
    }
  }
}
```

The shared `Config.record_launch` keeps `last_success_config` and adds a failed
attempt's reason without discarding the previous working configuration. The
launcher additionally writes `last_failure_config`, `last_result`, provenance
and `run/<game-id>/evidence.json`.

### KairoST catalog

The launcher automatically reads the optional [KairoST](https://github.com/MrJackSpade/KairoST)
catalog for canonical titles, years, publishers, developers, descriptions and
local box art. The shipped default lives in `defaults.json`:

```text
~/Projects/Hatari/atari_st/KairoST/catalog
```

**Settings… → Catalog lookup directories** accepts one or more roots (repository
root or its nested `catalog` data directory), and lookups merge them in order.
`HATARI_MCP_KAIROST_CATALOG` / `KAIROST_CATALOG` still work when no settings are
saved; missing data simply disables that layer. KairoST art is WebP under
`catalog/artwork/art/catalog/{atarist,moby}/<id>/` and is used before web
lookups; LaunchBox box/screenshot URLs in the catalog are the remote fallback.

### Per-game sidecars

Sidecars are plain JSON. The launcher reads, in increasing priority:

1. TOSEC filename fields
2. `online:<provider>` enrichment values
3. KairoST canonical values and local art
4. `<disk>.zip.json` / `<disk>.st.json`
5. `<disk>.json`
6. `state/metadata/<catalog-id>.json`

Online enrichment is written to candidate 6 with
`"provenance": {"source": "online:<provider>"}`. Local sidecar values win over
KairoST and online values; all win over TOSEC filename guesses.

```json
{
  "title": "Speedball 2: Brutal Deluxe",
  "year": 1990,
  "publisher": "Image Works",
  "genre": "Sports",
  "developer": "The Bitmap Brothers",
  "players": "1-2",
  "machine": "ste",
  "resolution": [320, 200],
  "description": "Future sports with a metal ball. Add a sidecar next to the disk or in state/metadata/<id>.json.",
  "args": ["--sound", "off"],
  "art": "art/speedball2.png",
  "art_source": "scanned box art"
}
```

* Relative `art` paths are resolved next to the sidecar file. An `art` value
  that is an `http://` or `https://` URL is downloaded and cached.
* The shared `ArtResolver` also checks
  `state/cache/art/<id>.png|gif|jpg|jpeg|webp`. Downloaded art is validated by
  image magic bytes, written atomically, and reused offline.
* When no art exists the UI draws a deterministic colored placeholder from the
  catalog ID, title, year and publisher.

Metadata provenance is recorded per field, e.g.
`publisher=sidecar:/games/Game.zip.json`, `year=filename`,
`description=online:my-provider`. The UI and `evidence.json` expose it.

### Optional online enrichment and art

Online enrichment is on by default and can be disabled in **Settings…** for
fully offline operation. KairoST is always tried first when its catalog is
installed, including local WebP art and its remote LaunchBox fallback. The
box-art template defaults to the keyless Libretro thumbnail index:

```text
https://thumbnails.libretro.com/Atari%20-%20ST/Named_Boxarts/{stem}.png
```

`{stem}` is the TOSEC filename without `.zip`, so release tags such as
`[cr BOSS]` or `[one disk]` usually resolve to the matching thumbnail. Downloads
are cached under `state/cache/art` and used offline afterwards. KairoST's
remote LaunchBox box/screenshot URLs are used only when no local KairoST art
exists; sidecar `art` still wins. Sources such as MobyGames, AtariMania,
Wikipedia and Video Games Museum are used by KairoST to build its catalog rather
than scraped directly by the launcher.

To enrich metadata, enter a **Metadata JSON URL** in **Settings…** (or keep the
equivalent `online.url` in `config.json`). The **Box-art URL template** field is
independent; leave it blank to disable web art. Both templates may use `{id}`,
`{title}`, `{filename}` and `{stem}`; values are URL-encoded. A typical custom
provider looks like:

```json
{
  "online": {
    "provider": "http",
    "mobygames_key": "your-mobygames-api-key",
    "url": "https://example.invalid/api/game/{id}",
    "art_url_template": "https://example.invalid/art/{stem}.png",
    "name": "example",
    "timeout": 8
  }
}
```

`mobygames_key` (or `MOBYGAMES_API_KEY`) enables genre lookups through
`api.mobygames.com` for titles that KairoST maps to a MobyGames id. The metadata
endpoint should return a JSON object with any of `title`, `year`, `publisher`,
`genre`, `description`, `developer`, `players`, `machine`, `resolution`, `art`.
Provider calls only run while **Online enrichment** is enabled; failures are
ignored and never block the UI.

## Machine/TOS selection

Selection order is: explicit **TOS** override, explicit **Launch as** override,
learned last-success configuration, sidecar `machine`, TOSEC tag/filename hint
(`STE`, `TT`, `Falcon`, `AGA`), then a safe Atari ST default.

| Machine | TOS preference | Common resolution |
| --- | --- | --- |
| ST | TOS 1.04 (1.02/1.00/EmuTOS fallback) | 320×200 |
| STE | TOS 1.06/1.62 (then 2.06/1.04/EmuTOS) | 320×200 |
| TT | TOS 3.06/4.04 | 640×480 |
| Falcon | TOS 4.04 only | 640×480 |

The observed resolution comes from the real captured frame; the expected
resolution is shown before launch and may be overridden by a sidecar. Starting
a different game with the same machine and TOS is an in-place disk swap
(`GameSession.swap_game()`), but it resets the emulator to boot the new game.
Inserting another disk of the **same** running multi-disk release uses a
separate runtime disk-change path that does **not** reset. A machine or TOS
change stops Hatari first and starts a new instance. Turbo boot uses
`--cpuclock 32 --fast-forward on` during boot and restores
`--cpuclock 8 --fast-forward off` once a stable changed frame appears (or after
10 seconds), so double-clicks on desktop loaders work at 8 MHz; the checkbox can
disable this. Each disk insert runs a short 32 MHz burst (2 seconds by default)
and then restores normal speed. The evidence records both the final restore and
whether the early restore fired.

### Autostarting a desktop executable

Some disks boot to GEM desktop instead of launching their game. Enter the
executable in **Launch path** (for example `DEFENDER.TOS`) and launch the game:
the launcher appends `--auto A:\DEFENDER.TOS` to the Hatari command line. Hatari
uses a virtual DESKTOP.INF to start that file, which needs TOS 1.04+ and a
program that exists on the boot disk. It only applies to a fresh boot; selecting
another disk and pressing **Insert disk** does not autostart anything. Custom
boot-sector cracktros that bypass GEM ignore `--auto`.

If a TT/Falcon TOS is not available, the profile falls back down the chain
(Falcon → TT → STE → ST) with a visible warning instead of pretending the
machine is available. TOS 3.06 is never used as a Falcon TOS.

## Evidence

On every launch attempt the launcher writes a bounded snapshot:

```text
state/run/<catalog-id>/evidence.json
state/run/<catalog-id>/frames/frame-*.bmp
```

`evidence.json` includes the `GameResult` (status, reason, observed resolution,
boot samples, log tails, turbo-boot restore state including `early_restore`), the
chosen profile, metadata provenance, art source and the normalised launch path.
The UI shows
`failure_reason`, the evidence path, and the last successful configuration,
including the last inserted disk. A clean Hatari exit that follows a
control-channel timeout is reported as `guest_cpu_halt`: that is how the modal
double bus/address halt dialog appears to the launcher, instead of the generic
`process_exit`.

## Tests

```bash
cd /home/ubuntu/Projects/Hatari/hatari_mcp/mcp/project
../.venv/bin/python -m pytest tests/examples/game_launcher -q

# only the headless GUI smoke
xvfb-run -a ../.venv/bin/python -m pytest \
  tests/examples/game_launcher/test_game_launcher_gui.py -q
```

The live test starts one or two real zipped TOSEC disks, swaps to a second disk
without restarting Hatari, stops cleanly, and checks that no Hatari process was
left behind. `test_launcher_features.py` covers title/release grouping,
multi-disk status aggregation, variant labels, launch-status colours, KairoST
metadata/local art, turbo-boot restore, disk-insert turbo, reset-free disk
insert, and cached web art without network access. The live and GUI tests skip
cleanly when the corpus, TOS image, display or Hatari binary is absent.

## Known limitations

* TOSEC filenames are parsed best-effort; KairoST and sidecars override title,
  year, publisher, genre, machine and art. Titles are grouped by normalised
  title only, so distinct titles that share a prefix stay separate.
* Title grouping intentionally does not merge remakes with different titles
  (for example "The Pictures of the Defender of the Crown").
* The catalog search index comes from parsed filenames. A sidecar-only title is
  displayed but is not part of the shared search index (use a sidecar filename
  match or the publisher/genre filters).
* One disk image per archive is mounted. `common.paths.materialize_disk`
  extracts the first supported member; multi-disk releases normally ship one
  disk per zip and are grouped by the launcher, then switched with **Insert
  disk**.
* A machine or TOS change must stop and restart Hatari. Inserting another disk
  of the same running game is in place and does not reset; starting a different
  game resets even when the profile is unchanged.
* Falcon requires TOS 4.x. Without a 4.x image the launcher falls back to a
  compatible TT/STE/ST profile instead of pretending Falcon is available.
* Artwork is PNG/GIF with stock Tk; JPEG and WebP (KairoST) need Pillow,
  otherwise the deterministic placeholder is shown. The built-in Libretro index
  covers many but not all TOSEC release filenames; a 404 simply leaves the
  placeholder. KairoST only covers titles present in its catalog.
* Online enrichment and art are on by default and enabled in **Settings…**;
  providers are provider-specific and unverified, and failures are silent.
  Missing genres trigger a MobyGames lookup when a key is configured, then a
  keyless Wikipedia lookup, cached under `state/cache/genres.json`. Turn online
  enrichment off for fully offline operation. KairoST carries no genre field;
  MobyGames blocks unauthenticated web scraping, so its API needs a free key.
* TOS discovery validates 192/256/512 KiB images; the 194,708-byte
  `emutos.img` build artifact is ignored. `HATARI_TOS` overrides discovery.
* One Hatari instance is managed at a time. Starting another game either swaps
  the disk in place or stops and restarts Hatari when the machine/TOS changes.
* `run.sh` and the Hatari window both need a working `DISPLAY`. The launcher
  shows Hatari by default and has no separate hide-window toggle; use
  `xvfb-run -a` only for headless automation.
