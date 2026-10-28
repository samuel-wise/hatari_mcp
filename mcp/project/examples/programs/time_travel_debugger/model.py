"""Pure timeline, checkpoint and diff logic for the time-travel debugger.

Nothing in this module talks to Tk, the MCP server or Hatari.  That keeps the
checkpoint format, retention and first-divergence rules unit-testable without
an emulator.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
CHECKPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HEX_BODY_RE = re.compile(r"^[0-9a-fA-F]+$")
_DECIMAL_RE = re.compile(r"^-?\d+$")
MAX_MEMORY_TOOL_BYTES = 65536

CATEGORY_ORDER = ("pc", "register", "memory", "framebuffer")


class ModelError(ValueError):
    pass


# -- numbers and registers ------------------------------------------------


def parse_int(value: Any, *, default: int | None = None) -> int | None:
    """Parse an integer from ``int``, ``$hex``, ``0xhex``, decimal text."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        if text.startswith("$"):
            return int(text[1:], 16)
        if text.lower().startswith("0x"):
            return int(text, 16)
        if _DECIMAL_RE.match(text):
            return int(text, 10)
        if _HEX_BODY_RE.match(text) and any(char in "abcdefABCDEF" for char in text):
            return int(text, 16)
    except (TypeError, ValueError):
        return default
    return default


def canonical_register(value: Any) -> str:
    """Return a stable textual register value for comparison and display."""
    if isinstance(value, str):
        text = value.strip()
        parsed = parse_int(text)
        if parsed is not None and (text.startswith("$") or text.lower().startswith("0x")):
            return f"0x{parsed:x}"
        if parsed is not None and _DECIMAL_RE.match(text):
            return f"0x{parsed:x}"
        if parsed is not None and _HEX_BODY_RE.match(text):
            return f"0x{parsed:x}"
        return text.lower()
    parsed = parse_int(value)
    if parsed is not None:
        return f"0x{parsed:x}"
    return str(value)


def normalize_registers(payload: Any) -> dict[str, str]:
    """Normalize an MCP register result (or a plain mapping) to hex strings."""
    if isinstance(payload, dict) and isinstance(payload.get("registers"), dict):
        payload = payload["registers"]
    if not isinstance(payload, dict):
        return {}
    return {
        str(name).lower(): canonical_register(value)
        for name, value in payload.items()
        if value is not None
    }


def register_int(registers: dict[str, str], name: str = "pc") -> int | None:
    value = registers.get(name.lower())
    return parse_int(value)


# -- memory regions -------------------------------------------------------


@dataclass(frozen=True)
class MemoryRegion:
    address: int
    length: int

    def __post_init__(self) -> None:
        if self.address < 0:
            raise ModelError("memory address must be non-negative")
        if not (1 <= self.length <= MAX_MEMORY_TOOL_BYTES):
            raise ModelError(
                f"memory length must be 1..{MAX_MEMORY_TOOL_BYTES}, got {self.length}"
            )

    @property
    def end(self) -> int:
        return self.address + self.length

    @property
    def label(self) -> str:
        return f"0x{self.address:x}:{self.length}"

    @classmethod
    def parse(cls, text: str) -> "MemoryRegion":
        match = re.match(
            r"^\s*(?P<addr>\$?0x[0-9a-fA-F]+|\$[0-9a-fA-F]+|\d+)\s*[:;,xX]\s*"
            r"(?P<len>\$?0x[0-9a-fA-F]+|\$[0-9a-fA-F]+|\d+)\s*$",
            str(text),
        )
        if not match:
            raise ModelError(f"invalid memory region (use ADDRESS:LENGTH): {text!r}")
        address = parse_int(match.group("addr"))
        length = parse_int(match.group("len"))
        if address is None or length is None:
            raise ModelError(f"invalid memory region: {text!r}")
        return cls(address, length)

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "length": self.length}


def parse_memory_regions(spec: str | Iterable[str] | Iterable[MemoryRegion]) -> list[MemoryRegion]:
    if isinstance(spec, MemoryRegion):
        return [spec]
    if isinstance(spec, str):
        parts = [part for part in re.split(r"[,\s]+", spec.strip()) if part]
        return [MemoryRegion.parse(part) for part in parts]
    regions: list[MemoryRegion] = []
    for item in spec:
        regions.append(item if isinstance(item, MemoryRegion) else MemoryRegion.parse(str(item)))
    return regions


def format_memory_regions(regions: Iterable[MemoryRegion]) -> str:
    return ",".join(region.label for region in regions)


# -- checkpoint components ------------------------------------------------


