"""Tkinter Atari ST game launcher.

The catalog scans on its own daemon thread.  All MCP work (start/swap/stop/close
and status checks) runs through ``common.ui.TaskRunner`` so the Tk event loop
never waits for Hatari.  Online enrichment is on by default; metadata needs a
provider URL, while box art uses the configured template or Libretro.
"""

from __future__ import annotations

import argparse
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import Catalog, Config, paths  # noqa: E402
from common.art import ArtResolver, placeholder_for  # noqa: E402
from common.game_runner import normalise_launch_path  # noqa: E402
from common.ui import StatusLog, TaskRunner  # noqa: E402

from . import profiles  # noqa: E402
from .enrichment import (  # noqa: E402
    HttpEnricher,
    enrichment_values,
    provider_from_config,
)
from .genres import GenreResolver  # noqa: E402
from .kairost import KairoSTCatalog  # noqa: E402
from .library import (  # noqa: E402
    FilterState,
    GameGroup,
    GameLibrary,
    TitleGroup,
    filters_from_values,
    launch_status,
    variant_label,
)
from .metadata import MetadataResolver, ResolvedMetadata  # noqa: E402
from .service import LauncherService  # noqa: E402
from .settings import (  # noqa: E402
    PathSettings,
    default_game_paths,
    from_config as load_path_settings,
    save as save_path_settings,
)
from .settings_ui import SettingsDialog  # noqa: E402

_FILTER_MACHINE_LABELS = [profiles.MACHINE_LABELS[machine] for machine in profiles.MACHINES]
_FILTER_MACHINE_MAP = {
    profiles.MACHINE_LABELS[machine]: machine for machine in profiles.MACHINES
}
_LAUNCH_MACHINE_VALUES = ["Auto"] + _FILTER_MACHINE_LABELS
_LAUNCH_MACHINE_MAP = {label: machine for label, machine in _FILTER_MACHINE_MAP.items()}
_PAGE_SIZE_DEFAULT = 100


def _tos_options(candidates: dict[str, Path]) -> dict[str, str]:
    """Map friendly TOS labels to resolved image paths."""
    options: dict[str, str] = {}
    for kind, path in sorted(candidates.items()):
        resolved = Path(path).expanduser().resolve()
        options[f"{kind}: {resolved.name}"] = str(resolved)
    return options


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _set_text(widget: tk.Text, text: str) -> None:
    try:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")
    except tk.TclError:
        return


