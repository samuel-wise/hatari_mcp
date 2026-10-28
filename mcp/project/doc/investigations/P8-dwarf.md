# P8.2 — DWARF source-debugging spike

Status: complete as a parser-agreement spike on m68k Linux ELF
Verdict: GO only for line-table parser agreement on m68k Linux ELF; NO-GO for Atari PRG/relocation and local-read claims
Date: 2026-10-27
Owner: P8 research spike
Review: independent review pending; self-review recorded in the P8 plan
Harness: `mcp/project/labs/dwarf_spike.py`

## Question and hypothesis

Can m68k ELF/DWARF line information be mapped reliably enough for file:line
breakpoints and local-variable inspection, given that Hatari symbols carry no
line numbers and AHCC may emit no DWARF? Hypothesis: line tables are
mechanically parseable, but an Atari-mint toolchain and a TOS PRG relocation
model are hard prerequisites.

## Pinned environment

| Item | Value |
| --- | --- |
| Host | Linux 6.17.0-1020-oracle, aarch64 |
| `m68k-atari-mint-gcc` | absent |
| `m68k-linux-gnu-gcc` | present (`/usr/bin/m68k-linux-gnu-gcc`) |
| `readelf` / `addr2line` / `objdump` | present |
| Fixture | linked m68k Linux ELF, `-g -O0 -gdwarf-4`, 62 line rows |
| AHCC corpus | `tools/ahcc/ahcc/HELLO/HELLO_ST.{TOS,ADB,MAP}` |

The fixture is a Linux m68k ELF, not an Atari TOS executable. It proves parser
and mapping behaviour only; it does not prove PRG relocation or a guest local
read.

## Method

Command:

```sh
cd mcp/project
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs dwarf \
  --out /tmp/hatari-mcp-p8-audit/dwarf
```

1. Compile a C fixture whose line table has 62 rows with
   `m68k-linux-gnu-gcc -g -O0 -gdwarf-4`.
2. Parse `.debug_line` with a small in-repo ELF32/DWARF 2–4 parser; no
   `pyelftools` dependency was added.
3. Require at least the requested sample size (50 by default). If fewer rows
   exist, the spike returns NO-GO instead of silently shrinking the sample.
4. Sample exactly 50 `(address, file, line)` rows and compare them against
   `readelf --debug-dump=decodedline`.
5. For every sampled address, compare `addr2line -f -i`. The denominator is
   `len(sample)`, not the number of successful lookups. A single success out of
   fifty is 2%, never 100%. `??`, empty file names and line `0` are treated as
   unresolved; every sample must resolve and agree, so the agreement threshold
   is 100%, not 90%.
6. Extract locals for `compute` from `readelf --debug-dump=info`.
7. Inspect AHCC HELLO outputs for ELF/DWARF markers and run `readelf -h`.

## Results

Raw data: `/tmp/hatari-mcp-p8-audit/dwarf/dwarf/dwarf.json`.

### Toolchain and corpus

| Check | Result |
| --- | --- |
| `m68k-atari-mint-gcc` | missing |
| `m68k-linux-gnu-gcc` | available; produces big-endian m68k ELF32 |
| `.debug_line` rows from fixture | 62 |
| Requested/sample rows | 50/50 |
| Exact match against `readelf` decoded line | 50/50 (100%) |
| Round-trip match through `addr2line` | 50/50 (100%) |
| All samples resolved | yes |
| Unresolved samples (`??`, line 0) | 0 |
| DWARF version produced with `-gdwarf-4` | 4 |
| Locals found for `compute` | `p` at `a6-16`, `total` at `a6-4` |
| AHCC `HELLO_ST.TOS` is ELF | no |
| AHCC `HELLO_ST.TOS` contains DWARF markers | no |
| AHCC `.ADB`/`.PDB` contains DWARF | no |
| `readelf -h HELLO_ST.TOS` | fails: not an ELF file |
| Local read verified from a stopped guest | **no** |

The parser read the DWARF 4 fixture correctly; its header paths cover the
DWARF 2–4 line-table forms. The fixture's locals use `DW_OP_breg14` (a6) with
constant offsets, so a frame-pointer-relative read would be possible for simple
non-optimized frames. CFI/frame-base resolution was not needed for this fixture
and remains untested. The locals were only inspected in DWARF; none was read
from a running guest.

### AHCC observation

AHCC emits a TOS executable plus its own `.ADB`/`.PDB` metadata and a `.MAP`
offset map. None of these are ELF and none contain DWARF. The map gives
function/symbol offsets, not source lines. Therefore AHCC-built HELLO cannot
provide file:line breakpoints or local-variable DWARF.

### PRG relocation

No Atari-mint toolchain is installed, so a TOS PRG with DWARF could not be
built or run. The coverage spike independently observed the HELLO_ST text load
base at `0x00f176` on TOS 1.62. A future implementation must add the PRG load
base to ELF section addresses, handle relocation/overlay, and verify that the
symbol table survives the PRG conversion. That model is **not** proven here.

## GO / NO-GO

- **GO (narrow): parser agreement on m68k Linux ELF.** The minimal parser
  agreed with `readelf` and `addr2line` on every row of the full 50-row sample
  with no unresolved rows. The GO threshold is 100% agreement, not 90%.
- **NO-GO for Atari end-to-end source debugging in this environment.** The
  required `m68k-atari-mint` toolchain is missing, the available AHCC corpus has
  no DWARF, and PRG relocation was not tested.
- **NO-GO for local-read claims.** Locals were found in DWARF but not read from
  a stopped guest.
- Do not advertise file:line breakpoints for AHCC or for TOS PRGs until those
  prerequisites are met.

## Limitations and safety

- Linux m68k ELF is not a TOS PRG; load-base and relocation behaviour differ.
- Only DWARF 2–4 line tables are parsed; DWARF 5 is rejected with a clear
  error. Default GCC output here is DWARF 3, but a future toolchain may emit 5.
- Only simple frame-pointer locals are assessed; optimized code, CFI,
  expression evaluation, and register-allocated variables are untested.
- The 50-row minimum is enforced; a fixture with fewer rows is NO-GO rather
  than a smaller sample.
- `addr2line` output such as `??:0` is unresolved, not a successful line-0
  match. A sample of 46 agreements plus four `??:0` lookups is NO-GO.
- No source editor, expression evaluator, or production code was added.
- No `pyelftools` dependency was added; no ROM or third-party source was
  copied. Remove `/tmp/hatari-mcp-p8-*` when the investigation is closed.

## Promotion prerequisites

Install and pin an `m68k-atari-mint` toolchain that emits DWARF, build a small
PRG with `-g`, document the PRG load/relocation model, test 50+ line mappings
on the real PRG, read one local from a stopped guest, and test the optimized
failure mode. Also decide whether DWARF 5 and `pyelftools` are acceptable.
