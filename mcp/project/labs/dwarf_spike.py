"""P8.2 DWARF line-table and local-variable spike.

No third-party Python dependency is used. A small ELF32/DWARF 2-4 line-table
parser is compared against ``readelf --debug-dump=decodedline`` and
``addr2line``. The spike then records whether an Atari-mint toolchain and
AHCC DWARF output actually exist.
"""

from __future__ import annotations

import argparse
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import require_labs
from .harness import PROJECT_DIR, make_out_dir, write_json

require_labs()

MIN_LINE_AGREEMENT = 1.0
UNRESOLVED_FILE_NAMES = {"", "?", "??"}

FIXTURE = r"""
#include <stdint.h>

struct point { int x; int y; };

static int add(int a, int b)
{
    int sum = a + b;
    return sum;
}

static int sub(int a, int b)
{
    int diff = a - b;
    return diff;
}

static int mul(int a, int b)
{
    int product = a * b;
    return product;
}

static int mix(int a, int b)
{
    int value = (a << 1) ^ b;
    value += a;
    value ^= b;
    return value;
}

static int scale(struct point *p, int factor)
{
    int local = p->x * factor;
    p->y = local + 7;
    return p->y;
}

int compute(int seed)
{
    struct point p = { seed, 0 };
    int total = 0;
    for (int i = 0; i < 5; i++) {
        total += add(seed, i);
        total += sub(seed, i);
        total += mul(seed, i);
        total += mix(seed, i);
        total ^= scale(&p, i + 1);
    }
    return total;
}

int compute2(int seed)
{
    int total = seed;
    total += add(total, 1);
    total += sub(total, 2);
    total += mul(total, 3);
    total += mix(total, 4);
    return total;
}

int main(void)
{
    return compute(3) ^ compute2(5);
}
"""


@dataclass
class Section:
    name: str
    offset: int
    size: int
    address: int
    entsize: int
    link: int
    info: int


class DwarfError(RuntimeError):
    pass


def _u16(data: bytes, offset: int, endian: str) -> int:
    return int.from_bytes(data[offset : offset + 2], "big" if endian == ">" else "little")


def _u32(data: bytes, offset: int, endian: str) -> int:
    return int.from_bytes(data[offset : offset + 4], "big" if endian == ">" else "little")


def _i8(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 1], "big", signed=True)


