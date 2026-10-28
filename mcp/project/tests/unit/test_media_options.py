"""Unit tests for media/device command builders and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hatari_mcp.errors import HatariError
from hatari_mcp.tools import devices, media


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _st_image(tmp_path: Path, name: str = "disk.st") -> Path:
    image = bytearray(720 * 1024)
    image[13] = 2  # sectors per cluster, as in a formatted FAT12 floppy
    return _write(tmp_path / name, bytes(image))


def _msa_image(tmp_path: Path, name: str = "disk.msa") -> Path:
    return _write(tmp_path / name, b"\x0e\x0f" + b"\0" * 64)


def _hd_image(tmp_path: Path, name: str = "hd.img") -> Path:
    return _write(tmp_path / name, b"\0" * (1024 * 1024))


def _cart_image(tmp_path: Path, name: str = "cart.img") -> Path:
    return _write(tmp_path / name, b"\0" * (128 * 1024))


def test_floppy_option_builders(tmp_path: Path) -> None:
    image = _st_image(tmp_path)
    assert media.build_floppy_option("A", str(image)) == f"--disk-a {image}"
    assert media.build_floppy_option("b", str(image)) == f"--disk-b {image}"
    assert media.build_floppy_eject_option("A") == "--disk-a none"
    assert media.build_floppy_eject_option("b") == "--disk-b none"


def test_floppy_option_escapes_spaces(tmp_path: Path) -> None:
    image = _st_image(tmp_path, "disk one.st")
    option = media.build_floppy_option("a", str(image))
    assert option == f"--disk-a {image.parent}/disk\\ one.st"


def test_option_escaping_does_not_double_backslashes() -> None:
    assert media.escape_option_arg("/tmp/a b") == "/tmp/a\\ b"
    assert media.escape_option_arg("/tmp/a\\ b") == "/tmp/a\\\\ b"

    with pytest.raises(HatariError) as excinfo:
        media.escape_option_arg("/tmp/a\nb")
    assert excinfo.value.code == "bad_option"


def test_restart_required_error_carries_exact_option() -> None:
    with pytest.raises(HatariError) as excinfo:
        media._restart_required("--acsi 0=/tmp/hd.img", "message", "hint")
    assert excinfo.value.code == "restart_required"
    assert excinfo.value.extra["option"] == "--acsi 0=/tmp/hd.img"
    assert excinfo.value.extra["command"] == "hatari-option --acsi 0=/tmp/hd.img"


def test_floppy_validation_accepts_st_and_msa(tmp_path: Path) -> None:
    assert media.validate_floppy_image(_st_image(tmp_path)) == _st_image(tmp_path)
    assert media.validate_floppy_image(_msa_image(tmp_path)) == _msa_image(tmp_path)


def test_floppy_rejects_bad_extension_and_capslib_formats(tmp_path: Path) -> None:
    text = _write(tmp_path / "disk.txt", b"not a disk")
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(text)
    assert excinfo.value.code == "unsupported_format"

    for suffix in (".stx", ".ipf"):
        special = _write(tmp_path / f"disk{suffix}", b"\0" * 512)
        with pytest.raises(HatariError) as excinfo:
            media.validate_floppy_image(special)
        assert excinfo.value.code == "unsupported_format"


def test_floppy_rejects_empty_and_malformed_images(tmp_path: Path) -> None:
    empty = _write(tmp_path / "empty.st", b"")
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(empty)
    assert excinfo.value.code == "invalid_image"

    malformed = _write(tmp_path / "bad.msa", b"nope" + b"\0" * 64)
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(malformed)
    assert excinfo.value.code == "invalid_image"

    unaligned = _write(tmp_path / "odd.st", b"\0" * 513)
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(unaligned)
    assert excinfo.value.code == "invalid_image"

    zeroed = _write(tmp_path / "zeroed.st", b"\0" * (720 * 1024))
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(zeroed)
    assert excinfo.value.code == "invalid_image"
    assert "Pacifist" in excinfo.value.hint


def test_floppy_rejects_missing_and_invalid_drive(tmp_path: Path) -> None:
    with pytest.raises(HatariError) as excinfo:
        media.validate_floppy_image(tmp_path / "missing.st")
    assert excinfo.value.code == "invalid_path"

    with pytest.raises(HatariError) as excinfo:
        media.normalize_drive("C")
    assert excinfo.value.code == "bad_option"


def test_hd_mount_options(tmp_path: Path) -> None:
    image = _hd_image(tmp_path)
    assert media.build_hd_mount_option("acsi", str(image)) == f"--acsi 0={image}"
    assert media.build_hd_mount_option("scsi", str(image), 7) == f"--scsi 7={image}"
    assert media.build_hd_mount_option("ide-master", str(image)) == f"--ide-master {image}"
    assert media.build_hd_mount_option("ide_slave", str(image)) == f"--ide-slave {image}"
    assert media.build_hd_mount_option("ide-master", str(image), 0) == f"--ide-master {image}"


def test_hd_unmount_options_use_none() -> None:
    assert media.build_hd_unmount_option("acsi", 3) == "--acsi 3=none"
    assert media.build_hd_unmount_option("scsi", 0) == "--scsi 0=none"
    assert media.build_hd_unmount_option("ide-master") == "--ide-master none"
    assert media.build_hd_unmount_option("ide-slave", 1) == "--ide-slave none"


def test_hd_target_id_validation(tmp_path: Path) -> None:
    image = _hd_image(tmp_path)
    for bad_id in (-1, 8):
        with pytest.raises(HatariError) as excinfo:
            media.build_hd_mount_option("acsi", str(image), bad_id)
        assert excinfo.value.code == "bad_option"

    with pytest.raises(HatariError) as excinfo:
        media.build_hd_mount_option("ide-slave", str(image), 0)
    assert excinfo.value.code == "bad_option"

    with pytest.raises(HatariError) as excinfo:
        media.build_hd_mount_option("floppy", str(image))
    assert excinfo.value.code == "bad_option"


def test_hd_image_validation(tmp_path: Path) -> None:
    image = _hd_image(tmp_path)
    assert media.validate_hd_image(image) == image

    empty = _write(tmp_path / "empty.img", b"")
    with pytest.raises(HatariError) as excinfo:
        media.validate_hd_image(empty)
    assert excinfo.value.code == "invalid_image"

    unaligned = _write(tmp_path / "odd.img", b"\0" * 513)
    with pytest.raises(HatariError) as excinfo:
        media.validate_hd_image(unaligned)
    assert excinfo.value.code == "invalid_image"

    with pytest.raises(HatariError) as excinfo:
        media.validate_hd_image(tmp_path / "missing.img")
    assert excinfo.value.code == "invalid_path"


def test_cartridge_validation_and_option(tmp_path: Path) -> None:
    image = _cart_image(tmp_path)
    assert media.validate_cartridge_image(image) == image
    assert media.build_cartridge_option(str(image)) == f"--cartridge {image}"

    empty = _write(tmp_path / "empty.rom", b"")
    with pytest.raises(HatariError) as excinfo:
        media.validate_cartridge_image(empty)
    assert excinfo.value.code == "invalid_image"


def test_device_command_builders() -> None:
    assert devices.build_device_command("printer", "toggle") == "hatari-toggle printer"
    assert devices.build_device_command("RS232", "enable") == "hatari-enable rs232"
    assert devices.build_device_command("sccalan", "disable") == "hatari-disable sccalan"

    with pytest.raises(HatariError) as excinfo:
        devices.build_device_command("floppy", "toggle")
    assert excinfo.value.code == "bad_option"

    with pytest.raises(HatariError) as excinfo:
        devices.build_device_command("printer", "start")
    assert excinfo.value.code == "bad_option"


def test_path_command_builders(tmp_path: Path) -> None:
    assert devices.build_path_command("printout", "/tmp/print.out") == "hatari-path printout /tmp/print.out"
    assert devices.build_path_command("sccain", "/tmp/in file") == "hatari-path sccain /tmp/in file"
    assert devices.build_path_command("midi-out", "/tmp/midi.out") == "hatari-path midiout /tmp/midi.out"
    assert devices.build_audio_capture_command("/tmp/sound.wav") == "hatari-path soundout /tmp/sound.wav"

    with pytest.raises(HatariError) as excinfo:
        devices.build_path_command("unknown", "/tmp/x")
    assert excinfo.value.code == "bad_option"

    with pytest.raises(HatariError) as excinfo:
        devices.build_path_command("printout", "/tmp/a\nb")
    assert excinfo.value.code == "bad_option"


def test_audio_capture_validation(tmp_path: Path) -> None:
    assert devices.validate_audio_capture(tmp_path / "sound.wav") == tmp_path / "sound.wav"
    assert devices.validate_audio_capture(tmp_path / "sound.YM") == tmp_path / "sound.YM"

    with pytest.raises(HatariError) as excinfo:
        devices.validate_audio_capture(tmp_path / "sound.mp3")
    assert excinfo.value.code == "unsupported_format"


def test_shortcut_builder() -> None:
    assert devices.build_shortcut_command("recsound") == "hatari-shortcut recsound"
    assert devices.build_shortcut_command("recanim") == "hatari-shortcut recanim"
    with pytest.raises(HatariError) as excinfo:
        devices.build_shortcut_command("reset")
    assert excinfo.value.code == "bad_option"


def test_avi_option_builder(tmp_path: Path) -> None:
    target = tmp_path / "capture.avi"
    assert devices.build_avi_options(target, "png", 25) == [
        f"--avi-file {target}",
        "--avi-vcodec png",
        "--avi-fps 25",
    ]
    assert devices.build_avi_options(target, "BMP", 50) == [
        f"--avi-file {target}",
        "--avi-vcodec bmp",
        "--avi-fps 50",
    ]

    with pytest.raises(HatariError) as excinfo:
        devices.build_avi_options(target, "h264", 25)
    assert excinfo.value.code == "bad_option"

    for fps in (0, 101):
        with pytest.raises(HatariError) as excinfo:
            devices.build_avi_options(target, "png", fps)
        assert excinfo.value.code == "bad_option"
