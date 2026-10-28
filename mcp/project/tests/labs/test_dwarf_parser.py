import os
import shutil

import pytest

os.environ.setdefault("HATARI_MCP_LABS", "1")

from labs import dwarf_spike  # noqa: E402

TOOLCHAIN_REQUIRED = pytest.mark.skipif(
    shutil.which("m68k-linux-gnu-gcc") is None or shutil.which("readelf") is None,
    reason="m68k-linux-gnu-gcc and readelf are required",
)


@TOOLCHAIN_REQUIRED
def test_line_table_matches_readelf_and_addr2line(tmp_path):
    result = dwarf_spike.run(tmp_path, sample_size=10)
    mapping = result["line_mapping"]
    assert result["compiled"]["available"] is True
    assert mapping["own_rows"] > 10
    assert mapping["exact_readelf_ratio"] >= 0.9
    assert mapping["addr2line_ratio"] >= 0.9
    assert mapping["all_samples_checked"] is True
    assert mapping["all_samples_resolved"] is True
    assert not mapping["unresolved"]
    assert result["go_line_mapping"] is True
    assert result["go_atari_end_to_end"] is False
    assert result["go_prg_relocation"] is False
    assert result["go_local_read"] is False
    assert result["locals"]


def test_missing_dwarf_reports_clear_error(tmp_path):
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"not an elf")
    with pytest.raises(dwarf_spike.DwarfError):
        dwarf_spike.parse_line_table(plain)


def test_addr2line_denominator_is_full_sample_not_successes():
    sample = [
        {"address": index, "file": "fixture.c", "line": index + 1} for index in range(50)
    ]
    reference = {(index, "fixture.c", index + 1) for index in range(50)}

    def resolve(address):
        return {"file": "fixture.c", "line": 1 if address == 0 else None}

    mapping = dwarf_spike.evaluate_samples(sample, reference, resolve, sample_size=50)
    assert mapping["addr2line_matches"] == 1
    assert mapping["addr2line_ratio"] == pytest.approx(1 / 50)
    assert mapping["all_samples_checked"] is False
    assert len(mapping["unresolved"]) == 49


def test_question_mark_line_zero_is_unresolved_and_fails_go():
    sample = [
        {"address": index, "file": "fixture.c", "line": index + 1} for index in range(50)
    ]
    reference = {(index, "fixture.c", index + 1) for index in range(50)}

    def resolve(address):
        if address < 46:
            return {"file": "fixture.c", "line": address + 1, "resolved": True}
        return {"file": "??", "line": 0, "resolved": False}

    mapping = dwarf_spike.evaluate_samples(sample, reference, resolve, sample_size=50)
    assert mapping["addr2line_matches"] == 46
    assert mapping["addr2line_ratio"] == pytest.approx(46 / 50)
    assert mapping["all_samples_checked"] is False
    assert mapping["all_samples_resolved"] is False
    assert len(mapping["unresolved"]) == 4
    assert dwarf_spike.line_mapping_ok(mapping, sample_size=50) is False


def test_addr2line_question_mark_output_is_parsed_as_unresolved(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "??\n??:0\n"

    monkeypatch.setattr(dwarf_spike.subprocess, "run", lambda *a, **k: Proc())
    result = dwarf_spike.addr2line(object(), 0x1234)
    assert result["file"] is None
    assert result["line"] is None
    assert result["resolved"] is False


@TOOLCHAIN_REQUIRED
def test_minimum_sample_size_is_enforced(tmp_path):
    result = dwarf_spike.run(tmp_path, sample_size=10_000)
    assert result["go_line_mapping"] is False
    assert "requires at least 10000" in result["verdict"]