def _uleb(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def _sleb(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40:
                result |= -(1 << shift)
            return result, offset


def read_elf_sections(path: Path) -> tuple[dict[str, Section], str, int]:
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        raise DwarfError(f"not an ELF file: {path}")
    elf_class = data[4]
    elf_data = data[5]
    if elf_class != 1:
        raise DwarfError(f"only ELF32 is supported, class={elf_class}")
    endian = ">" if elf_data == 2 else "<"
    if endian == "<":
        raise DwarfError("only big-endian m68k ELF is supported by this spike")
    e_shoff = _u32(data, 32, endian)
    e_shentsize = _u16(data, 46, endian)
    e_shnum = _u16(data, 48, endian)
    e_shstrndx = _u16(data, 50, endian)
    raw: list[tuple[int, ...]] = []
    for index in range(e_shnum):
        base = e_shoff + index * e_shentsize
        fields = tuple(_u32(data, base + 4 * item, endian) for item in range(10))
        raw.append(fields)
    if e_shstrndx >= len(raw):
        raise DwarfError("invalid section-name string table index")
    str_offset = raw[e_shstrndx][4]
    str_size = raw[e_shstrndx][5]
    strings = data[str_offset : str_offset + str_size]
    sections: dict[str, Section] = {}
    for fields in raw:
        name_offset, _, _, address, offset, size, link, info, _, entsize = fields
        end = strings.find(b"\x00", name_offset)
        name = strings[name_offset:end].decode("ascii", "replace")
        sections[name] = Section(name, offset, size, address, entsize, link, info)
    return sections, endian, e_shoff


def _decode_file_names(data: bytes, offset: int, endian: str) -> tuple[list[str], list[tuple[str, int]], int]:
    dirs: list[str] = []
    while True:
        end = data.find(b"\x00", offset)
        if end < 0:
            raise DwarfError("unterminated include directory table")
        value = data[offset:end].decode("utf-8", "replace")
        offset = end + 1
        if not value:
            break
        dirs.append(value)
    files: list[tuple[str, int]] = []
    while True:
        end = data.find(b"\x00", offset)
        if end < 0:
            raise DwarfError("unterminated file name table")
        name = data[offset:end].decode("utf-8", "replace")
        offset = end + 1
        if not name:
            break
        directory, offset = _uleb(data, offset)
        _, offset = _uleb(data, offset)
        _, offset = _uleb(data, offset)
        files.append((name, directory))
    return dirs, files, offset


def parse_line_table(path: Path) -> dict[str, Any]:
    sections, endian, _ = read_elf_sections(path)
    if ".debug_line" not in sections:
        raise DwarfError("missing .debug_line section")
    section = sections[".debug_line"]
    data = path.read_bytes()[section.offset : section.offset + section.size]
    cursor = 0
    rows: list[dict[str, Any]] = []
    units = 0
    while cursor < len(data):
        unit_length = _u32(data, cursor, endian)
        if unit_length == 0:
            break
        unit_end = cursor + 4 + unit_length
        version = _u16(data, cursor + 4, endian)
        if version >= 5:
            raise DwarfError("DWARF 5 line tables are not parsed by this spike; compile with -gdwarf-4")
        header_length = _u32(data, cursor + 6, endian)
        pos = cursor + 10
        min_inst = data[pos]
        pos += 1
        if version >= 4:
            max_ops = data[pos]
            pos += 1
        else:
            max_ops = 1
        default_is_stmt = data[pos]
        pos += 1
        line_base = _i8(data, pos)
        pos += 1
        line_range = data[pos]
        pos += 1
        opcode_base = data[pos]
        pos += 1
        std_lengths = list(data[pos : pos + opcode_base - 1])
        pos += opcode_base - 1
        dirs, files, pos = _decode_file_names(data, pos, endian)
        if not files:
            raise DwarfError("empty DWARF file table")
        file_names = [item[0] for item in files]
        state = {
            "address": 0,
            "op_index": 0,
            "file": 1,
            "line": 1,
            "column": 0,
            "is_stmt": bool(default_is_stmt),
            "end_sequence": False,
        }

        def advance(operation_advance: int) -> None:
            if max_ops <= 1:
                state["address"] += min_inst * operation_advance
                state["op_index"] = 0
            else:
                total = state["op_index"] + operation_advance
                state["address"] += min_inst * (total // max_ops)
                state["op_index"] = total % max_ops

        while pos < unit_end:
            opcode = data[pos]
            pos += 1
            if opcode >= opcode_base:
                adjusted = opcode - opcode_base
                advance(adjusted // line_range)
                state["line"] += line_base + (adjusted % line_range)
                if state["line"] and state["file"] <= len(file_names):
                    rows.append(
                        {
                            "address": state["address"],
                            "file": file_names[state["file"] - 1],
                            "line": state["line"],
                            "column": state["column"],
                            "is_stmt": state["is_stmt"],
                        }
                    )
                continue
            if opcode == 0:
                length, pos = _uleb(data, pos)
                sub_end = pos + length
                sub = data[pos]
                pos += 1
                if sub == 1:
                    rows.append(
                        {
                            "address": state["address"],
                            "file": file_names[state["file"] - 1]
                            if state["file"] <= len(file_names)
                            else "",
                            "line": state["line"],
                            "column": state["column"],
                            "is_stmt": state["is_stmt"],
                            "end_sequence": True,
                        }
                    )
                    state = {
                        "address": 0,
                        "op_index": 0,
                        "file": 1,
                        "line": 1,
                        "column": 0,
                        "is_stmt": bool(default_is_stmt),
                        "end_sequence": False,
                    }
                elif sub == 2:
                    state["address"] = _u32(data, pos, endian)
                pos = sub_end
            elif opcode == 1:
                if state["line"] and state["file"] <= len(file_names):
                    rows.append(
                        {
                            "address": state["address"],
                            "file": file_names[state["file"] - 1],
                            "line": state["line"],
                            "column": state["column"],
                            "is_stmt": state["is_stmt"],
                        }
                    )
            elif opcode == 2:
                value, pos = _uleb(data, pos)
                advance(value)
            elif opcode == 3:
                value, pos = _sleb(data, pos)
                state["line"] += value
            elif opcode == 4:
                state["file"], pos = _uleb(data, pos)
            elif opcode == 5:
                state["column"], pos = _uleb(data, pos)
            elif opcode == 6:
                state["is_stmt"] = not state["is_stmt"]
            elif opcode == 7:
                pass
            elif opcode == 8:
                advance((255 - opcode_base) // line_range)
            elif opcode == 9:
                state["address"] += _u16(data, pos, endian)
                state["op_index"] = 0
                pos += 2
            elif opcode == 10:
                pass
            elif opcode == 11:
                pass
            elif opcode == 12:
                _, pos = _uleb(data, pos)
            else:
                for _ in range(std_lengths[opcode - 1]):
                    _, pos = _uleb(data, pos)
        cursor = unit_end
        units += 1
    return {
        "rows": [row for row in rows if row["line"] and not row.get("end_sequence")],
        "units": units,
        "directories": dirs,
    }


DECODED_RE = re.compile(r"^(?P<file>\S+)\s+(?P<line>\d+)\s+0x(?P<addr>[0-9a-fA-F]+)")


def _basename(value: str | None) -> str | None:
    if not value:
        return value
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def readelf_decoded_rows(path: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["readelf", "--debug-dump=decodedline", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = []
    for line in proc.stdout.splitlines():
        match = DECODED_RE.match(line.strip())
        if match:
            rows.append(
                {
                    "file": _basename(match.group("file")),
                    "line": int(match.group("line")),
                    "address": int(match.group("addr"), 16),
                }
            )
    return rows


def addr2line(path: Path, address: int) -> dict[str, Any]:
    proc = subprocess.run(
        ["addr2line", "-e", str(path), "-f", "-i", f"0x{address:x}"],
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    location = lines[1] if len(lines) > 1 else ""
    match = re.match(r"(.*):(\d+)", location)
    file = _basename(match.group(1)) if match else None
    line = int(match.group(2)) if match else None
    if file in UNRESOLVED_FILE_NAMES or line is None or line <= 0:
        file = None
        line = None
    return {
        "function": lines[0] if lines else "",
        "location": location,
        "file": file,
        "line": line,
        "resolved": file is not None and line is not None,
    }


def _is_resolved(reverse: dict[str, Any]) -> bool:
    line = reverse.get("line")
    file = _basename(reverse.get("file"))
    return bool(
        line is not None
        and int(line) > 0
        and file not in UNRESOLVED_FILE_NAMES
        and file is not None
    )


def evaluate_samples(
    sample: list[dict[str, Any]],
    reference_set: set[tuple[int, str | None, int]],
    resolve: Any,
    *,
    sample_size: int,
) -> dict[str, Any]:
    """Evaluate a line-table sample with honest denominators.

    ``addr2line_ratio`` always divides by ``len(sample)``, not by the number of
    rows that happened to resolve. A single successful lookup out of fifty is
    2%, never 100%.
    """
    exact_matches = 0
    addr2line_matches = 0
    addr2line_checked = 0
    samples = []
    for row in sample:
        exact = (row["address"], _basename(row["file"]), row["line"]) in reference_set
        exact_matches += int(exact)
        reverse = resolve(row["address"])
        checked = _is_resolved(reverse)
        if checked:
            addr2line_checked += 1
            match = reverse["line"] == row["line"] and _basename(
                reverse.get("file")
            ) == _basename(row["file"])
            addr2line_matches += int(match)
        samples.append(
            {
                "address": row["address"],
                "file": row["file"],
                "line": row["line"],
                "exact_readelf_match": exact,
                "addr2line": reverse,
            }
        )
    total = len(sample)
    exact_ratio = exact_matches / total if total else 0.0
    addr_ratio = addr2line_matches / total if total else 0.0
    unresolved = [item for item in samples if not _is_resolved(item["addr2line"])]
    return {
        "sample_size": total,
        "requested_sample_size": sample_size,
        "exact_readelf_matches": exact_matches,
        "exact_readelf_ratio": exact_ratio,
        "addr2line_checked": addr2line_checked,
        "addr2line_matches": addr2line_matches,
        "addr2line_ratio": addr_ratio,
        "all_samples_checked": bool(total and addr2line_checked == total),
        "all_samples_resolved": bool(total and not unresolved),
        "unresolved": unresolved,
        "samples": samples,
    }


def line_mapping_ok(
    mapping: dict[str, Any],
    *,
    sample_size: int,
    threshold: float = MIN_LINE_AGREEMENT,
) -> bool:
    """A line-mapping GO requires every requested sample to resolve and agree."""
    return bool(
        int(mapping.get("sample_size") or 0) >= sample_size
        and mapping.get("all_samples_checked")
        and mapping.get("all_samples_resolved")
        and not mapping.get("unresolved")
        and float(mapping.get("exact_readelf_ratio") or 0.0) >= threshold
        and float(mapping.get("addr2line_ratio") or 0.0) >= threshold
    )


def _find_compiler() -> tuple[str | None, list[str]]:
    candidates = [
        ("m68k-atari-mint-gcc", ["-g", "-O0", "-gdwarf-4"]),
        ("m68k-linux-gnu-gcc", ["-g", "-O0", "-gdwarf-4"]),
        ("gcc", ["-g", "-O0", "-gdwarf-4"]),
    ]
    for name, flags in candidates:
        if shutil.which(name):
            return name, flags
    return None, []


def compile_fixture(work: Path) -> dict[str, Any]:
    compiler, flags = _find_compiler()
    if not compiler:
        return {"available": False, "reason": "no C compiler found"}
    source = work / "p8_dwarf_fixture.c"
    source.write_text(FIXTURE, encoding="utf-8")
    elf = work / "p8_dwarf_fixture.elf"
    command = [compiler, *flags, str(source), "-o", str(elf)]
    proc = subprocess.run(command, capture_output=True, text=True)
    linked = proc.returncode == 0
    if not linked:
        elf = work / "p8_dwarf_fixture.o"
        command = [compiler, *flags, "-c", str(source), "-o", str(elf)]
        proc = subprocess.run(command, capture_output=True, text=True)
    return {
        "available": proc.returncode == 0,
        "compiler": compiler,
        "flags": flags,
        "linked": linked,
        "source": str(source),
        "elf": str(elf),
        "command": command,
        "stderr": proc.stderr[-2000:],
    }


def inspect_locals(path: Path, function: str = "compute") -> list[dict[str, str]]:
    proc = subprocess.run(
        ["readelf", "--debug-dump=info", str(path)], capture_output=True, text=True
    )
    lines = proc.stdout.splitlines()
    in_function = False
    locals_: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if "DW_TAG_subprogram" in line:
            in_function = False
            current = None
        if not in_function and "DW_AT_name" in line and f": {function}" in line:
            in_function = True
            continue
        if not in_function:
            continue
        if "DW_TAG_subprogram" in line or re.match(r" <1><[0-9a-f]+>", line):
            if locals_ or current:
                break
        if "DW_TAG_variable" in line:
            if current:
                locals_.append(current)
            current = {}
        if current is not None:
            if "DW_AT_name" in line:
                value = line.split(":", 1)[-1].strip()
                if "): " in value:
                    value = value.rsplit("): ", 1)[-1]
                current["name"] = value
            elif "DW_AT_location" in line:
                current["location"] = line.split(":", 1)[-1].strip()
    if current:
        locals_.append(current)
    return locals_


def inspect_ahcc() -> dict[str, Any]:
    ahcc = PROJECT_DIR / "tools/ahcc/ahcc/HELLO"
    binary = ahcc / "HELLO_ST.TOS"
    result: dict[str, Any] = {"directory": str(ahcc), "files": []}
    for name in ("HELLO_ST.TOS", "HELLO_ST.ADB", "HELLO_ST.MAP"):
        path = ahcc / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        result["files"].append(
            {
                "name": name,
                "size": len(raw),
                "elf": raw[:4] == b"\x7fELF",
                "dwarf_marker": b".debug_info" in raw or b"DWARF" in raw,
                "magic": raw[:16].hex(),
            }
        )
    if binary.is_file():
        proc = subprocess.run(["readelf", "-h", str(binary)], capture_output=True, text=True)
        result["readelf"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout[:400],
            "stderr": proc.stderr[:400],
        }
    return result


def run(out_dir: Path, sample_size: int = 50) -> dict[str, Any]:
    work = out_dir / "dwarf"
    work.mkdir(parents=True, exist_ok=True)
    compiled = compile_fixture(work)
    result: dict[str, Any] = {
        "spike": "P8.2",
        "compiled": compiled,
        "toolchains": {
            "m68k-atari-mint-gcc": shutil.which("m68k-atari-mint-gcc"),
            "m68k-linux-gnu-gcc": shutil.which("m68k-linux-gnu-gcc"),
            "readelf": shutil.which("readelf"),
            "addr2line": shutil.which("addr2line"),
            "objdump": shutil.which("objdump"),
        },
        "ahcc": inspect_ahcc(),
        "line_mapping": {},
        "locals": [],
        "limitations": [
            "No m68k-atari-mint toolchain is installed in this environment.",
            "The fixture is a linked m68k Linux ELF, not a TOS PRG; PRG relocation is untested.",
            "AHCC TOS/ADB/PDB outputs are not ELF and carry no DWARF.",
        ],
    }
    if not compiled.get("available"):
        result["verdict"] = "NO-GO: no compiler available"
        return result
    elf = Path(compiled["elf"])
    try:
        parsed = parse_line_table(elf)
    except DwarfError as exc:
        result["line_mapping"] = {"error": str(exc)}
        result["verdict"] = f"NO-GO: {exc}"
        return result
    reference = readelf_decoded_rows(elf)
    reference_set = {
        (row["address"], _basename(row["file"]), row["line"]) for row in reference
    }
    rows = parsed["rows"]
    if len(rows) < sample_size:
        result["line_mapping"] = {
            "error": f"only {len(rows)} line-table rows; at least {sample_size} required",
            "own_rows": len(rows),
            "requested_sample_size": sample_size,
        }
        result["verdict"] = (
            f"NO-GO: fixture produced only {len(rows)} DWARF line rows; "
            f"the spike requires at least {sample_size}"
        )
        result["go_line_mapping"] = False
        result["go_atari_end_to_end"] = False
        result["go_parser_agreement"] = False
        return result
    rng = random.Random(8128)
    sample = rng.sample(rows, sample_size)
    mapping = evaluate_samples(
        sample, reference_set, lambda address: addr2line(elf, address), sample_size=sample_size
    )
    mapping.update(
        {
            "own_rows": len(rows),
            "readelf_rows": len(reference),
            "units": parsed["units"],
        }
    )
    result["line_mapping"] = mapping
    result["locals"] = inspect_locals(elf, "compute")
    line_ok = line_mapping_ok(mapping, sample_size=sample_size)
    result["verdict"] = (
        "GO (limited to parser agreement on m68k Linux ELF): line-table parser "
        "agrees with readelf and addr2line on every row of the full sample. "
        "NO-GO for Atari PRG relocation or local-read claims; the "
        "m68k-atari-mint toolchain is missing and AHCC output has no DWARF."
        if line_ok
        else (
            "NO-GO: line mapping was unresolved or did not agree on every row "
            "of the full requested sample (?? and line 0 count as unresolved)"
        )
    )
    result["go_line_mapping"] = line_ok
    result["go_parser_agreement"] = line_ok
    result["go_scope"] = "parser agreement on m68k Linux ELF only"
    result["go_atari_end_to_end"] = False
    result["go_prg_relocation"] = False
    result["go_local_read"] = False
    result["locals_verified_in_guest"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--sample-size", type=int, default=50)
    args = parser.parse_args(argv)
    out_dir = args.out or make_out_dir("dwarf")
    result = run(out_dir, sample_size=args.sample_size)
    write_json(out_dir / "dwarf.json", result)
    print(f"wrote {out_dir / 'dwarf.json'}")
    mapping = result.get("line_mapping", {})
    print(
        f"line exact={mapping.get('exact_readelf_ratio')} "
        f"addr2line={mapping.get('addr2line_ratio')} locals={len(result.get('locals', []))}"
    )
    print(result.get("verdict"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