def _short(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ArtCanvas(ttk.Frame):
    """Draw cached art, or a deterministic colored placeholder."""

    def __init__(self, parent: Any, *, width: int = 360, height: int = 360) -> None:
        super().__init__(parent)
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(
            self,
            width=width,
            height=height,
            bg="#0d1117",
            highlightthickness=1,
            highlightbackground="#2c3440",
        )
        self.canvas.pack(fill="both", expand=True)
        self._photo: Any = None

    def show(
        self,
        entry: Any,
        resolved: ResolvedMetadata,
        resolver: ArtResolver,
        *,
        master: Any,
    ) -> str:
        self.canvas.delete("all")
        self._photo = None
        neutral = ttk.Style(self).lookup("TFrame", "background") or "#0d1117"
        self.canvas.configure(bg=neutral, highlightthickness=0)
        path = resolved.art_path
        source = resolved.art_source
        if path is None:
            found = resolver.find(entry)
            if found is not None:
                path = found.path
                source = f"cache:{found.path}"
        if path is not None:
            photo = resolver.load_tk_photo(
                path,
                master=master,
                max_width=self.width - 20,
                max_height=self.height - 20,
            )
            if photo is not None:
                # Tk's PhotoImage supports subsample; ImageTk.PhotoImage does
                # not, so Pillow-backed images are already scaled above.
                if hasattr(photo, "subsample"):
                    factor = 1
                    while (
                        photo.width() // factor > self.width - 20
                        or photo.height() // factor > self.height - 20
                    ) and factor < 16:
                        factor += 1
                    if factor > 1:
                        photo = photo.subsample(factor)
                self._photo = photo
                self.canvas.create_image(
                    self.width // 2, self.height // 2, image=photo, anchor="center"
                )
                self.canvas.create_text(
                    8,
                    self.height - 8,
                    text=_short(source or str(path), 80),
                    anchor="sw",
                    fill="#9aa7b8",
                    font=("TkDefaultFont", 8),
                )
                return source or f"cache:{path}"

        palette = placeholder_for(
            {
                "id": _entry_value(entry, "id", ""),
                "title": resolved.title,
                "year": resolved.year,
                "publisher": resolved.publisher,
            }
        )
        background = palette.background
        accent = palette.accent
        foreground = palette.foreground
        self.canvas.configure(
            bg=background,
            highlightthickness=1,
            highlightbackground="#2c3440",
        )
        self.canvas.create_rectangle(
            0, 0, self.width, self.height, fill=background, outline=""
        )
        self.canvas.create_rectangle(
            0, self.height - 10, self.width, self.height, fill=accent, outline=""
        )
        for index in range(5):
            x = 20 + index * 47
            self.canvas.create_line(
                x, 14, x + 90, self.height - 24, fill=accent, width=1
            )
        self.canvas.create_text(
            self.width // 2,
            self.height // 2 - 18,
            text=palette.initials,
            fill=foreground,
            font=("TkDefaultFont", 46, "bold"),
        )
        self.canvas.create_text(
            self.width // 2,
            self.height // 2 + 44,
            text=_short(resolved.title, 44),
            fill=foreground,
            font=("TkDefaultFont", 12, "bold"),
            width=self.width - 40,
            justify="center",
        )
        detail = " ".join(
            str(value)
            for value in (resolved.year or "", resolved.publisher or "")
            if value
        )
        if detail:
            self.canvas.create_text(
                self.width // 2,
                self.height // 2 + 70,
                text=_short(detail, 56),
                fill=foreground,
                font=("TkDefaultFont", 9),
                width=self.width - 40,
            )
        self.canvas.create_text(
            8,
            self.height - 8,
            text="deterministic placeholder art",
            anchor="sw",
            fill=foreground,
            font=("TkDefaultFont", 8),
        )
        return "generated placeholder"


class GameLauncherApp:
    """Main window; safe to construct headlessly when a Tk root exists."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        roots: list[str | Path] | None = None,
        config: Config | None = None,
        state_dir: str | Path | None = None,
        page_size: int = _PAGE_SIZE_DEFAULT,
        default_requested_machine: str | None = None,
    ):
        self.root = root
        self.config = config or Config()
        self.state_dir = (
            Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        )
        self.page_size = max(10, int(page_size))
        self.root.title("Atari ST Game Launcher")
        self.root.geometry("1280x840")
        self.root.minsize(1040, 680)

        self.path_settings = load_path_settings(self.config)
        self._explicit_roots = roots is not None
        self._cli_roots = (
            [Path(item).expanduser().resolve() for item in roots]
            if roots is not None
            else []
        )
        installed_roots = self._catalog_roots()
        self.kairost = KairoSTCatalog(self.path_settings.catalog_dirs)
        self.metadata_resolver = MetadataResolver(
            state_dir=self.state_dir,
            metadata_dir=self.state_dir / "metadata",
            cache_dir=self.state_dir / "cache" / "art",
            kairost=self.kairost,
        )
        self.art_resolver = ArtResolver(self.state_dir / "cache" / "art")
        self.catalog = Catalog(
            installed_roots,
            state_dir=self.state_dir,
            metadata_dir=self.state_dir / "metadata",
        )
        self.library = GameLibrary(
            self.catalog, metadata=self.metadata_resolver, config=self.config
        )
        self.boot_turbo = bool(self.path_settings.boot_turbo)
        self.online_enrichment = bool(self.path_settings.online_enrichment)
        self.status_visible = bool(self.config.ui_get("status_visible", False))
        self._tos_candidates = profiles.discover_tos_candidates(
            self.path_settings.tos_dirs
        )
        self._tos_options = _tos_options(self._tos_candidates)
        online_config = self.config.get("online", {}) or {}
        mobygames_key = (
            online_config.get("mobygames_key")
            if isinstance(online_config, dict)
            else None
        )
        self.genres = GenreResolver(self.state_dir, mobygames_key=mobygames_key)
        self.service = LauncherService(
            config=self.config,
            state_dir=self.state_dir,
            metadata=self.metadata_resolver,
            turbo=self.boot_turbo,
            hatari_bin=self.path_settings.hatari_bin,
            tos_dirs=self.path_settings.tos_dirs,
            tos_candidates=self._tos_candidates,
        )
        self.tasks = TaskRunner(self.root)

        self._page_offset = 0
        self._page_entries: list[Any] = []
        self._page_groups: list[GameGroup] = []
        self._page_titles: list[TitleGroup] = []
        self._selected_id: str | None = None
        self._selected_entry: Any = None
        self._selected_group: GameGroup | None = None
        self._selected_title: TitleGroup | None = None
        self._tree_entries: dict[str, Any] = {}
        self._tree_groups: dict[str, GameGroup] = {}
        self._tree_titles: dict[str, TitleGroup] = {}
        self._entry_iids: dict[str, str] = {}
        self._entry_groups: dict[str, GameGroup] = {}
        self._entry_titles: dict[str, TitleGroup] = {}
        self._running_group_key: str | None = None
        self._last_launch_group_key: str | None = None
        self._running_disk_id: str | None = None
        self._last_outcome: Any = None
        self._scan_count = -1
        self._facets_started = False
        self._busy = False
        self._closing = False
        self._running = False
        self._can_stop = False
        self._poll_job: str | None = None
        self._status_job: str | None = None
        self._status_inflight = False
        self._mcp_state: str | None = None
        self._search_job: str | None = None
        self._enrichment_started = False
        self._enriched_ids: list[str] = []
        self._enriched_lock = threading.Lock()
        self._art_attempts: set[tuple[str, str]] = set()
        self._genre_attempts: set[str] = set()
        self._launch_path_entry_id: str | None = None
        self._tos_entry_id: str | None = None

        self._build_variables()
        self._configure_style()
        self._build_ui()
        self.status_button.configure(
            text="Hide status" if self.status_visible else "Show status"
        )
        self._bind_events()
        if default_requested_machine in profiles.MACHINES:
            for label, machine in _LAUNCH_MACHINE_MAP.items():
                if machine == default_requested_machine:
                    self.launch_machine_var.set(label)
                    break
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.status_log.log(
            "Catalog roots: " + ", ".join(str(path) for path in installed_roots)
        )
        self.catalog.start_scan()
        self._poll_scan()
        self._poll_status()

    # -- construction --------------------------------------------------

    def _catalog_roots(self) -> list[Path]:
        if self._explicit_roots and self._cli_roots:
            return list(self._cli_roots)
        return default_game_paths(self.path_settings)

    def _build_variables(self) -> None:
        self.search_var = tk.StringVar()
        self.filter_machine_var = tk.StringVar(value="Any")
        self.filter_genre_var = tk.StringVar(value="Any")
        self.filter_year_var = tk.StringVar(value="Any")
        self.launch_machine_var = tk.StringVar(value="Auto")
        self.launch_path_var = tk.StringVar(value="")
        self.tos_choice_var = tk.StringVar(value="Auto (profile)")
        self.online_var = tk.BooleanVar(value=self.online_enrichment)
        self.boot_turbo_var = tk.BooleanVar(value=self.boot_turbo)
        self.disk_var = tk.StringVar(value="")
        self.scan_status_var = tk.StringVar(value="Scanning…")
        self.mcp_status_var = tk.StringVar(value="MCP: not started")
        self.page_var = tk.StringVar(value="Page 1")
        self.total_var = tk.StringVar(value="0 games")
        self.title_var = tk.StringVar(value="Select a game")
        self.year_var = tk.StringVar(value="—")
        self.publisher_var = tk.StringVar(value="—")
        self.genre_var = tk.StringVar(value="—")
        self.developer_var = tk.StringVar(value="—")
        self.machine_var = tk.StringVar(value="—")
        self.tos_var = tk.StringVar(value="—")
        self.resolution_var = tk.StringVar(value="—")
        self.description_var = tk.StringVar(value="—")
        self.filename_var = tk.StringVar(value="—")
        self.learned_var = tk.StringVar(value="No learned configuration yet")
        self.result_var = tk.StringVar(value="No launch result yet")

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
        style.configure("Subtitle.TLabel", font=("TkDefaultFont", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Launch.TButton", font=("TkDefaultFont", 10, "bold"))
        style.configure("Treeview", rowheight=22)

    def _build_ui(self) -> None:
        self._build_toolbar()
        self.paned = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            sashwidth=6,
            bd=0,
            bg="#20252d",
        )
        self.paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(self.paned, padding=8)
        center = ttk.Frame(self.paned, padding=8)
        self.status_frame = ttk.Frame(self.paned, padding=8)
        self.paned.add(left, minsize=360, width=450)
        self.paned.add(center, minsize=520, width=660)
        self._build_catalog(left)
        self._build_details(center)
        self._build_status(self.status_frame)
        if self.status_visible:
            self.paned.add(self.status_frame, minsize=280, width=320)

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        toolbar.pack(fill="x")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(toolbar, text="Machine").grid(row=0, column=2, sticky="w")
        self.filter_machine = ttk.Combobox(
            toolbar,
            textvariable=self.filter_machine_var,
            values=["Any"] + _FILTER_MACHINE_LABELS,
            state="readonly",
            width=11,
        )
        self.filter_machine.grid(row=0, column=3, padx=(4, 10))
        ttk.Label(toolbar, text="Genre").grid(row=0, column=4, sticky="w")
        self.filter_genre = ttk.Combobox(
            toolbar,
            textvariable=self.filter_genre_var,
            values=["Any"],
            state="readonly",
            width=13,
        )
        self.filter_genre.grid(row=0, column=5, padx=(4, 10))
        ttk.Label(toolbar, text="Year").grid(row=0, column=6, sticky="w")
        self.filter_year = ttk.Combobox(
            toolbar,
            textvariable=self.filter_year_var,
            values=["Any"],
            state="readonly",
            width=9,
        )
        self.filter_year.grid(row=0, column=7, padx=(4, 10))
        ttk.Button(toolbar, text="Settings…", command=self._open_settings).grid(
            row=0, column=8, padx=(0, 10)
        )
        self.status_button = ttk.Button(
            toolbar, text="Show status", command=self._toggle_status
        )
        self.status_button.grid(row=0, column=9, sticky="e")

    def _build_catalog(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Catalog", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.total_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("title", "variant", "disks", "year", "machine", "genre"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=22, stretch=False)
        self.tree.heading("title", text="Title")
        self.tree.heading("variant", text="Variant")
        self.tree.heading("disks", text="Disks")
        self.tree.heading("year", text="Year")
        self.tree.heading("machine", text="Machine")
        self.tree.heading("genre", text="Genre")
        self.tree.column("title", width=220, anchor="w")
        self.tree.column("variant", width=128, anchor="w", stretch=False)
        self.tree.column("disks", width=56, anchor="center", stretch=False)
        self.tree.column("year", width=54, anchor="center", stretch=False)
        self.tree.column("machine", width=76, anchor="center", stretch=False)
        self.tree.column("genre", width=96, anchor="w", stretch=False)
        self.tree.tag_configure("success", foreground="#15803d")
        self.tree.tag_configure("failed", foreground="#dc2626")
        self.tree.tag_configure("regressed", foreground="#d97706")
        self.tree.tag_configure("untested", foreground="#111827")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        pager = ttk.Frame(parent)
        pager.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        pager.columnconfigure(1, weight=1)
        self.prev_button = ttk.Button(pager, text="◀ Prev", command=self._previous_page)
        self.prev_button.grid(row=0, column=0, sticky="w")
        ttk.Label(pager, textvariable=self.page_var, anchor="center").grid(
            row=0, column=1, sticky="ew"
        )
        self.next_button = ttk.Button(pager, text="Next ▶", command=self._next_page)
        self.next_button.grid(row=0, column=2, sticky="e")

    def _build_details(self, parent: ttk.Frame) -> None:
        self._details_frame = parent
        parent.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(
            parent,
            textvariable=self.title_var,
            style="Title.TLabel",
            wraplength=640,
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        body = ttk.Frame(parent)
        body.grid(row=1, column=0, sticky="new", pady=(6, 4))
        body.columnconfigure(1, weight=1)
        self.art_canvas = ArtCanvas(body, width=360, height=360)
        self.art_canvas.grid(row=0, column=0, sticky="nw", padx=(0, 14))

        fields = ttk.Frame(body)
        fields.grid(row=0, column=1, sticky="nsew")
        fields.columnconfigure(1, weight=1)
        rows = [
            ("Year", self.year_var),
            ("Genre", self.genre_var),
            ("Publisher", self.publisher_var),
            ("Developer", self.developer_var),
            ("Machine", self.machine_var),
            ("TOS", self.tos_var),
            ("Resolution", self.resolution_var),
        ]
        for index, (label, variable) in enumerate(rows):
            ttk.Label(fields, text=label, style="Subtitle.TLabel").grid(
                row=index, column=0, sticky="nw", padx=(0, 8), pady=2
            )
            ttk.Label(
                fields,
                textvariable=variable,
                wraplength=250,
                justify="left",
            ).grid(row=index, column=1, sticky="nw", pady=2)

        self.description_frame = ttk.Frame(parent)
        self.description_frame.grid(row=2, column=0, sticky="ew", pady=(4, 6))
        self.description_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.description_frame, text="Description", style="Subtitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        self.description_label = ttk.Label(
            self.description_frame,
            textvariable=self.description_var,
            wraplength=620,
            justify="left",
            anchor="w",
        )
        self.description_label.grid(
            row=1, column=0, sticky="ew", pady=(2, 0)
        )

        controls = ttk.Frame(parent)
        self.controls_frame = controls
        controls.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(4, weight=1)
        ttk.Label(controls, text="Launch as").grid(row=0, column=0, sticky="w")
        self.launch_machine = ttk.Combobox(
            controls,
            textvariable=self.launch_machine_var,
            values=_LAUNCH_MACHINE_VALUES,
            state="readonly",
            width=13,
        )
        self.launch_machine.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.launch_button = ttk.Button(
            controls,
            text="Launch",
            style="Launch.TButton",
            command=self._launch,
        )
        self.launch_button.grid(row=0, column=2, padx=(0, 6))
        self.stop_button = ttk.Button(
            controls,
            text="Stop",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=3, sticky="w")
        ttk.Label(controls, text="TOS").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.tos_combo = ttk.Combobox(
            controls,
            textvariable=self.tos_choice_var,
            values=["Auto (profile)"]
            + sorted(self._tos_options, key=str.casefold),
            state="readonly",
            width=34,
        )
        self.tos_combo.grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(6, 12),
            pady=(4, 0),
        )
        self.tos_browse_button = ttk.Button(
            controls,
            text="Browse…",
            command=self._browse_tos,
        )
        self.tos_browse_button.grid(row=1, column=4, sticky="w", pady=(4, 0))
        ttk.Label(controls, text="Launch path").grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        self.launch_path_entry = ttk.Entry(
            controls, textvariable=self.launch_path_var
        )
        self.launch_path_entry.grid(
            row=2,
            column=1,
            columnspan=4,
            sticky="ew",
            padx=(6, 0),
            pady=(4, 0),
        )
        ttk.Label(controls, text="Disk").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.disk_combo = ttk.Combobox(
            controls,
            textvariable=self.disk_var,
            values=[],
            state="disabled",
            width=34,
        )
        self.disk_combo.grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(6, 12), pady=(4, 0)
        )
        self.insert_button = ttk.Button(
            controls,
            text="Insert disk",
            command=self._insert_disk,
            state="disabled",
        )
        self.insert_button.grid(row=3, column=4, sticky="w", pady=(4, 0))
        self.file_label = ttk.Label(controls, text="File")
        self.file_label.grid(row=4, column=0, sticky="nw", pady=(6, 0))
        self.filename_label = ttk.Label(
            controls,
            textvariable=self.filename_var,
            wraplength=560,
            justify="left",
            anchor="w",
        )
        self.filename_label.grid(
            row=4,
            column=1,
            columnspan=4,
            sticky="ew",
            padx=(6, 0),
            pady=(6, 0),
        )

        ttk.Label(parent, text="Provenance", style="Subtitle.TLabel").grid(
            row=4, column=0, sticky="w", pady=(4, 2)
        )
        self.provenance_text = tk.Text(
            parent,
            height=3,
            wrap="word",
            state="disabled",
            bg="#11161d",
            fg="#b9c4d4",
            insertbackground="#b9c4d4",
            relief="flat",
            padx=6,
            pady=4,
        )
        self.provenance_text.grid(row=5, column=0, sticky="ew")

        self._details_frame.bind("<Configure>", self._sync_detail_wraps)
        self.description_frame.bind("<Configure>", self._sync_detail_wraps)
        self.controls_frame.bind("<Configure>", self._sync_detail_wraps)
        self.root.after_idle(self._sync_detail_wraps)

        ttk.Label(parent, text="Learned configuration / last result", style="Subtitle.TLabel").grid(
            row=6, column=0, sticky="w", pady=(6, 2)
        )
        self.learned_text = tk.Text(
            parent,
            height=5,
            wrap="word",
            state="disabled",
            bg="#11161d",
            fg="#b9c4d4",
            insertbackground="#b9c4d4",
            relief="flat",
            padx=6,
            pady=4,
        )
        self.learned_text.grid(row=7, column=0, sticky="nsew")
        parent.rowconfigure(7, weight=1)

    def _build_status(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Status", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(header, text="Clear", command=lambda: self.status_log.clear()).grid(
            row=0, column=1, sticky="e"
        )
        labels = ttk.Frame(parent)
        labels.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        labels.columnconfigure(0, weight=1)
        ttk.Label(labels, textvariable=self.scan_status_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(labels, textvariable=self.mcp_status_var).grid(
            row=1, column=0, sticky="w"
        )
        self.status_log = StatusLog(parent, height=28)
        self.status_log.grid(row=2, column=0, sticky="nsew")
        self.status_log.text.configure(
            bg="#11161d", fg="#d5dde8", insertbackground="#d5dde8", relief="flat"
        )

    def _bind_events(self) -> None:
        self.search_entry.bind("<KeyRelease>", self._on_search_changed)
        self.filter_machine.bind(
            "<<ComboboxSelected>>", lambda _event: self._filters_changed()
        )
        self.filter_genre.bind(
            "<<ComboboxSelected>>", lambda _event: self._filters_changed()
        )
        self.filter_year.bind(
            "<<ComboboxSelected>>", lambda _event: self._filters_changed()
        )
        self.launch_machine.bind(
            "<<ComboboxSelected>>", lambda _event: self._render_selected()
        )
        self.tos_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_tos_selected()
        )
        self.disk_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_disk_selected()
        )
        self.launch_path_entry.bind("<FocusOut>", self._save_launch_path)
        self.launch_path_entry.bind("<Return>", self._save_launch_path)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda _event: self._launch())
        self.root.bind("<Control-f>", lambda _event: self.search_entry.focus_set())
        self.root.bind("<Escape>", lambda _event: self._stop())

    # -- catalog/paging ------------------------------------------------

    def _poll_scan(self) -> None:
        if self._closing:
            return
        count = self.catalog.scanned_files
        ready = self.catalog.ready
        error = self.catalog.error
        if ready:
            self.scan_status_var.set(
                f"Catalog ready: {len(self.catalog)} games"
                + (f" — {error}" if error else "")
            )
        else:
            self.scan_status_var.set(
                f"Scanning… {count} entries discovered"
            )
        if count != self._scan_count:
            self._scan_count = count
            self._refresh_list()
        if ready and not self._facets_started:
            self._facets_started = True
            if error:
                self.status_log.log(f"catalog error: {error}", level="error")
            else:
                self.status_log.log(
                    f"catalog ready with {len(self.catalog)} games", level="ok"
                )
            self._refresh_facets()
            if self.online_var.get():
                self._start_online_enrichment()
        with self._enriched_lock:
            enriched = list(self._enriched_ids)
            self._enriched_ids.clear()
        if enriched:
            for entry_id in enriched:
                self.library.invalidate(entry_id)
            self.status_log.log(
                f"online enrichment updated {len(enriched)} entries", level="ok"
            )
            self._refresh_list()
            self._render_selected()
        delay = 700 if ready else 400
        self._poll_job = self.root.after(delay, self._poll_scan)

    def _poll_status(self) -> None:
        self._status_job = None
        if self._closing:
            return
        if not self._busy and not self._status_inflight:
            self._update_mcp_status()
        self._status_job = self.root.after(1500, self._poll_status)

    def _refresh_facets(self) -> None:
        if self._closing:
            return

        def success(facets: dict[str, list[str]]) -> None:
            machine_values = ["Any"] + [
                profiles.MACHINE_LABELS[machine]
                for machine in facets.get("machine", [])
                if machine in profiles.MACHINE_LABELS
            ]
            self._set_combo_values(
                self.filter_machine, self.filter_machine_var, machine_values
            )
            self._set_combo_values(
                self.filter_genre,
                self.filter_genre_var,
                ["Any"] + list(facets.get("genre", [])),
            )
            self._set_combo_values(
                self.filter_year,
                self.filter_year_var,
                ["Any"] + list(facets.get("decade", [])),
            )

        self.tasks.submit(
            self.library.facets,
            on_success=success,
            on_error=lambda exc: self.status_log.log(
                f"facets unavailable: {exc}", level="warn"
            ),
            name="facets",
        )

    @staticmethod
    def _set_combo_values(combo: ttk.Combobox, variable: tk.StringVar, values: list[str]) -> None:
        current = variable.get()
        combo.configure(values=values)
        if current not in values:
            variable.set(values[0] if values else "Any")

    def _filter_state(self) -> FilterState:
        machine_label = self.filter_machine_var.get()
        machine = (
            None
            if machine_label in {"", "Any"}
            else _FILTER_MACHINE_MAP.get(machine_label, machine_label)
        )
        return filters_from_values(
            self.search_var.get(),
            machine,
            self.filter_genre_var.get(),
            self.filter_year_var.get(),
        )

    @staticmethod
    def _disk_choice(entry: Any) -> str:
        disk = _entry_value(entry, "disk", 0) or 0
        total = _entry_value(entry, "disk_total", 0) or 0
        part = str(_entry_value(entry, "part", "") or "")
        if disk:
            prefix = f"Disk {disk} of {total}" if total else f"Disk {disk}"
        elif part:
            prefix = f"Part {part}"
        else:
            prefix = "Disk"
        return f"{prefix} — {_entry_value(entry, 'filename', '')}"

    def _genre_text(self, entry: Any) -> str:
        resolved = self.library.resolved(entry)
        if resolved.genre:
            return resolved.genre
        return self.genres.cached(
            str(_entry_value(entry, "id", "")), resolved.title
        ) or ""

    def _status_for_entries(self, entries: list[Any]) -> str:
        records = [
            self.config.get_game(str(_entry_value(entry, "id", ""))) or {}
            for entry in entries
        ]
        return launch_status(records)

    def _entry_status(self, entry: Any) -> str:
        return self._status_for_entries([entry])

    def _release_status(self, release: GameGroup) -> str:
        # Any working disk makes the release green.  A failed extra disk must
        # not turn a working release red, and a working Disk 2 must still count
        # when the boot disk has no result yet.
        statuses = {self._entry_status(entry) for entry in release.entries}
        for candidate in ("success", "regressed", "failed"):
            if candidate in statuses:
                return candidate
        return "untested"

    def _title_status(self, title: TitleGroup) -> str:
        statuses = {self._release_status(release) for release in title.releases}
        for candidate in ("success", "regressed", "failed"):
            if candidate in statuses:
                return candidate
        return "untested"

    def _add_release_row(
        self,
        parent: str,
        release: GameGroup,
        title: TitleGroup,
    ) -> str:
        primary = release.primary
        resolved = self.library.resolved(primary)
        machine, _source = self.library.machine(primary)
        iid = release.key
        self.tree.insert(
            parent,
            "end",
            iid=iid,
            text="",
            values=(
                resolved.title,
                variant_label(release),
                str(release.disk_total) if release.multi else "",
                resolved.year or resolved.year_raw or "",
                machine.upper(),
                self._genre_text(primary),
            ),
            tags=(self._release_status(release),),
        )
        self._tree_entries[iid] = primary
        self._tree_groups[iid] = release
        self._tree_titles[iid] = title
        for entry in release.entries:
            entry_id = str(_entry_value(entry, "id", ""))
            self._entry_groups[entry_id] = release
            self._entry_titles[entry_id] = title
            self._page_entries.append(entry)
            self._entry_iids[entry_id] = iid
        return iid

    def _refresh_list(self) -> None:
        state = self._filter_state()
        titles, total = self.library.page_titles(
            state,
            offset=self._page_offset,
            limit=self.page_size,
        )
        if total > 0 and self._page_offset >= total:
            self._page_offset = max(0, ((total - 1) // self.page_size) * self.page_size)
            titles, total = self.library.page_titles(
                state,
                offset=self._page_offset,
                limit=self.page_size,
            )
        selected = self._selected_id
        self.tree.delete(*self.tree.get_children())
        self._page_entries = []
        self._page_groups = []
        self._page_titles = list(titles)
        self._tree_entries = {}
        self._tree_groups = {}
        self._tree_titles = {}
        self._entry_iids = {}
        self._entry_groups = {}
        self._entry_titles = {}
        for title in titles:
            if title.multi:
                primary = title.primary
                resolved = self.library.resolved(primary)
                machine, _source = self.library.machine(primary)
                parent_iid = title.key
                self.tree.insert(
                    "",
                    "end",
                    iid=parent_iid,
                    text="",
                    values=(
                        resolved.title,
                        "",
                        "",
                        resolved.year or resolved.year_raw or "",
                        machine.upper(),
                        self._genre_text(primary),
                    ),
                    tags=(self._title_status(title),),
                )
                self._tree_entries[parent_iid] = primary
                self._tree_groups[parent_iid] = title.primary_release
                self._tree_titles[parent_iid] = title
                self.tree.item(parent_iid, open=True)
                for release in title.releases:
                    self._add_release_row(parent_iid, release, title)
            else:
                release = title.primary_release
                self._add_release_row("", release, title)
            self._page_groups.extend(title.releases)
        pages = max(1, (total + self.page_size - 1) // self.page_size)
        current_page = min(pages, self._page_offset // self.page_size + 1)
        first = self._page_offset + 1 if total else 0
        last = min(self._page_offset + len(titles), total)
        self.page_var.set(f"Page {current_page} / {pages}  ({first}-{last})")
        label = "title" if total == 1 else "titles"
        self.total_var.set(f"{total} {label}")
        self.prev_button.configure(
            state="normal" if self._page_offset > 0 else "disabled"
        )
        self.next_button.configure(
            state="normal" if self._page_offset + self.page_size < total else "disabled"
        )
        target = self._entry_iids.get(selected) if selected else None
        if target is None and titles:
            target = self._entry_iids.get(
                str(_entry_value(titles[0].primary, "id", ""))
            )
        if target and self.tree.exists(target):
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        elif not titles:
            self._selected_id = None
            self._selected_entry = None
            self._selected_group = None
            self._selected_title = None
            self._clear_details("No games match the current filters")

    def _set_page(self, offset: int) -> None:
        self._page_offset = max(0, int(offset))
        self._refresh_list()

    def _next_page(self) -> None:
        self._set_page(self._page_offset + self.page_size)

    def _previous_page(self) -> None:
        self._set_page(self._page_offset - self.page_size)

    def _on_search_changed(self, _event: Any = None) -> None:
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except tk.TclError:
                pass
        self._search_job = self.root.after(250, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        self._page_offset = 0
        self._refresh_list()
        self._refresh_facets()

    def _filters_changed(self) -> None:
        self._page_offset = 0
        self._refresh_list()

    def _clear_filters(self) -> None:
        self.search_var.set("")
        self.filter_machine_var.set("Any")
        self.filter_genre_var.set("Any")
        self.filter_year_var.set("Any")
        self._page_offset = 0
        self._refresh_list()
        self._refresh_facets()
        self.search_entry.focus_set()

    # -- details -------------------------------------------------------

    def _on_tree_select(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        iid = str(selection[0])
        entry = self._tree_entries.get(iid)
        if entry is None:
            return
        group = self._tree_groups.get(iid)
        selected = self._selected_entry
        if (
            selected is not None
            and self._selected_group is not None
            and group is not None
            and self._selected_group.key == group.key
            and self._entry_iids.get(str(_entry_value(selected, "id", ""))) == iid
        ):
            entry = selected
        self._selected_id = str(_entry_value(entry, "id", ""))
        self._selected_entry = entry
        self._selected_group = group or self._selected_group
        self._selected_title = self._tree_titles.get(iid)
        self._show_details(entry)

    def _on_disk_selected(self) -> None:
        group = self._selected_group
        if group is None:
            return
        choice = self.disk_var.get()
        for candidate in group.entries:
            if self._disk_choice(candidate) != choice:
                continue
            self._selected_id = str(_entry_value(candidate, "id", ""))
            self._selected_entry = candidate
            self._selected_title = self._entry_titles.get(self._selected_id)
            iid = self._entry_iids.get(self._selected_id)
            if iid and self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
            self._show_details(candidate)
            return

    def _render_selected(self) -> None:
        if self._selected_entry is not None:
            self._show_details(self._selected_entry)

    def _requested_machine(self) -> str | None:
        label = self.launch_machine_var.get()
        if label in {"", "Auto"}:
            return None
        return _LAUNCH_MACHINE_MAP.get(label, label)

    def _art_provider(self) -> Any:
        if not self.online_enrichment:
            return None
        provider = provider_from_config(self.config)
        if provider is not None:
            return provider if getattr(provider, "art_url_template", None) else None
        template = self.path_settings.art_url_template
        if not template:
            return None
        return HttpEnricher(
            url_template="", name="settings", art_url_template=template
        )

    def _sync_detail_wraps(self, _event: Any = None) -> None:
        width = self._details_frame.winfo_width()
        if width > 80:
            self.title_label.configure(wraplength=max(160, width - 8))
        width = self.description_frame.winfo_width()
        if width > 80:
            self.description_label.configure(wraplength=max(160, width - 8))
        width = self.controls_frame.winfo_width()
        if width > 80:
            label_width = self.file_label.winfo_reqwidth()
            self.filename_label.configure(
                wraplength=max(160, width - label_width - 24)
            )

    def _show_details(self, entry: Any) -> None:
        entry_id = str(_entry_value(entry, "id", ""))
        record = self.config.get_game(entry_id) or {}
        if self._launch_path_entry_id != entry_id:
            self.launch_path_var.set(str(record.get("launch_path") or ""))
            self._launch_path_entry_id = entry_id
        if self._tos_entry_id != entry_id:
            self._set_tos_choice(record.get("tos_override"))
            self._tos_entry_id = entry_id
        self._selected_group = self._entry_groups.get(entry_id, self._selected_group)
        self._selected_title = self._entry_titles.get(entry_id, self._selected_title)
        resolved = self.library.resolved(entry)
        requested = self._requested_machine()
        requested_tos = self._requested_tos()
        profile = profiles.choose_profile(
            entry,
            config=self.config,
            metadata=resolved,
            requested_machine=requested,
            requested_tos=requested_tos,
            tos_candidates=self._tos_candidates,
        )
        self.title_var.set(resolved.title)
        self.year_var.set(
            str(resolved.year)
            if resolved.year
            else (resolved.year_raw or "Unknown")
        )
        self.publisher_var.set(resolved.publisher or "Unknown")
        genre = resolved.genre or self.genres.cached(entry_id, resolved.title)
        self.genre_var.set(genre or "Unknown")
        if not genre and self.online_var.get():
            self._queue_genre_lookup(entry, resolved)
        self.developer_var.set(resolved.developer or "Unknown")
        machine_text = f"{profile.machine_label} [{profile.source}]"
        if profile.warnings:
            machine_text += " (fallback)"
        self.machine_var.set(machine_text)
        self.tos_var.set(f"{profile.tos_name} [{profile.tos_source}]")
        expected = profile.expected_resolution
        if expected:
            resolution_source = (
                "metadata" if getattr(resolved, "resolution", None) else "default"
            )
            self.resolution_var.set(
                f"{expected[0]}x{expected[1]} [{resolution_source}]"
            )
        else:
            self.resolution_var.set("unknown")
        self.description_var.set(
            resolved.description or "No description sidecar for this game."
        )
        self.art_canvas.show(entry, resolved, self.art_resolver, master=self.root)
        if resolved.art_path is None and self.online_var.get():
            provider = self._art_provider()
            art_url = resolved.art_url
            if not art_url and provider is not None:
                art_url = provider.art_url(entry, resolved)
            if art_url:
                self._queue_art_download(entry, art_url)
        self.filename_var.set(
            f"{_entry_value(entry, 'relative', '')}  ({_entry_value(entry, 'filename', '')})"
        )
        _set_text(self.provenance_text, resolved.provenance_summary())

        learned = self.config.get_game(str(_entry_value(entry, "id", ""))) or {}
        lines: list[str] = []
        success = learned.get("last_success_config")
        if isinstance(success, dict):
            lines.append(
                "last success: "
                + ", ".join(
                    f"{key}={success.get(key)}"
                    for key in ("machine", "profile", "tos")
                    if success.get(key) is not None
                )
            )
        failure = learned.get("last_failure_config")
        if isinstance(failure, dict):
            lines.append(
                "last failure: "
                + str(failure.get("failure_reason") or "unknown")
                + " — "
                + str(failure.get("message") or "")
            )
            if failure.get("evidence_path"):
                lines.append(f"evidence: {failure['evidence_path']}")
        if learned.get("launcher"):
            launcher = learned["launcher"]
            lines.append(
                "last launch: "
                f"{launcher.get('action')} on {launcher.get('machine')} "
                f"({launcher.get('profile_source')})"
            )
        last_result = learned.get("last_result")
        if isinstance(last_result, dict):
            status = "ok" if last_result.get("ok") else "failed"
            reason = (
                last_result.get("failure_reason")
                or last_result.get("message")
                or ""
            )
            lines.append(f"result: {status} — {reason}")
            if last_result.get("evidence_path"):
                lines.append(f"evidence path: {last_result['evidence_path']}")
        last_disk = learned.get("last_disk")
        if isinstance(last_disk, dict):
            lines.append(
                f"last disk: {last_disk.get('filename') or last_disk.get('id')} "
                f"in drive {str(last_disk.get('drive') or 'a').upper()}"
            )
        if self._last_outcome is not None and self._last_outcome.result.game_id == str(
            _entry_value(entry, "id", "")
        ):
            result = self._last_outcome.result
            lines.append(
                f"this session: {'ok' if result.ok else 'failed'} — {result.message}"
            )
            if result.failure_reason:
                lines.append(f"reason: {result.failure_reason}")
            if self._last_outcome.evidence_path:
                lines.append(f"evidence path: {self._last_outcome.evidence_path}")
        if not lines:
            lines.append(self.learned_var.get())
        _set_text(self.learned_text, "\n".join(lines))
        self._update_disk_controls()

    def _update_disk_controls(self) -> None:
        group = self._selected_group
        entries = list(group.entries) if group is not None else []
        multi = bool(group is not None and group.multi)
        values = [self._disk_choice(candidate) for candidate in entries]
        self.disk_combo.configure(
            values=values,
            state="readonly" if multi and not self._busy and not self._closing else "disabled",
        )
        if self._selected_entry is not None:
            self.disk_var.set(self._disk_choice(self._selected_entry))
        insert_state = (
            "normal"
            if (
                multi
                and self._running
                and not self._busy
                and not self._closing
                and self._running_group_key == group.key
            )
            else "disabled"
        )
        self.insert_button.configure(state=insert_state)

    def _save_launch_path(self, _event: Any = None) -> None:
        entry_id = self._launch_path_entry_id
        if not entry_id:
            return
        try:
            value = normalise_launch_path(self.launch_path_var.get())
        except ValueError as exc:
            self.status_log.log(f"invalid launch path: {exc}", level="warn")
            return
        self.launch_path_var.set(value or "")
        try:
            self.config.update_game(entry_id, {"launch_path": value})
        except OSError as exc:
            self.status_log.log(f"could not save launch path: {exc}", level="warn")

    def _requested_tos(self) -> str | None:
        label = self.tos_choice_var.get()
        if not label or label.startswith("Auto"):
            return None
        return self._tos_options.get(label)

    def _refresh_tos_values(self) -> None:
        self.tos_combo.configure(
            values=["Auto (profile)"]
            + sorted(self._tos_options, key=str.casefold)
        )

    def _set_tos_choice(self, value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            self.tos_choice_var.set("Auto (profile)")
            return
        candidate = Path(text).expanduser()
        if not paths.is_valid_tos(candidate):
            self.tos_choice_var.set("Auto (profile)")
            return
        resolved = candidate.resolve()
        for label, path in self._tos_options.items():
            if Path(path) == resolved:
                self.tos_choice_var.set(label)
                return
        label = f"{resolved.name} (custom)"
        self._tos_options[label] = str(resolved)
        self._refresh_tos_values()
        self.tos_choice_var.set(label)

    def _browse_tos(self) -> None:
        if self._busy or self._closing:
            return
        current = self._requested_tos()
        filename = filedialog.askopenfilename(
            parent=self.root,
            title="Select TOS image",
            initialdir=str(Path(current).parent) if current else str(Path.home()),
            filetypes=(("TOS images", "*.img *.rom"), ("All files", "*.*")),
        )
        if not filename:
            return
        if not paths.is_valid_tos(filename):
            self.status_log.log(
                "not a valid 192/256/512 KB TOS image", level="warn"
            )
            return
        self._set_tos_choice(filename)
        self._on_tos_selected()

    def _on_tos_selected(self) -> None:
        if self._busy or self._closing:
            return
        entry = self._selected_entry
        if entry is None:
            return
        entry_id = str(_entry_value(entry, "id", ""))
        value = self._requested_tos()
        try:
            self.config.update_game(entry_id, {"tos_override": value})
        except OSError as exc:
            self.status_log.log(f"could not save TOS choice: {exc}", level="warn")
            return
        self._tos_entry_id = entry_id
        self._show_details(entry)

    def _clear_details(self, message: str = "Select a game") -> None:
        self.title_var.set(message)
        self.launch_path_var.set("")
        self._launch_path_entry_id = None
        self.tos_choice_var.set("Auto (profile)")
        self._tos_entry_id = None
        for variable in (
            self.year_var,
            self.publisher_var,
            self.genre_var,
            self.developer_var,
            self.machine_var,
            self.tos_var,
            self.resolution_var,
            self.description_var,
            self.filename_var,
        ):
            variable.set("—")
        self.art_canvas.canvas.delete("all")
        self.disk_combo.configure(values=[], state="disabled")
        self.disk_var.set("")
        self.insert_button.configure(state="disabled")
        _set_text(self.provenance_text, "")
        _set_text(self.learned_text, "")

    # -- launch/stop ---------------------------------------------------

    def _launch(self) -> None:
        if self._busy or self._closing:
            return
        entry = self._selected_entry
        if entry is None:
            self.status_log.log("select a game first", level="warn")
            return
        group = self._selected_group
        if (
            self._running
            and group is not None
            and self._running_group_key == group.key
            and self._running_disk_id != str(_entry_value(entry, "id", ""))
        ):
            self._insert_disk()
            return
        resolved = self.library.resolved(entry)
        self._last_launch_group_key = group.key if group is not None else None
        self._busy = True
        self._set_controls()
        self.status_log.log(f"Launching {resolved.title}…")
        requested = self._requested_machine()
        requested_tos = self._requested_tos()
        launch_path = self.launch_path_var.get()

        def success(outcome: Any) -> None:
            self._busy = False
            result = outcome.result
            self._running = bool(result.ok)
            self._can_stop = bool(result.ok)
            self._last_outcome = outcome
            self._running_group_key = group.key if group is not None else None
            self._running_disk_id = result.game_id
            recorded = outcome.recorded if isinstance(outcome.recorded, dict) else {}
            saved_path = recorded.get("launch_path")
            if saved_path:
                self.launch_path_var.set(str(saved_path))
            self._set_controls()
            if result.ok:
                self.status_log.log(
                    f"launched {result.title}: {outcome.explanation}", level="ok"
                )
                if result.resolution:
                    self.status_log.log(
                        f"game frame {result.resolution[0]}x{result.resolution[1]} "
                        f"after {result.duration_ms} ms"
                    )
            else:
                self.status_log.log(
                    f"launch failed: {result.failure_reason or result.status}: "
                    f"{result.message}",
                    level="error",
                )
                if result.frame_path:
                    self.status_log.log(f"failure frame: {result.frame_path}", level="error")
            if outcome.evidence_path:
                self.status_log.log(f"evidence: {outcome.evidence_path}")
            self._refresh_list()
            self._render_selected()
            self._update_mcp_status()

        def error(exc: BaseException) -> None:
            self._busy = False
            self._set_controls()
            self.status_log.log(
                f"{type(exc).__name__}: {exc}", level="error"
            )
            self._update_mcp_status()

        self.tasks.submit(
            lambda: self.service.launch(
                entry,
                requested_machine=requested,
                requested_tos=requested_tos,
                launch_path=launch_path,
            ),
            on_success=success,
            on_error=error,
            name="launch-game",
        )

    def _stop(self) -> None:
        if self._busy or self._closing or not self._can_stop:
            return
        self._busy = True
        self._set_controls()
        self.status_log.log("Stopping Hatari…")

        def success(result: dict[str, Any]) -> None:
            self._busy = False
            self._running = False
            self._can_stop = False
            self._running_group_key = None
            self._last_launch_group_key = None
            self._running_disk_id = None
            self._set_controls()
            state = result.get("state") or result.get("error") or "stopped"
            self.status_log.log(f"stopped: {state}", level="ok")
            self._render_selected()
            self._update_mcp_status()

        def error(exc: BaseException) -> None:
            self._busy = False
            self._set_controls()
            self.status_log.log(f"stop failed: {exc}", level="error")
            self._update_mcp_status()

        self.tasks.submit(
            self.service.stop,
            on_success=success,
            on_error=error,
            name="stop-game",
        )

    def _insert_disk(self) -> None:
        if self._busy or self._closing:
            return
        entry = self._selected_entry
        group = self._selected_group
        if entry is None or group is None or not group.multi:
            return
        entry_id = str(_entry_value(entry, "id", ""))
        if not (
            self._running
            and self._can_stop
            and self._running_group_key == group.key
        ):
            self.status_log.log(
                "launch the game before inserting another disk", level="warn"
            )
            return
        if self._running_disk_id == entry_id:
            self.status_log.log(
                f"{_entry_value(entry, 'filename', 'disk')} is already inserted"
            )
            return
        filename = str(_entry_value(entry, "filename", "disk"))
        self._busy = True
        self._set_controls()
        self.status_log.log(f"Inserting {filename}…")

        def success(result: Any) -> None:
            self._busy = False
            if result.ok:
                self._running_disk_id = entry_id
                self.status_log.log(result.message or f"inserted {filename}", level="ok")
            else:
                self.status_log.log(
                    f"disk insert failed: {result.failure_reason or result.status}: "
                    f"{result.message}",
                    level="error",
                )
            self._set_controls()
            self._render_selected()
            self._update_mcp_status()

        def error(exc: BaseException) -> None:
            self._busy = False
            self._set_controls()
            self.status_log.log(
                f"disk insert failed: {type(exc).__name__}: {exc}", level="error"
            )
            self._update_mcp_status()

        self.tasks.submit(
            lambda: self.service.switch_disk(entry),
            on_success=success,
            on_error=error,
            name="insert-disk",
        )

    def _set_controls(self) -> None:
        launch_state = "disabled" if self._busy or self._closing else "normal"
        stop_state = (
            "normal" if (self._can_stop and not self._busy and not self._closing) else "disabled"
        )
        self.launch_button.configure(state=launch_state)
        self.stop_button.configure(state=stop_state)
        combo_state = "disabled" if self._busy or self._closing else "readonly"
        self.launch_machine.configure(state=combo_state)
        self.tos_combo.configure(state=combo_state)
        self.tos_browse_button.configure(state=launch_state)
        self.launch_path_entry.configure(state=launch_state)
        self._update_disk_controls()

    def _update_mcp_status(self) -> None:
        if self._closing or self._status_inflight:
            return
        self._status_inflight = True

        def finish() -> None:
            self._status_inflight = False

        def success(status: dict[str, Any]) -> None:
            state = status.get("state", "unknown")
            pid = status.get("pid")
            was_running = self._running
            self.mcp_status_var.set(
                f"MCP: {state}" + (f" (pid {pid})" if pid else "")
            )
            alive = {
                "starting",
                "running",
                "at_prompt",
                "remote_paused",
                "unresponsive",
            }
            terminal = {
                "not_started",
                "stopped",
                "stopping",
                "exited",
                "crashed",
                "failed",
            }
            if state in alive:
                self._running = True
                self._can_stop = True
                if self._running_group_key is None:
                    self._running_group_key = self._last_launch_group_key
            elif state in terminal:
                self._running = False
                self._can_stop = False
                self._running_group_key = None
                self._last_launch_group_key = None
                self._running_disk_id = None
                if was_running:
                    self.status_log.log(
                        f"Hatari is no longer running ({state})", level="warn"
                    )
            self._mcp_state = state
            finish()
            self._set_controls()

        def error(exc: BaseException) -> None:
            self.mcp_status_var.set(f"MCP: unavailable ({exc})")
            finish()

        self.tasks.submit(
            self.service.status,
            on_success=success,
            on_error=error,
            name="mcp-status",
        )

    # -- folders/online ------------------------------------------------

    def _toggle_status(self) -> None:
        self.status_visible = not self.status_visible
        if self.status_visible:
            self.paned.add(self.status_frame, minsize=280, width=320)
            self.status_button.configure(text="Hide status")
        else:
            self.paned.forget(self.status_frame)
            self.status_button.configure(text="Show status")
        self.config.ui_set("status_visible", self.status_visible)

    def _queue_art_download(self, entry: Any, url: str) -> None:
        entry_id = str(_entry_value(entry, "id", ""))
        key = (entry_id, url)
        if not entry_id or key in self._art_attempts or self._closing:
            return
        self._art_attempts.add(key)
        title = str(_entry_value(entry, "title", "game"))

        def success(path: Any) -> None:
            if path is None:
                self.status_log.log(
                    f"no usable art returned for {title}", level="warn"
                )
                return
            self.library.invalidate(entry_id)
            self.status_log.log(f"cached art for {title}", level="ok")
            self._refresh_list()
            self._render_selected()

        def error(exc: BaseException) -> None:
            self.status_log.log(
                f"art download failed for {title}: {exc}", level="warn"
            )

        self.tasks.submit(
            lambda: self.art_resolver.download(url, entry_id),
            on_success=success,
            on_error=error,
            name="art-download",
        )

    def _queue_genre_lookup(self, entry: Any, resolved: ResolvedMetadata) -> None:
        entry_id = str(_entry_value(entry, "id", ""))
        if not entry_id or entry_id in self._genre_attempts or self._closing:
            return
        self._genre_attempts.add(entry_id)
        title = str(resolved.title or _entry_value(entry, "title", "game"))

        def success(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            genre, source = result
            self.status_log.log(f"genre for {title}: {genre} ({source})")
            self._refresh_list()
            self._render_selected()

        def error(exc: BaseException) -> None:
            self.status_log.log(
                f"genre lookup failed for {title}: {exc}", level="warn"
            )

        self.tasks.submit(
            lambda: self.genres.lookup(entry, resolved),
            on_success=success,
            on_error=error,
            name="genre-lookup",
        )

    def _open_settings(self) -> None:
        if self._busy or self._closing:
            return
        SettingsDialog(self.root, self.path_settings, self._apply_path_settings)

    def _apply_path_settings(self, settings: PathSettings) -> None:
        previous_bin = self.service.hatari_bin
        self.path_settings = save_path_settings(self.config, settings)
        self.online_enrichment = bool(self.path_settings.online_enrichment)
        self.boot_turbo = bool(self.path_settings.boot_turbo)
        self.online_var.set(self.online_enrichment)
        self.boot_turbo_var.set(self.boot_turbo)
        self._art_attempts.clear()
        self._genre_attempts.clear()
        self._explicit_roots = False
        self._cli_roots = []
        self._tos_candidates = profiles.discover_tos_candidates(
            self.path_settings.tos_dirs
        )
        self._tos_options = _tos_options(self._tos_candidates)
        self._refresh_tos_values()
        current = self.tos_choice_var.get()
        if (
            current
            and not current.startswith("Auto")
            and current not in self._tos_options
        ):
            self.tos_choice_var.set("Auto (profile)")
        self._tos_entry_id = None
        self.kairost = KairoSTCatalog(self.path_settings.catalog_dirs)
        self.metadata_resolver = MetadataResolver(
            state_dir=self.state_dir,
            metadata_dir=self.state_dir / "metadata",
            cache_dir=self.state_dir / "cache" / "art",
            kairost=self.kairost,
        )
        self.library.metadata = self.metadata_resolver
        self.service.metadata = self.metadata_resolver
        self.service.set_paths(
            hatari_bin=self.path_settings.hatari_bin,
            tos_dirs=self.path_settings.tos_dirs,
            tos_candidates=self._tos_candidates,
        )
        self.service.turbo = self.boot_turbo
        if previous_bin != self.service.hatari_bin and self._running:
            self.status_log.log(
                "Hatari path change applies after the current session stops",
                level="warn",
            )
        self._reload_catalog()
        self.status_log.log(
            "settings saved; online enrichment "
            + ("enabled" if self.online_enrichment else "disabled")
            + ", turbo boot "
            + ("enabled" if self.boot_turbo else "disabled"),
            level="ok",
        )

    def _reload_catalog(self) -> None:
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self.catalog.stop()
        roots = self._catalog_roots()
        self.metadata_resolver.invalidate()
        self.catalog = Catalog(
            roots,
            state_dir=self.state_dir,
            metadata_dir=self.state_dir / "metadata",
        )
        self.library = GameLibrary(
            self.catalog, metadata=self.metadata_resolver, config=self.config
        )
        self._page_offset = 0
        self._page_groups = []
        self._page_titles = []
        self._scan_count = -1
        self._facets_started = False
        self._selected_id = None
        self._selected_entry = None
        self._selected_group = None
        self._selected_title = None
        self._tree_entries = {}
        self._tree_groups = {}
        self._tree_titles = {}
        self._entry_iids = {}
        self._entry_groups = {}
        self._entry_titles = {}
        self._enrichment_started = False
        self.status_log.log("catalog roots: " + ", ".join(str(path) for path in roots))
        self.catalog.start_scan()
        self._poll_scan()

    def _start_online_enrichment(self) -> None:
        if self._enrichment_started or self._closing:
            return
        provider = provider_from_config(self.config)
        if provider is None:
            self._enrichment_started = True
            self.status_log.log(
                "online art only; add an 'online' provider in config.json to enrich metadata"
            )
            return
        if not str(getattr(provider, "url_template", "") or "").strip():
            self._enrichment_started = True
            self.status_log.log(
                "online art only; the configured provider has no metadata URL"
            )
            return
        self._enrichment_started = True

        def enrich(entry: Any) -> dict[str, Any] | None:
            try:
                values = enrichment_values(
                    provider, entry, self.library.resolved(entry)
                )
            except BaseException:
                return None
            if values:
                with self._enriched_lock:
                    self._enriched_ids.append(str(_entry_value(entry, "id", "")))
            return values

        self.catalog.start_enrichment(enrich)
        self.status_log.log(f"online enrichment started ({provider.name})")

    # -- shutdown ------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._busy = True
        self._set_controls()
        self.status_log.log("Shutting down…")
        try:
            self.catalog.stop()
        except BaseException:
            pass

        def worker() -> None:
            try:
                self.service.close()
            finally:
                try:
                    self.catalog.stop()
                except BaseException:
                    pass

        self.tasks.submit(
            worker,
            on_success=lambda _result: self._finish_close(),
            on_error=lambda _exc: self._finish_close(),
            name="shutdown",
        )

    def _finish_close(self) -> None:
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._status_job is not None:
            try:
                self.root.after_cancel(self._status_job)
            except tk.TclError:
                pass
            self._status_job = None
        self.tasks.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def close_now(self) -> None:
        """Synchronous shutdown for tests and the headless smoke script."""
        if self._closing:
            return
        self._closing = True
        self._busy = True
        try:
            self.catalog.stop()
        except BaseException:
            pass
        try:
            self.service.close()
        finally:
            self.tasks.close()
            try:
                self.root.destroy()
            except tk.TclError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atari ST game launcher")
    parser.add_argument(
        "--games",
        action="append",
        metavar="DIR",
        help="game directory (repeatable); defaults to config/HATARI_MCP_GAMES",
    )
    parser.add_argument("--config", help="config.json path")
    parser.add_argument("--state", help="example state directory")
    parser.add_argument(
        "--page-size", type=int, default=_PAGE_SIZE_DEFAULT, help="entries per page"
    )
    parser.add_argument(
        "--machine",
        choices=list(profiles.MACHINES),
        help="initial launch-machine override (Auto by default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = (
        Path(args.state).expanduser()
        if args.state
        else (Path(args.config).expanduser().parent if args.config else None)
    )
    config_path = (
        Path(args.config).expanduser()
        if args.config
        else (state_dir / "config.json" if state_dir else None)
    )
    config = Config(config_path)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            f"cannot open a display: {exc}\n"
            "Use a desktop session or run under: xvfb-run -a",
            file=sys.stderr,
        )
        return 1
    GameLauncherApp(
        root,
        roots=args.games,
        config=config,
        state_dir=state_dir,
        page_size=args.page_size,
        default_requested_machine=args.machine,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
