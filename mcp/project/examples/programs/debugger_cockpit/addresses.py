"""Address, number and hex-dump helpers for the debugger cockpit.

Bare address tokens are hexadecimal by convention (the Atari debugger's native
notation).  Values entered in register fields use decimal unless prefixed.
"""

from __future__ import annotations

import re
from typing import Iterable

_HEX_DIGITS = re.compile(r"^[0-9a-fA-F]+$")
_DECIMAL = re.compile(r"^[0-9]+$")
_MAX_32 = 0xFFFFFFFF


class AddressError(ValueError):
    """Raised when a user-entered address or number cannot be parsed."""


def _strip_number(text: str) -> str:
    return "".join(text.split())


def parse_address(text: str | int, *, default: int | None = None) -> int:
    """Parse a 32-bit address.

    Accepted forms: ``$1234``, ``0x1234``, ``1234h``, ``%1010``, or a bare
    hexadecimal token such as ``1234``.  A bare decimal-looking token is still
    treated as hexadecimal, matching Atari debugger usage.  Use ``1234d`` for
    an explicit decimal address.
    """

    if isinstance(text, bool):
        raise AddressError("booleans are not addresses")
    if isinstance(text, int):
        value = text
    else:
        raw = _strip_number(str(text))
        if not raw:
            if default is not None:
                return default
            raise AddressError("address is empty")
        sign = 1
        if raw[0] in "+-":
            sign = -1 if raw[0] == "-" else 1
            raw = raw[1:]
            if not raw:
                raise AddressError("address has a sign but no digits")
        lower = raw.lower()
        try:
            if lower.startswith("$"):
                value = int(lower[1:], 16)
            elif lower.startswith("0x"):
                value = int(lower[2:], 16)
            elif lower.startswith("0b"):
                value = int(lower[2:], 2)
            elif lower.startswith("%"):
                value = int(lower[1:], 2)
            elif lower.endswith("h"):
                value = int(lower[:-1], 16)
            elif lower.endswith("d"):
                value = int(lower[:-1], 10)
            elif _HEX_DIGITS.fullmatch(lower):
                value = int(lower, 16)
            else:
                raise AddressError(f"not a hexadecimal address: {text!r}")
        except ValueError as exc:
            raise AddressError(f"invalid address: {text!r}") from exc
        value *= sign
    if value < 0 or value > _MAX_32:
        raise AddressError(f"address out of 32-bit range: {value}")
    return value


def parse_value(text: str | int, *, default: int | None = None) -> int:
    """Parse a register/memory value.

    Prefixes ``$``/``0x`` mean hexadecimal, ``%``/``0b`` binary; a bare token is
    decimal.  The ``h`` and ``d`` suffixes force hexadecimal or decimal.
    """

    if isinstance(text, bool):
        raise AddressError("booleans are not values")
    if isinstance(text, int):
        value = text
    else:
        raw = _strip_number(str(text))
        if not raw:
            if default is not None:
                return default
            raise AddressError("value is empty")
        sign = 1
        if raw[0] in "+-":
            sign = -1 if raw[0] == "-" else 1
            raw = raw[1:]
            if not raw:
                raise AddressError("value has a sign but no digits")
        lower = raw.lower()
        try:
            if lower.startswith("$"):
                value = int(lower[1:], 16)
            elif lower.startswith("0x"):
                value = int(lower[2:], 16)
            elif lower.startswith("0b"):
                value = int(lower[2:], 2)
            elif lower.startswith("%"):
                value = int(lower[1:], 2)
            elif lower.endswith("h"):
                value = int(lower[:-1], 16)
            elif lower.endswith("d"):
                value = int(lower[:-1], 10)
            elif _DECIMAL.fullmatch(lower):
                value = int(lower, 10)
            elif _HEX_DIGITS.fullmatch(lower):
                value = int(lower, 16)
            else:
                raise AddressError(f"not a number: {text!r}")
        except ValueError as exc:
            raise AddressError(f"invalid value: {text!r}") from exc
        value *= sign
    if value < 0 or value > _MAX_32:
        raise AddressError(f"value out of 32-bit range: {value}")
    return value


def parse_count(text: str | int, *, minimum: int = 1, maximum: int = 65536) -> int:
    if isinstance(text, bool):
        raise AddressError("booleans are not counts")
    if isinstance(text, int):
        value = text
    else:
        raw = _strip_number(str(text))
        if not raw:
            raise AddressError("count is empty")
        try:
            if raw.lower().startswith("$"):
                value = int(raw[1:], 16)
            elif raw.lower().startswith("0x"):
                value = int(raw[2:], 16)
            else:
                value = int(raw, 10)
        except ValueError as exc:
            raise AddressError(f"invalid count: {text!r}") from exc
    if value < minimum or value > maximum:
        raise AddressError(f"count must be between {minimum} and {maximum}")
    return value


def format_address(value: int, *, width: int = 8) -> str:
    return f"${int(value) & _MAX_32:0{max(1, width)}x}"


def format_value(value: int, *, width: int = 8) -> str:
    return f"${int(value) & _MAX_32:0{max(1, width)}x}"


def normalize_hex_data(text: str) -> str:
    """Return compact lowercase hex from ``de ad be ef`` or ``0xdeadbeef``."""

    raw = str(text).strip()
    if not raw:
        raise AddressError("hex data is empty")
    cleaned = raw.replace("0x", "").replace("0X", "")
    for separator in (" ", "\t", "\r", "\n", ",", ":", "-", "_"):
        cleaned = cleaned.replace(separator, "")
    if not cleaned:
        raise AddressError("hex data is empty")
    if not _HEX_DIGITS.fullmatch(cleaned):
        raise AddressError(f"hex data contains non-hex characters: {text!r}")
    if len(cleaned) % 2:
        raise AddressError("hex data must contain an even number of digits")
    return cleaned.lower()


def bytes_from_hex(text: str) -> bytes:
    return bytes.fromhex(normalize_hex_data(text))


def _printable(byte: int) -> str:
    return chr(byte) if 32 <= byte < 127 else "."


def format_hex_dump(data: bytes | bytearray | Iterable[int], base_address: int) -> str:
    """Format bytes as a classic offset + hex + ASCII dump."""

    payload = bytes(data)
    if not payload:
        return "(no data)"
    lines: list[str] = []
    for offset in range(0, len(payload), 16):
        chunk = payload[offset : offset + 16]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        hex_part = f"{hex_part:<47}"
        ascii_part = "".join(_printable(byte) for byte in chunk)
        lines.append(f"{format_address(base_address + offset)}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def format_word_dump(words: Iterable[int], *, base_address: int = 0) -> str:
    values = list(words)
    if not values:
        return "(no stack words)"
    lines = []
    for index, value in enumerate(values):
        lines.append(f"{format_address(base_address + index * 4)}  {format_value(int(value))}")
    return "\n".join(lines)
