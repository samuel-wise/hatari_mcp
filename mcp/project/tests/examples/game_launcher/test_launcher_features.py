"""Multi-disk grouping, boot turbo, hot disk switching and art caching."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import Catalog, Config  # noqa: E402
from common.art import ArtResolver  # noqa: E402
from common.catalog import DiskEntry  # noqa: E402
from common.game_runner import (  # noqa: E402
    GameResult,
    GameSession,
    _has_option,
    _strip_disk_args,
    normalise_launch_path,
)
from game_launcher.app import GameLauncherApp  # noqa: E402
from game_launcher.enrichment import default_art_provider, provider_from_config  # noqa: E402
from game_launcher.kairost import KairoSTCatalog  # noqa: E402
from game_launcher.library import (  # noqa: E402
    FilterState,
    GameLibrary,
    group_entries,
    group_titles,
    launch_status,
    variant_label,
)
from game_launcher.metadata import MetadataResolver  # noqa: E402
from game_launcher.service import LauncherService  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if name == "emu_status":
            return {"state": "running"}
        if name == "dbg_registers_get":
            return {"registers": {"pc": 0x1000}}
        return {"ok": True}


class StartSession(GameSession):
    def _wait_for_game(self, **kwargs: Any) -> GameResult:
        return GameResult(
            ok=True,
            game_id=kwargs["game_id"],
            title=kwargs["title"],
            machine=self.current_machine or "st",
            tos=self.current_tos,
            message="game frame",
        )


class SwitchSession(GameSession):
    def _status(self) -> dict[str, Any]:
        return {"state": "running"}


def _disk(root: Path, name: str) -> DiskEntry:
    path = root / name
    path.write_bytes(b"ST" * 30)
    return DiskEntry.from_path(path, root)


def test_multi_disk_entries_group_and_sort(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    for name in (
        "Defender (1987)(Cinemaware)(Disk 2 of 2)[cr BOSS].zip",
        "Defender (1987)(Cinemaware)(Disk 1 of 2)[cr BOSS].zip",
        "Solo (1990)(Pub)(Action).st",
    ):
        (root / name).write_bytes(b"PK" if name.endswith(".zip") else b"ST")
    state = tmp_path / "state"
    catalog = Catalog([root], state_dir=state, metadata_dir=state / "metadata").scan(
        timeout=10
    )
    library = GameLibrary(
        catalog,
        metadata=MetadataResolver(
            state_dir=state,
            metadata_dir=state / "metadata",
            cache_dir=state / "cache" / "art",
        ),
    )

    groups = library.filter_groups(FilterState())
    assert len(groups) == 2
    defender = next(group for group in groups if group.title == "Defender")
    assert defender.multi is True
    assert defender.disk_count == 2
    assert [entry.disk for entry in defender.entries] == [1, 2]
    solo = next(group for group in groups if group.title == "Solo")
    assert solo.multi is False
    assert solo.primary.filename.startswith("Solo")

    page, total = library.page_groups(FilterState(), offset=0, limit=1)
    assert total == 2
    assert len(page) == 1


def test_boot_turbo_is_added_and_restored(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "Turbo (1990)(Pub).st")
    client = FakeClient()
    session = StartSession(
        client,  # type: ignore[arg-type]
        config=Config(tmp_path / "config.json"),
        state_dir=tmp_path / "state",
        turbo=True,
    )

    result = session.start_game(entry)

    assert result.ok, result.message
    start = next(kwargs for name, kwargs in client.calls if name == "emu_start")
    args = start["args"]
    assert "--cpuclock" in args and args[args.index("--cpuclock") + 1] == "32"
    assert "--fast-forward" in args and args[args.index("--fast-forward") + 1] == "on"
    restore = [
        kwargs
        for name, kwargs in client.calls
        if name == "emu_set_option"
        and kwargs.get("options") == "--cpuclock 8 --fast-forward off"
    ]
    assert restore
    assert result.evidence["boot_turbo"] == {
        "enabled": True,
        "restored": True,
        "early_restore": False,
    }


def test_launch_path_is_normalised_and_added_to_emu_start(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "Loader (1990)(Pub).st")
    client = FakeClient()
    session = StartSession(
        client,  # type: ignore[arg-type]
        config=Config(tmp_path / "config.json"),
        state_dir=tmp_path / "state",
        turbo=False,
    )

    result = session.start_game(entry, autostart="FOLDER/RUNME.TOS")

    assert result.ok, result.message
    start = next(kwargs for name, kwargs in client.calls if name == "emu_start")
    args = start["args"]
    assert args[args.index("--auto") + 1] == "A:\\FOLDER\\RUNME.TOS"
    assert result.evidence["launch_path"] == "A:\\FOLDER\\RUNME.TOS"


def test_launch_path_normalisation() -> None:
    assert normalise_launch_path(None) is None
    assert normalise_launch_path("  ") is None
    assert normalise_launch_path("RUNME.PRG") == "A:\\RUNME.PRG"
    assert normalise_launch_path("FOLDER/RUNME.TOS") == "A:\\FOLDER\\RUNME.TOS"
    assert normalise_launch_path("a:RUNME.PRG") == "A:\\RUNME.PRG"
    for value in ("..\\RUNME.PRG", "FOLDER\\..\\RUNME.PRG", "A:", "\\"):
        try:
            normalise_launch_path(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {value!r}")


def test_launch_path_forces_fresh_boot(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "Loader (1990)(Pub).st")
    state = tmp_path / "state"
    metadata = MetadataResolver(
        state_dir=state,
        metadata_dir=state / "metadata",
        cache_dir=state / "cache" / "art",
    )

    class FakeClient:
        def start(self) -> None:
            return None

        def stop(self, **_kwargs: Any) -> None:
            return None

    class FakeSession:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.running = True
            self.current_game_id = "game-1"
            self.current_machine = "st"
            self.current_tos = None
            self.stopped = False
            self.start_kwargs: dict[str, Any] | None = None

        def stop(self) -> dict[str, Any]:
            self.stopped = True
            self.running = False
            return {"ok": True, "state": "stopped"}

        def start_game(self, _entry: Any, **kwargs: Any) -> GameResult:
            self.start_kwargs = kwargs
            return GameResult(
                ok=True,
                game_id="game-1",
                title="Loader",
                machine="st",
                tos=None,
            )

    session = FakeSession()
    service = LauncherService(
        config=Config(tmp_path / "config.json"),
        state_dir=state,
        metadata=metadata,
        client_factory=FakeClient,
        session_factory=lambda *_args, **_kwargs: session,
    )
    import game_launcher.service as service_module

    monkeypatch.setattr(
        service_module.profiles,
        "incompatibility_reason",
        lambda *_args, **_kwargs: None,
    )

    outcome = service.launch(entry, launch_path="RUNME.PRG")

    assert outcome.action == "restart"
    assert session.stopped is True
    assert session.start_kwargs is not None
    assert session.start_kwargs["autostart"] == "A:\\RUNME.PRG"
    assert outcome.recorded["launch_path"] == "A:\\RUNME.PRG"


def test_switch_disk_uses_runtime_option_without_reset(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "Game (1990)(Pub)(Disk 2 of 2).st")
    client = FakeClient()
    session = SwitchSession(
        client,  # type: ignore[arg-type]
        config=Config(tmp_path / "config.json"),
        state_dir=tmp_path / "state",
        disk_turbo_seconds=0.01,
    )
    session.current_game_id = "game-1"
    session.current_title = "Game"
    session.current_machine = "st"

    result = session.switch_disk(entry)

    assert result.ok, result.message
    names = [name for name, _kwargs in client.calls]
    assert names == ["emu_set_option", "emu_set_option", "emu_set_option"]
    options = client.calls[0][1]["options"]
    assert options.startswith("--disk-a ")
    assert client.calls[1][1]["options"] == "--cpuclock 32 --fast-forward on"
    assert client.calls[2][1]["options"] == "--cpuclock 8 --fast-forward off"
    assert "emu_reset" not in names
    assert session.current_disk is not None
    assert result.evidence["disk_turbo"] == {
        "enabled": True,
        "seconds": 0.01,
        "restored": True,
    }


def test_strip_disk_args_removes_value() -> None:
    assert _strip_disk_args(["--disk-a", "/tmp/a.st", "--sound", "off"]) == [
        "--sound",
        "off",
    ]
    assert _strip_disk_args(["--disk-a=/tmp/a.st", "--disk-b", "/tmp/b.st"]) == []
    assert _has_option(["--cpuclock=16"], "--cpuclock")
    assert not _has_option(["--sound", "off"], "--cpuclock")


def test_metadata_accepts_remote_art_url(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    disk = root / "Remote (1990)(Pub).st"
    disk.write_bytes(b"ST")
    entry = DiskEntry.from_path(disk, root)
    sidecar = disk.with_suffix(disk.suffix + ".json")
    sidecar.write_text(
        json.dumps({"art": "https://example.invalid/remote.png"}),
        encoding="utf-8",
    )
    resolver = MetadataResolver(
        state_dir=tmp_path / "state",
        metadata_dir=tmp_path / "state" / "metadata",
        cache_dir=tmp_path / "state" / "cache" / "art",
    )

    resolved = resolver.resolve(entry)

    assert resolved.art_path is None
    assert resolved.art_url == "https://example.invalid/remote.png"
    assert resolved.provenance["art"] == f"sidecar:{sidecar}"


def test_art_download_caches_and_reuses_file(tmp_path: Path, monkeypatch: Any) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"test"
    calls = {"count": 0}

    class Response:
        headers = {"Content-Type": "image/png"}

        def read(self, _limit: int) -> bytes:
            return png

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(_request: Any, timeout: float = 0) -> Response:
        calls["count"] += 1
        return Response()

    monkeypatch.setattr("common.art.urllib.request.urlopen", fake_urlopen)
    resolver = ArtResolver(tmp_path / "art")

    path = resolver.download("https://example.invalid/cover.png", "entry-1")
    assert path is not None
    assert path.name == "entry-1.png"
    assert path.read_bytes() == png

    again = resolver.download("https://example.invalid/cover.png", "entry-1")
    assert again == path
    assert calls["count"] == 1


def test_default_libretro_art_uses_tosec_stem(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    name = "Defender of the Crown (1987)(Cinemaware)[one disk].zip"
    (root / name).write_bytes(b"PK")
    entry = DiskEntry.from_path(root / name, root)
    resolver = MetadataResolver(
        state_dir=tmp_path / "state",
        metadata_dir=tmp_path / "state" / "metadata",
        cache_dir=tmp_path / "state" / "cache" / "art",
    )
    resolved = resolver.resolve(entry)

    provider = default_art_provider()
    url = provider.art_url(entry, resolved)

    assert url == (
        "https://thumbnails.libretro.com/Atari%20-%20ST/Named_Boxarts/"
        "Defender%20of%20the%20Crown%20%281987%29%28Cinemaware%29%5Bone%20disk%5D.png"
    )
    assert provider_from_config({"online": {"provider": "libretro"}}) is not None


def test_title_groups_collapse_variants_and_keep_remakes_separate(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    names = (
        "Defender (1987)(Cinemaware)(Disk 1 of 2)[!].zip",
        "Defender (1987)(Cinemaware)(Disk 2 of 2)[!].zip",
        "Defender (1987)(Cinemaware)[cr BOSS][one disk].zip",
        "Defender (1987)(Cinemaware)[tr fr partial][one disk].zip",
        "The Pictures of Defender (1987)(Cinemaware).st",
    )
    for name in names:
        (root / name).write_bytes(b"PK" if name.endswith(".zip") else b"ST")
    state = tmp_path / "state"
    catalog = Catalog([root], state_dir=state, metadata_dir=state / "metadata").scan(
        timeout=10
    )
    library = GameLibrary(
        catalog,
        metadata=MetadataResolver(
            state_dir=state,
            metadata_dir=state / "metadata",
            cache_dir=state / "cache" / "art",
            kairost=KairoSTCatalog(tmp_path / "missing-kairost"),
        ),
    )

    releases = library.filter_groups(FilterState())
    titles = library.filter_titles(FilterState())

    assert len(releases) == 4
    assert len(titles) == 2
    defender = next(title for title in titles if title.title == "Defender")
    assert defender.multi is True
    assert defender.release_count == 3
    assert {variant_label(release) for release in defender.releases} == {
        "[!]",
        "[cr BOSS]",
        "[tr fr partial]",
    }
    assert sorted(release.multi for release in defender.releases) == [False, False, True]
    pictures = next(title for title in titles if title.title.startswith("The Pictures"))
    assert pictures.multi is False
    page, total = library.page_titles(FilterState(), offset=0, limit=1)
    assert total == 2
    assert len(page) == 1


def test_group_titles_preserves_order_and_sorting(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    for name in (
        "Zeta (1990)(Pub)[a].st",
        "Alpha (1990)(Pub)[b].st",
        "Alpha (1990)(Pub)[a].st",
    ):
        (root / name).write_bytes(b"ST")
    state = tmp_path / "state"
    catalog = Catalog([root], state_dir=state, metadata_dir=state / "metadata").scan(
        timeout=10
    )
    library = GameLibrary(
        catalog,
        metadata=MetadataResolver(
            state_dir=state,
            metadata_dir=state / "metadata",
            cache_dir=state / "cache" / "art",
            kairost=KairoSTCatalog(tmp_path / "missing-kairost"),
        ),
    )
    titles = library.filter_titles(FilterState())
    assert [title.title for title in titles] == ["Alpha", "Zeta"]
    alpha = titles[0]
    assert alpha.multi
    assert [variant_label(release) for release in alpha.releases] == ["[a]", "[b]"]
    assert group_titles(library.filter_groups(FilterState()))[0].release_count == 2


def test_launch_status_classification() -> None:
    assert launch_status([]) == "untested"
    assert launch_status([{}]) == "untested"
    assert launch_status([{"last_success": 1}]) == "success"
    assert launch_status([{"last_result": {"at": 1, "ok": True}}]) == "success"
    assert launch_status([{"last_result": {"at": 1, "ok": False}}]) == "failed"
    assert (
        launch_status(
            [{"last_success": 1, "last_result": {"at": 2, "ok": False}}]
        )
        == "regressed"
    )
    assert (
        launch_status(
            [
                {"last_result": {"at": 5, "ok": True}},
                {"last_result": {"at": 9, "ok": False}},
            ]
        )
        == "regressed"
    )
    assert (
        launch_status(
            [
                {"last_result": {"at": 9, "ok": True}},
                {"last_result": {"at": 5, "ok": False}},
            ]
        )
        == "success"
    )


def test_release_status_prefers_any_working_disk(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entries = [
        _disk(root, "Defender (1987)(Cinemaware)(Disk 1 of 2)[cr BOSS].st"),
        _disk(root, "Defender (1987)(Cinemaware)(Disk 2 of 2)[cr BOSS].st"),
        _disk(root, "Defender (1987)(Cinemaware)[a].st"),
    ]
    releases = group_entries(entries)
    multi = next(release for release in releases if release.multi)
    alternative = next(release for release in releases if not release.multi)
    assert multi.disk_total == 2
    assert alternative.disk_total == 1

    records = {
        multi.primary.id: {
            "last_success": 1,
            "last_result": {"at": 10, "ok": True},
        },
        multi.entries[1].id: {"last_result": {"at": 20, "ok": False}},
        alternative.primary.id: {"last_result": {"at": 30, "ok": False}},
    }

    class FakeConfig:
        def get_game(self, game_id: str) -> dict[str, Any]:
            return records.get(game_id, {})

    harness = GameLauncherApp.__new__(GameLauncherApp)
    harness.config = FakeConfig()
    assert harness._release_status(multi) == "success"
    titles = group_titles(releases)
    defender = next(title for title in titles if title.title == "Defender")
    assert defender.release_count == 2
    assert harness._title_status(defender) == "success"

    records[multi.primary.id] = {}
    records[multi.entries[1].id] = {"last_result": {"at": 20, "ok": True}}
    assert harness._release_status(multi) == "success"

    records[multi.primary.id] = {"last_result": {"at": 10, "ok": False}}
    records[multi.entries[1].id] = {"last_result": {"at": 40, "ok": False}}
    assert harness._release_status(multi) == "failed"
    assert harness._title_status(defender) == "failed"


def test_kairost_metadata_and_local_art(tmp_path: Path) -> None:
    data = tmp_path / "kairost"
    (data / "research").mkdir(parents=True)
    (data / "research" / "games-v1.json").write_text(
        json.dumps(
            {
                "games": {
                    "atarist:1": {
                        "title": "Canonical Defender",
                        "year": 1986,
                        "publisher": "Cinemaware",
                        "developer": "Cinemaware",
                        "description": "A strategy classic.",
                        "launchbox": 123,
                        "sourceGroups": [
                            "atarist:Atari ST - Games/Catalog Game (1987)(Pub)[one disk].zip"
                        ],
                    },
                    "atarist:2": {
                        "title": "Remote Art Game",
                        "year": 1991,
                        "boxArtUrl": "https://example.invalid/box.jpg",
                        "sourceGroups": [
                            "atarist:Atari ST - Games/Remote Art Game (1991)(Pub).zip"
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    art_dir = data / "artwork" / "art" / "catalog" / "atarist" / "123"
    art_dir.mkdir(parents=True)
    box = art_dir / "box-abc.webp"
    box.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 20)
    catalog = KairoSTCatalog(data)
    assert catalog.available

    root = tmp_path / "games"
    root.mkdir()
    disk = root / "Catalog Game (1987)(Pub)[one disk].zip"
    disk.write_bytes(b"PK")
    entry = DiskEntry.from_path(disk, root)
    game = catalog.lookup(entry)
    assert game is not None
    assert game.metadata()["title"] == "Canonical Defender"
    assert game.local_art() == box.resolve()

    resolver = MetadataResolver(
        state_dir=tmp_path / "state",
        metadata_dir=tmp_path / "state" / "metadata",
        cache_dir=tmp_path / "state" / "cache" / "art",
        kairost=catalog,
    )
    resolved = resolver.resolve(entry)
    assert resolved.title == "Canonical Defender"
    assert resolved.year == 1986
    assert resolved.publisher == "Cinemaware"
    assert resolved.art_path == box.resolve()
    assert resolved.provenance["title"] == "kairost:atarist:1"
    assert resolved.provenance["art"] == "kairost:atarist:1"

    remote = root / "Remote Art Game (1991)(Pub).zip"
    remote.write_bytes(b"PK")
    remote_entry = DiskEntry.from_path(remote, root)
    remote_resolved = resolver.resolve(remote_entry)
    assert remote_resolved.art_path is None
    assert remote_resolved.art_url == "https://example.invalid/box.jpg"


def test_tos_choice_is_saved_per_game(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "TOS Choice (1990)(Pub).st")
    tos = tmp_path / "custom.img"
    tos.write_bytes(b"\0" * 262144)
    config = Config(tmp_path / "config.json")

    class Var:
        def __init__(self) -> None:
            self.value = "Auto (profile)"

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    harness = GameLauncherApp.__new__(GameLauncherApp)
    harness.config = config
    harness._selected_entry = entry
    harness._busy = False
    harness._closing = False
    harness._tos_entry_id = None
    harness._tos_options = {}
    harness.tos_choice_var = Var()
    harness._refresh_tos_values = lambda: None
    harness._show_details = lambda _entry: None
    harness.status_log = type(
        "Log", (), {"log": lambda *_args, **_kwargs: None}
    )()

    harness._set_tos_choice(str(tos))
    harness._on_tos_selected()

    assert harness._requested_tos() == str(tos.resolve())
    assert config.get_game(entry.id)["tos_override"] == str(tos.resolve())

    harness.tos_choice_var.set("Auto (profile)")
    harness._on_tos_selected()
    assert config.get_game(entry.id)["tos_override"] is None


def test_external_exit_clears_running_state() -> None:
    class Var:
        def __init__(self) -> None:
            self.value = ""

        def set(self, value: str) -> None:
            self.value = value

    messages: list[str] = []

    class Tasks:
        def submit(self, fn: Any, *, on_success: Any, on_error: Any, name: str) -> None:
            on_success(fn())

    harness = GameLauncherApp.__new__(GameLauncherApp)
    harness._closing = False
    harness._status_inflight = False
    harness._running = True
    harness._can_stop = True
    harness._running_group_key = "release"
    harness._last_launch_group_key = "release"
    harness._running_disk_id = "disk"
    harness._mcp_state = "running"
    harness.mcp_status_var = Var()
    harness.status_log = type(
        "Log",
        (),
        {"log": lambda _self, message, **_kwargs: messages.append(message)},
    )()
    harness._set_controls = lambda: None
    harness.tasks = Tasks()
    harness.service = type(
        "Service", (), {"status": lambda _self: {"state": "exited"}}
    )()

    harness._update_mcp_status()

    assert harness._running is False
    assert harness._can_stop is False
    assert harness._running_group_key is None
    assert harness._last_launch_group_key is None
    assert messages and "no longer running" in messages[0]


def test_launch_recording_preserves_tos_override(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.json")
    config.update_game(
        "game-id",
        {
            "tos_override": "/tmp/etos192us.img",
            "launch_path": "A:\\RUNME.PRG",
        },
    )

    config.record_launch(
        "game-id",
        machine="st",
        profile="st",
        tos=tmp_path / "tos104us.img",
        status="success",
        success=True,
    )

    record = config.get_game("game-id")
    assert record["tos_override"] == "/tmp/etos192us.img"
    assert record["launch_path"] == "A:\\RUNME.PRG"
    assert record["machine"] == "st"


def test_requested_tos_forces_restart_and_is_used(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    entry = _disk(root, "TOS Boot (1990)(Pub).st")
    current_tos = tmp_path / "current.img"
    requested_tos = tmp_path / "requested.img"
    current_tos.write_bytes(b"\0" * 262144)
    requested_tos.write_bytes(b"\0" * 262144)
    state = tmp_path / "state"
    metadata = MetadataResolver(
        state_dir=state,
        metadata_dir=state / "metadata",
        cache_dir=state / "cache" / "art",
    )

    class FakeClient:
        def start(self) -> None:
            return None

        def stop(self, **_kwargs: Any) -> None:
            return None

    class FakeSession:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.running = True
            self.current_game_id = "game-1"
            self.current_machine = "st"
            self.current_tos = current_tos
            self.start_kwargs: dict[str, Any] | None = None

        def stop(self) -> dict[str, Any]:
            self.running = False
            return {"ok": True, "state": "stopped"}

        def start_game(self, _entry: Any, **kwargs: Any) -> GameResult:
            self.start_kwargs = kwargs
            return GameResult(
                ok=True,
                game_id="game-1",
                title="TOS Boot",
                machine="st",
                tos=str(kwargs.get("tos")) if kwargs.get("tos") else None,
            )

    session = FakeSession()
    service = LauncherService(
        config=Config(tmp_path / "config.json"),
        state_dir=state,
        metadata=metadata,
        client_factory=FakeClient,
        session_factory=lambda *_args, **_kwargs: session,
    )

    outcome = service.launch(entry, requested_tos=requested_tos)

    assert outcome.action == "restart"
    assert session.start_kwargs is not None
    assert Path(str(session.start_kwargs["tos"])).resolve() == requested_tos.resolve()
