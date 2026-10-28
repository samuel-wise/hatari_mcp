"""Machine/TOS choice and hot-swap compatibility tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import Config, paths  # noqa: E402
from game_launcher.profiles import (  # noqa: E402
    choose_profile,
    incompatibility_reason,
)


def _tos_files(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for key in ("emutos", "tos104", "tos106", "tos162", "tos206", "tos306", "tos404"):
        path = tmp_path / f"{key}.img"
        path.write_bytes(b"\0" * 196608)
        result[key] = path
    return result


def _entry(
    tmp_path: Path,
    name: str = "Game (1990)(Pub).zip",
    tags: tuple[str, ...] = (),
    title: str = "Game",
):
    disk = tmp_path / name
    disk.write_bytes(b"PK" + b"\0" * 8)
    return SimpleNamespace(
        id="game-1",
        path=disk,
        filename=name,
        title=title,
        year=1990,
        publisher="Pub",
        genre=None,
        tags=tags,
    )


def test_default_profile_is_st_with_tos104(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    profile = choose_profile(_entry(tmp_path), tos_candidates=tos)
    assert profile.machine == "st"
    assert profile.tos == tos["tos104"]
    assert profile.tos_source == "auto:tos104"
    assert profile.expected_resolution == (320, 200)
    assert profile.source == "default"
    assert not profile.warnings


def test_filename_tags_select_ste_and_tt_when_tos_exists(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    ste = choose_profile(
        _entry(tmp_path, "Menu (1990)(Pub)[STE fixed].zip"), tos_candidates=tos
    )
    assert ste.machine == "ste"
    assert ste.source == "filename"
    assert ste.tos == tos["tos106"]

    tt = choose_profile(
        _entry(tmp_path, "TT Demo (1994)(Pub)[TT].zip"), tos_candidates=tos
    )
    assert tt.machine == "tt"
    assert tt.tos == tos["tos306"]
    assert tt.expected_resolution == (640, 480)


def test_falcon_requires_tos404_and_falls_back(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    falcon = choose_profile(
        _entry(tmp_path, "Falcon Game (1995)(Pub)[Falcon].zip"), tos_candidates=tos
    )
    assert falcon.machine == "falcon"
    assert falcon.tos == tos["tos404"]

    without_404 = {key: value for key, value in tos.items() if key != "tos404"}
    fallback = choose_profile(
        _entry(tmp_path, "Falcon Game (1995)(Pub)[Falcon].zip"),
        tos_candidates=without_404,
    )
    assert fallback.machine == "tt"
    assert fallback.tos == tos["tos306"]
    assert fallback.source.endswith("+fallback")
    assert fallback.warnings
    assert "Falcon" in fallback.warnings[0]


def test_learned_config_wins_and_is_persisted(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    cfg = Config(tmp_path / "config.json")
    cfg.record_launch(
        "game-1",
        machine="ste",
        profile="ste",
        tos=str(tos["emutos"]),
        args=[],
        resolution=(320, 200),
        status="success",
        success=True,
    )
    entry = _entry(tmp_path, "Menu (1990)(Pub)[Falcon].zip")
    metadata = SimpleNamespace(machine="falcon", resolution=None, args=())
    profile = choose_profile(
        entry, config=cfg, metadata=metadata, tos_candidates=tos
    )
    assert profile.machine == "ste"
    assert profile.source == "learned"
    assert profile.tos == tos["emutos"].resolve()


def test_requested_machine_and_tos_override_everything(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    profile = choose_profile(
        _entry(tmp_path),
        requested_machine="tt",
        requested_tos=tos["tos404"],
        tos_candidates=tos,
    )
    assert profile.machine == "tt"
    assert profile.source == "requested"
    assert profile.tos == tos["tos404"].resolve()
    assert profile.tos_source == "requested"


def test_metadata_args_and_resolution_are_used_and_disk_args_filtered(
    tmp_path: Path,
) -> None:
    tos = _tos_files(tmp_path)
    metadata = SimpleNamespace(
        machine="ste",
        resolution=[640, 200],
        args=["--disk-a", "/tmp/x.st", "--fast-boot", "off"],
    )
    profile = choose_profile(
        _entry(tmp_path), metadata=metadata, tos_candidates=tos
    )
    assert profile.machine == "ste"
    assert profile.source == "sidecar"
    assert profile.expected_resolution == (640, 200)
    assert profile.extra_args == ("--fast-boot", "off")


def test_incompatibility_reason() -> None:
    profile = SimpleNamespace(machine="st", tos=Path("/tmp/tos.img"))
    assert (
        incompatibility_reason("st", "/tmp/tos.img", profile, current_game_id="g")
        is None
    )
    assert "machine change" in (
        incompatibility_reason("ste", "/tmp/tos.img", profile, current_game_id="g")
        or ""
    )
    assert "TOS change" in (
        incompatibility_reason("st", "/tmp/other.img", profile, current_game_id="g")
        or ""
    )
    assert "no tracked game" in (
        incompatibility_reason("st", "/tmp/tos.img", profile, current_game_id=None)
        or ""
    )


def test_tos_image_size_validation(tmp_path: Path) -> None:
    invalid = tmp_path / "emutos.img"
    invalid.write_bytes(b"\0" * 194708)
    assert not paths.is_valid_tos(invalid)
    for size in (196608, 262144, 524288):
        valid = tmp_path / f"tos-{size}.img"
        valid.write_bytes(b"\0" * size)
        assert paths.is_valid_tos(valid)


def test_invalid_emutos_is_skipped_for_a_valid_tos104(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    invalid = tmp_path / "bad-emutos.img"
    invalid.write_bytes(b"\0" * 194708)
    tos["emutos"] = invalid
    profile = choose_profile(_entry(tmp_path), tos_candidates=tos)
    assert profile.tos == tos["tos104"]
    assert profile.tos_source == "auto:tos104"


def test_title_words_do_not_select_machine(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    cases = (
        ("Falcon (1990)(Pub).zip", "Falcon"),
        ("Falcon Patrol (1991)(Pub).zip", "Falcon Patrol"),
        ("Game (1990)(Pub).zip", "Falcon"),
    )
    for name, title in cases:
        profile = choose_profile(
            _entry(tmp_path, name=name, title=title), tos_candidates=tos
        )
        assert profile.machine == "st"
        assert profile.source == "default"


def test_explicit_machine_markers_detect_machine(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    cases = (
        ("[Falcon]", "falcon"),
        ("[STE fixed]", "ste"),
        ("[TT]", "tt"),
        ("(TT030)", "tt"),
        ("[AGA]", "falcon"),
        ("[Videl]", "falcon"),
    )
    for marker, expected in cases:
        entry = _entry(tmp_path, name=f"Game (1990)(Pub){marker}.zip")
        profile = choose_profile(entry, tos_candidates=tos)
        assert profile.machine == expected
        assert profile.source == "filename"


def test_falcon_disk_requirement_is_not_a_machine_marker(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    entry = _entry(
        tmp_path,
        name="Falcon Mission Disk (1991)(Pub)[needs Falcon Disk 2].zip",
    )
    profile = choose_profile(entry, tos_candidates=tos)
    assert profile.machine == "st"
    assert profile.source == "default"

    tagged = _entry(
        tmp_path,
        name="Game (1990)(Pub).zip",
        tags=("needs Falcon Disk 2",),
    )
    profile = choose_profile(tagged, tos_candidates=tos)
    assert profile.machine == "st"
    assert profile.source == "default"


def test_sidecar_machine_wins_over_title_word(tmp_path: Path) -> None:
    tos = _tos_files(tmp_path)
    entry = _entry(tmp_path, name="Falcon (1990)(Pub).zip", title="Falcon")
    metadata = SimpleNamespace(machine="ste")
    profile = choose_profile(entry, metadata=metadata, tos_candidates=tos)
    assert profile.machine == "ste"
    assert profile.source == "sidecar"
