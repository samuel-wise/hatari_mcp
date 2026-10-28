"""Machine/TOS/resolution selection for a game launch.

The chooser prefers the last known-good configuration recorded by ``Config``,
then explicit sidecar metadata, then TOSEC tags/filename hints, and finally a
safe ST + TOS 1.04 default, with EmuTOS as a last resort.  TT and Falcon are
only selected when a matching TOS
image is actually available; otherwise the profile falls back to a compatible
machine and says why.  Nothing here starts an emulator.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import paths  # noqa: E402
from common.game_runner import profile_args  # noqa: E402

MACHINES = ("st", "ste", "tt", "falcon")
MACHINE_LABELS = {
    "st": "Atari ST",
    "ste": "Atari STE",
    "tt": "Atari TT",
    "falcon": "Atari Falcon",
}
TOS_PREFERENCES: dict[str, tuple[str, ...]] = {
    "st": ("tos104", "tos102", "tos100", "tos106", "emutos"),
    "ste": ("tos106", "tos162", "tos206", "tos104", "emutos"),
    "tt": ("tos306", "tos304", "tos404", "emutos"),
    # Stock TOS 3.x is not a Falcon TOS.  Only use a real 4.x image.
    "falcon": ("tos404",),
}
FALLBACK_CHAIN: dict[str, tuple[str, ...]] = {
    "st": ("st",),
    "ste": ("ste", "st"),
    "tt": ("tt", "ste", "st"),
    "falcon": ("falcon", "tt", "ste", "st"),
}
MACHINE_RESOLUTION: dict[str, tuple[int, int]] = {
    "st": (320, 200),
    "ste": (320, 200),
    "tt": (640, 480),
    "falcon": (640, 480),
}
_FALLBACK_TOS_FILES = {
    "tos100": ("tos1x/roms/tos100us.img",),
    "tos102": ("tos1x/roms/tos102us.img",),
    "tos104": ("tos1x/roms/tos104us.img",),
    "tos106": ("tos1x/roms/tos106us.img",),
    "tos162": ("tos1x/roms/tos162us.img",),
    "tos206": ("tos3x/roms/tos206us.img",),
    "tos306": ("tos3x/roms/tos306us.img",),
    "tos404": ("tos3x/roms/tos404.img", "tos3x/roms/tos404us.img"),
}
_MACHINE_ALIASES = {
    "falcon": "falcon",
    "falcon030": "falcon",
    "ct2": "falcon",
    "videl": "falcon",
    "aga": "falcon",
    "tt": "tt",
    "tt030": "tt",
    "ste": "ste",
    "megaste": "ste",
}


_REQUIREMENT_TOKENS = {"need", "needs", "require", "requires", "required"}


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _machine_from_marker(text: Any) -> str | None:
    tokens = re.findall(r"[a-z0-9]+", str(text).casefold())
    if any(token in _REQUIREMENT_TOKENS for token in tokens):
        return None
    for token in tokens:
        machine = _MACHINE_ALIASES.get(token)
        if machine:
            return machine
    return None


def _explicit_machine(entry: Any) -> str | None:
    for tag in _entry_value(entry, "tags", ()) or ():
        machine = _machine_from_marker(tag)
        if machine:
            return machine
    filename = str(_entry_value(entry, "filename", "") or "")
    for group in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", filename):
        machine = _machine_from_marker(group)
        if machine:
            return machine
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def tos_roots(roots: list[str | Path] | None = None) -> list[Path]:
    if roots is not None:
        return [Path(item).expanduser() for item in roots]
    default = Path(
        os.environ.get("HATARI_TOS_DIR", "/home/ubuntu/Projects/Hatari/tos")
    ).expanduser()
    found = [default]
    extra = os.environ.get("HATARI_TOS")
    if extra:
        found.append(Path(extra).expanduser().parent)
    return found


def discover_tos_candidates(
    roots: list[str | Path] | None = None,
) -> dict[str, Path]:
    """Discovered TOS images plus a small local fallback for common layouts.

    ``roots`` comes from the launcher settings.  ``common.paths`` does not find
    ``tos3x/roms/tos404.img`` on this machine (it looks for ``tos404us.img``),
    so the launcher adds the known fallback names itself.
    """
    found = dict(paths.tos_candidates(roots))
    for key, names in _FALLBACK_TOS_FILES.items():
        if key in found:
            continue
        for root in tos_roots(roots):
            for name in names:
                candidate = root / name
                if paths.is_valid_tos(candidate):
                    found[key] = candidate.resolve()
                    break
            if key in found:
                break
    return found


def _env_tos() -> Path | None:
    value = os.environ.get("HATARI_TOS")
    if not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if paths.is_valid_tos(path) else None


def detect_machine(entry: Any, machine: str | None = None) -> tuple[str, str]:
    """Return ``(machine, source)`` using sidecar metadata or explicit markers.

    Only TOSEC tags and bracketed/parenthesized filename groups are inspected,
    so a title such as ``Falcon`` or ``Falcon Patrol`` stays a plain ST game.
    """
    from .metadata import _normalise_machine  # local import avoids a cycle

    sidecar_machine = _normalise_machine(machine)
    if sidecar_machine:
        return sidecar_machine, "sidecar"
    candidate = _explicit_machine(entry)
    if candidate:
        return candidate, "filename"
    return "st", "default"


def _first_tos(
    machine: str,
    tos_map: Mapping[str, Path],
    *,
    env_tos: Path | None,
) -> tuple[Path | None, str | None]:
    if env_tos is not None:
        return env_tos, "env:HATARI_TOS"
    for kind in TOS_PREFERENCES.get(machine, ()):
        candidate = tos_map.get(kind)
        if candidate is not None and paths.is_valid_tos(Path(candidate)):
            return Path(candidate), f"auto:{kind}"
    return None, None


def _learned_tos(learned: Mapping[str, Any]) -> Path | None:
    value = learned.get("tos")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if paths.is_valid_tos(path) else None


def _normalise_args(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    skip_next = False
    for item in value:
        text = str(item)
        if skip_next:
            skip_next = False
            continue
        if text == "--disk-a":
            skip_next = True
            continue
        if text.startswith("--disk-a="):
            continue
        result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class LaunchProfile:
    machine: str
    profile: str
    tos: Path | None
    tos_kind: str | None
    tos_source: str
    expected_resolution: tuple[int, int] | None
    extra_args: tuple[str, ...] = ()
    source: str = "default"
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    requested_machine: str | None = None

    @property
    def machine_label(self) -> str:
        return MACHINE_LABELS.get(self.machine, self.machine)

    @property
    def machine_args(self) -> list[str]:
        return profile_args(self.machine)

    @property
    def tos_name(self) -> str:
        return self.tos.name if self.tos else "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "profile": self.profile,
            "tos": str(self.tos) if self.tos else None,
            "tos_kind": self.tos_kind,
            "tos_source": self.tos_source,
            "expected_resolution": (
                list(self.expected_resolution) if self.expected_resolution else None
            ),
            "extra_args": list(self.extra_args),
            "source": self.source,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "requested_machine": self.requested_machine,
        }


def choose_profile(
    entry: Any,
    *,
    config: Any = None,
    metadata: Any = None,
    requested_machine: str | None = None,
    requested_tos: str | Path | None = None,
    tos_candidates: Mapping[str, Path] | None = None,
) -> LaunchProfile:
    """Choose a safe machine, TOS, expected resolution and extra arguments."""
    game_id = str(_entry_value(entry, "id", "") or "")
    learned: Mapping[str, Any] = {}
    if config is not None and game_id:
        try:
            learned = config.learned_launch(game_id) or {}
        except Exception:
            learned = {}

    metadata_machine = _text(getattr(metadata, "machine", None))
    if requested_machine and requested_machine.casefold() in MACHINES:
        machine = requested_machine.casefold()
        source = "requested"
    elif isinstance(learned.get("machine"), str) and learned["machine"].casefold() in MACHINES:
        machine = learned["machine"].casefold()
        source = "learned"
    else:
        machine, source = detect_machine(entry, metadata_machine)

    tos_map = dict(tos_candidates) if tos_candidates is not None else discover_tos_candidates()
    env_tos = _env_tos()
    reasons: list[str] = [f"machine {machine} from {source}"]
    warnings: list[str] = []
    tos_kind: str | None = None

    chosen_tos: Path | None = None
    tos_source = "none"
    if requested_tos is not None:
        candidate = Path(requested_tos).expanduser()
        if paths.is_valid_tos(candidate):
            chosen_tos = candidate.resolve()
            tos_source = "requested"
    if chosen_tos is None:
        learned_tos = _learned_tos(learned)
        learned_machine = (
            learned.get("machine").casefold()
            if isinstance(learned.get("machine"), str)
            else None
        )
        if learned_tos is not None and learned_machine in (None, machine):
            chosen_tos = learned_tos
            tos_source = "learned"
    if chosen_tos is None:
        chosen_tos, tos_source = _first_tos(machine, tos_map, env_tos=env_tos)

    if chosen_tos is None:
        for fallback in FALLBACK_CHAIN.get(machine, ("st",)):
            if fallback == machine:
                continue
            fallback_tos, fallback_source = _first_tos(
                fallback, tos_map, env_tos=env_tos
            )
            if fallback_tos is not None:
                warnings.append(
                    f"no usable {MACHINE_LABELS.get(machine, machine)} TOS; "
                    f"using {fallback} instead"
                )
                machine = fallback
                chosen_tos = fallback_tos
                tos_source = fallback_source or "fallback"
                source = f"{source}+fallback"
                break

    if chosen_tos is None:
        warnings.append("no TOS image was found; launch may fail")
    if tos_source and tos_source.startswith("auto:"):
        tos_kind = tos_source.split(":", 1)[1]
    elif tos_source.startswith("env:"):
        tos_kind = "env"

    expected = MACHINE_RESOLUTION.get(machine)
    if metadata is not None:
        metadata_resolution = getattr(metadata, "resolution", None)
        if metadata_resolution:
            expected = tuple(metadata_resolution)
        metadata_args = getattr(metadata, "args", ()) or ()
        extra_args = _normalise_args(metadata_args)
    else:
        extra_args = ()
    learned_args = _normalise_args(learned.get("args") or ())
    if source.startswith("learned") and learned_args:
        extra_args = learned_args

    reasons.append(
        f"TOS {chosen_tos.name if chosen_tos else 'none'} ({tos_source})"
    )
    if expected:
        reasons.append(f"expected {expected[0]}x{expected[1]}")
    if any(warning for warning in warnings):
        reasons.extend(warnings)

    return LaunchProfile(
        machine=machine,
        profile=machine,
        tos=chosen_tos,
        tos_kind=tos_kind,
        tos_source=tos_source,
        expected_resolution=expected,
        extra_args=extra_args,
        source=source,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        requested_machine=requested_machine,
    )


def _same_tos(current: str | Path | None, target: str | Path | None) -> bool:
    if current is None and target is None:
        return True
    if current is None or target is None:
        return False
    try:
        return Path(current).resolve() == Path(target).resolve()
    except OSError:
        return str(current) == str(target)


def incompatibility_reason(
    current_machine: str | None,
    current_tos: str | Path | None,
    profile: LaunchProfile,
    *,
    current_game_id: str | None = None,
) -> str | None:
    """Return a human-readable reason a hot disk swap is not possible."""
    if not current_game_id:
        return "no tracked game is running"
    if (current_machine or "st").casefold() != profile.machine:
        return (
            f"machine change {current_machine or 'unknown'} -> {profile.machine} "
            "requires a new Hatari process"
        )
    if not _same_tos(current_tos, profile.tos):
        return "TOS change requires a new Hatari process"
    return None


def describe_profile(profile: LaunchProfile) -> str:
    lines = [
        f"{profile.machine_label} ({profile.machine})",
        f"TOS: {profile.tos_name} [{profile.tos_source}]",
    ]
    if profile.expected_resolution:
        lines.append(
            "expected resolution: "
            f"{profile.expected_resolution[0]}x{profile.expected_resolution[1]}"
        )
    lines.append(f"selection: {profile.source}")
    if profile.extra_args:
        lines.append("extra args: " + " ".join(profile.extra_args))
    for warning in profile.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)