@dataclass
class MemorySnapshot:
    address: int
    length: int
    sha256: str
    path: str | None = None
    data: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.address < 0 or self.length < 0:
            raise ModelError("memory snapshot address/length must be non-negative")
        if self.data is not None and len(self.data) != self.length:
            raise ModelError(
                f"memory snapshot data is {len(self.data)} bytes, expected {self.length}"
            )

    def bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is None:
            return b""
        try:
            return Path(self.path).read_bytes()
        except OSError:
            return b""

    def to_dict(self, *, base_dir: str | Path | None = None) -> dict[str, Any]:
        return {
            "address": self.address,
            "length": self.length,
            "sha256": self.sha256,
            "path": _relative_path(self.path, base_dir),
            # Embedded bytes are only needed when there is no sidecar file
            # (mostly for hermetic tests); captured snapshots keep a .bin file.
            "base64": (
                base64.b64encode(self.data).decode("ascii")
                if self.data is not None and self.path is None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: str | Path | None = None) -> "MemorySnapshot":
        raw = data.get("base64")
        payload: bytes | None = None
        if isinstance(raw, str) and raw:
            try:
                payload = base64.b64decode(raw.encode("ascii"))
            except (ValueError, TypeError):
                payload = None
        return cls(
            address=int(data.get("address", 0)),
            length=int(data.get("length", 0)),
            sha256=str(data.get("sha256", "")),
            path=_absolute_path(data.get("path"), base_dir),
            data=payload,
        )


@dataclass
class Checkpoint:
    id: str
    timestamp: float
    index: int
    state_path: str
    screenshot_path: str | None
    frame_sha256: str
    registers: dict[str, str] = field(default_factory=dict)
    memory: list[MemorySnapshot] = field(default_factory=list)
    vbl: int | None = None
    label: str = ""
    source: str = "capture"
    reference_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not CHECKPOINT_ID_RE.match(self.id):
            raise ModelError(f"invalid checkpoint id: {self.id!r}")

    @property
    def pc(self) -> int | None:
        return register_int(self.registers, "pc")

    def memory_by_address(self) -> dict[int, MemorySnapshot]:
        return {item.address: item for item in self.memory}

    def to_dict(self, *, base_dir: str | Path | None = None) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "id": self.id,
            "timestamp": self.timestamp,
            "index": self.index,
            "vbl": self.vbl,
            "label": self.label,
            "source": self.source,
            "reference_id": self.reference_id,
            "state_path": _relative_path(self.state_path, base_dir),
            "screenshot_path": _relative_path(self.screenshot_path, base_dir),
            "frame_sha256": self.frame_sha256,
            "registers": dict(self.registers),
            "memory": [item.to_dict(base_dir=base_dir) for item in self.memory],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: str | Path | None = None) -> "Checkpoint":
        return cls(
            id=str(data["id"]),
            timestamp=float(data.get("timestamp", 0.0)),
            index=int(data.get("index", 0)),
            vbl=parse_int(data.get("vbl")),
            label=str(data.get("label", "")),
            source=str(data.get("source", "capture")),
            reference_id=data.get("reference_id"),
            state_path=str(_absolute_path(data.get("state_path"), base_dir)),
            screenshot_path=_absolute_path(data.get("screenshot_path"), base_dir),
            frame_sha256=str(data.get("frame_sha256", "")),
            registers={str(k): str(v) for k, v in dict(data.get("registers") or {}).items()},
            memory=[
                MemorySnapshot.from_dict(item, base_dir=base_dir)
                for item in data.get("memory") or []
            ],
            metadata=dict(data.get("metadata") or {}),
        )


# -- timeline -------------------------------------------------------------


class Timeline:
    """Ordered checkpoint collection with retention-pruning rules."""

    def __init__(self, checkpoints: Iterable[Checkpoint] | None = None) -> None:
        self._items: dict[str, Checkpoint] = {}
        for checkpoint in checkpoints or []:
            self.add(checkpoint)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self.ordered())

    def __contains__(self, checkpoint_id: object) -> bool:
        return str(checkpoint_id) in self._items

    def add(self, checkpoint: Checkpoint) -> Checkpoint:
        self._items[checkpoint.id] = checkpoint
        return checkpoint

    def remove(self, checkpoint_id: str) -> Checkpoint | None:
        return self._items.pop(checkpoint_id, None)

    def get(self, checkpoint_id: str | None) -> Checkpoint | None:
        if checkpoint_id is None:
            return None
        return self._items.get(str(checkpoint_id))

    def ordered(self, *, sources: Iterable[str] | None = None) -> list[Checkpoint]:
        items = list(self._items.values())
        if sources is not None:
            allowed = set(sources)
            items = [item for item in items if item.source in allowed]
        return sorted(items, key=lambda item: (item.timestamp, item.index, item.id))

    def recorded(self) -> list[Checkpoint]:
        return self.ordered(sources={"capture"})

    def replays(self) -> list[Checkpoint]:
        return self.ordered(sources={"replay"})

    @property
    def latest(self) -> Checkpoint | None:
        ordered = self.ordered()
        return ordered[-1] if ordered else None

    def next_index(self) -> int:
        if not self._items:
            return 1
        return max(item.index for item in self._items.values()) + 1

    def nearest_before_vbl(self, vbl: int) -> Checkpoint | None:
        candidates = [
            item for item in self.ordered() if item.vbl is not None and item.vbl <= vbl
        ]
        return candidates[-1] if candidates else None

    def between(
        self,
        start_id: str,
        end_id: str,
        *,
        include_start: bool = False,
        include_end: bool = True,
        recorded_only: bool = True,
    ) -> list[Checkpoint]:
        ordered = self.recorded() if recorded_only else self.ordered()
        ids = [item.id for item in ordered]
        if start_id not in ids or end_id not in ids:
            raise ModelError("start or end checkpoint is not in the timeline")
        start = ids.index(start_id)
        end = ids.index(end_id)
        if start > end:
            raise ModelError("start checkpoint is after the end checkpoint")
        lower = start if include_start else start + 1
        upper = end + 1 if include_end else end
        return ordered[lower:upper]

    def prune(
        self,
        max_checkpoints: int,
        *,
        protected: Iterable[str] = (),
    ) -> list[Checkpoint]:
        """Drop oldest checkpoints until at most *max_checkpoints* remain.

        Protected ids are never dropped.  If protection prevents reaching the
        bound, the timeline may remain above the limit; the removed list always
        describes what was actually dropped.
        """
        if max_checkpoints < 1:
            raise ModelError("max_checkpoints must be at least 1")
        ordered = self.ordered()
        excess = len(ordered) - max_checkpoints
        if excess <= 0:
            return []
        protected_ids = {str(item) for item in protected}
        removed: list[Checkpoint] = []
        for checkpoint in ordered:
            if len(removed) >= excess:
                break
            if checkpoint.id in protected_ids:
                continue
            removed.append(checkpoint)
        for checkpoint in removed:
            self._items.pop(checkpoint.id, None)
        return removed

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.ordered()]

    @classmethod
    def from_list(cls, data: Any, *, base_dir: str | Path | None = None) -> "Timeline":
        if not isinstance(data, list):
            return cls()
        return cls(
            Checkpoint.from_dict(item, base_dir=base_dir)
            for item in data
            if isinstance(item, dict) and item.get("id")
        )


# -- diff and first divergence -------------------------------------------


@dataclass(frozen=True)
class Divergence:
    category: str
    key: str
    before: Any
    after: Any
    detail: str = ""
    address: int | None = None
    offset: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
            "address": self.address,
            "offset": self.offset,
        }

    def describe(self) -> str:
        text = f"{self.category} {self.key}: {self.before!r} -> {self.after!r}"
        if self.detail:
            text += f" ({self.detail})"
        return text


@dataclass
class DiffReport:
    before_id: str
    after_id: str
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def match(self) -> bool:
        return not self.divergences

    @property
    def first(self) -> Divergence | None:
        return self.divergences[0] if self.divergences else None

    def summary(self) -> str:
        if self.match:
            return "checkpoints match in PC, registers, memory and framebuffer"
        first = self.first
        assert first is not None
        return f"first divergence: {first.describe()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "before_id": self.before_id,
            "after_id": self.after_id,
            "match": self.match,
            "first": self.first.as_dict() if self.first else None,
            "divergences": [item.as_dict() for item in self.divergences],
        }


def _first_byte_difference(left: bytes, right: bytes, limit: int = 1 << 30) -> tuple[int, int]:
    length = min(len(left), len(right), limit)
    differing = 0
    first = -1
    for index in range(length):
        if left[index] != right[index]:
            differing += 1
            if first < 0:
                first = index
    if len(left) != len(right):
        differing += abs(len(left) - len(right))
        if first < 0:
            first = length
    return first, differing


def diff_checkpoints(
    before: Checkpoint,
    after: Checkpoint,
    *,
    max_register_diffs: int | None = None,
) -> DiffReport:
    """Compare two checkpoints, ordered by PC, registers, memory, framebuffer."""
    divergences: list[Divergence] = []

    before_pc = before.registers.get("pc")
    after_pc = after.registers.get("pc")
    if before_pc != after_pc:
        divergences.append(
            Divergence("pc", "pc", before_pc, after_pc, "program counter differs")
        )

    names = sorted(
        (set(before.registers) | set(after.registers)) - {"pc"}
    )
    for name in names:
        left = before.registers.get(name)
        right = after.registers.get(name)
        if left != right:
            divergences.append(Divergence("register", name, left, right))
            if max_register_diffs is not None and len(
                [item for item in divergences if item.category == "register"]
            ) >= max_register_diffs:
                break

    before_memory = before.memory_by_address()
    after_memory = after.memory_by_address()
    for address in sorted(set(before_memory) | set(after_memory)):
        left_snapshot = before_memory.get(address)
        right_snapshot = after_memory.get(address)
        if left_snapshot is None or right_snapshot is None:
            divergences.append(
                Divergence(
                    "memory",
                    f"0x{address:x}",
                    "present" if left_snapshot else "missing",
                    "present" if right_snapshot else "missing",
                    "memory region differs",
                    address=address,
                )
            )
            continue
        if left_snapshot.sha256 and right_snapshot.sha256:
            if left_snapshot.sha256 == right_snapshot.sha256:
                continue
        left_bytes = left_snapshot.bytes()
        right_bytes = right_snapshot.bytes()
        if left_bytes == right_bytes and left_snapshot.sha256 == right_snapshot.sha256:
            continue
        offset, changed = _first_byte_difference(left_bytes, right_bytes)
        address_text = f"0x{address + max(offset, 0):x}" if offset >= 0 else "unknown"
        divergences.append(
            Divergence(
                "memory",
                f"0x{address:x}",
                left_snapshot.sha256,
                right_snapshot.sha256,
                f"{changed} byte(s) differ; first at {address_text}",
                address=address,
                offset=offset if offset >= 0 else None,
            )
        )

    if before.frame_sha256 or after.frame_sha256:
        if before.frame_sha256 != after.frame_sha256:
            divergences.append(
                Divergence(
                    "framebuffer",
                    "frame_sha256",
                    before.frame_sha256,
                    after.frame_sha256,
                    "screenshot hash differs",
                )
            )
    return DiffReport(before.id, after.id, divergences)


@dataclass(frozen=True)
class MemoryRangeDiff:
    address: int
    length: int
    before_hex: str
    after_hex: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "length": self.length,
            "before_hex": self.before_hex,
            "after_hex": self.after_hex,
        }


def memory_diff_ranges(
    before: Checkpoint,
    after: Checkpoint,
    *,
    address: int | None = None,
    max_ranges: int = 64,
    max_bytes: int = 32,
) -> list[MemoryRangeDiff]:
    """Return contiguous byte ranges that changed between two checkpoints."""
    before_memory = before.memory_by_address()
    after_memory = after.memory_by_address()
    addresses = [address] if address is not None else sorted(set(before_memory) & set(after_memory))
    ranges: list[MemoryRangeDiff] = []
    for region_address in addresses:
        left = before_memory.get(region_address)
        right = after_memory.get(region_address)
        if left is None or right is None:
            continue
        left_bytes = left.bytes()
        right_bytes = right.bytes()
        length = min(len(left_bytes), len(right_bytes))
        start: int | None = None
        for index in range(length + 1):
            changed = index < length and left_bytes[index] != right_bytes[index]
            if changed and start is None:
                start = index
            elif not changed and start is not None:
                end = index
                if len(ranges) >= max_ranges:
                    return ranges
                ranges.append(
                    MemoryRangeDiff(
                        address=region_address + start,
                        length=end - start,
                        before_hex=left_bytes[start:end][:max_bytes].hex(),
                        after_hex=right_bytes[start:end][:max_bytes].hex(),
                    )
                )
                start = None
        if len(left_bytes) != len(right_bytes):
            if len(ranges) >= max_ranges:
                return ranges
            extra_start = length
            ranges.append(
                MemoryRangeDiff(
                    address=region_address + extra_start,
                    length=abs(len(left_bytes) - len(right_bytes)),
                    before_hex=left_bytes[extra_start:][:max_bytes].hex(),
                    after_hex=right_bytes[extra_start:][:max_bytes].hex(),
                )
            )
    return ranges


# -- path helpers ---------------------------------------------------------


def _relative_path(path: str | Path | None, base_dir: str | Path | None) -> str | None:
    if path is None:
        return None
    target = Path(path)
    if base_dir is None:
        return str(target)
    base = Path(base_dir)
    try:
        return str(target.resolve().relative_to(base.resolve()))
    except (ValueError, OSError):
        return str(target)


def _absolute_path(path: Any, base_dir: str | Path | None) -> str | None:
    if path is None:
        return None
    target = Path(str(path))
    if not target.is_absolute() and base_dir is not None:
        target = Path(base_dir) / target
    return str(target)
