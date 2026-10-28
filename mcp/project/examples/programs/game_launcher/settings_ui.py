"""Settings dialog for configurable launcher paths."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .settings import PathSettings, load_defaults


class PathListEditor(ttk.Frame):
    """A small list of directories with Add and Remove buttons."""

    def __init__(
        self,
        parent: tk.Misc,
        values: list[str],
        *,
        title: str,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=title).grid(row=0, column=0, columnspan=3, sticky="w")
        self.listbox = tk.Listbox(self, height=4, selectmode="extended")
        self.listbox.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(2, 4))
        self.columnconfigure(0, weight=1)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=1, column=3, sticky="ns", pady=(2, 4))
        self.listbox.configure(yscrollcommand=scrollbar.set)
        ttk.Button(self, text="Add folder…", command=self.add_folder).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Button(self, text="Remove", command=self.remove_selected).grid(
            row=2, column=1, sticky="w", padx=(6, 0)
        )
        self.set_values(values)

    def set_values(self, values: list[str]) -> None:
        self.listbox.delete(0, "end")
        for value in values:
            self.listbox.insert("end", value)

    def values(self) -> list[str]:
        return [str(item) for item in self.listbox.get(0, "end")]

    def add_folder(self) -> None:
        directory = filedialog.askdirectory(parent=self, title="Select a folder")
        if not directory:
            return
        if directory not in self.values():
            self.listbox.insert("end", directory)

    def remove_selected(self) -> None:
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)


class SettingsDialog(tk.Toplevel):
    """Modal-ish editor for Hatari, TOS, game and catalog paths."""

    def __init__(
        self,
        parent: tk.Misc,
        settings: PathSettings,
        on_save: Callable[[PathSettings], None],
    ) -> None:
        super().__init__(parent)
        self._on_save = on_save
        self._settings = settings.normalised()
        self.title("Launcher settings")
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("820x820")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.columnconfigure(0, weight=1)

        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Hatari executable").grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        self.hatari_var = tk.StringVar(value=self._settings.hatari_bin)
        entry = ttk.Entry(frame, textvariable=self.hatari_var)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=(0, 2))
        ttk.Button(frame, text="Browse…", command=self._browse_hatari).grid(
            row=0, column=2, sticky="w", pady=(0, 2)
        )

        self.tos_editor = PathListEditor(
            frame, self._settings.tos_dirs, title="TOS directories"
        )
        self.tos_editor.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.game_editor = PathListEditor(
            frame, self._settings.game_dirs, title="Disk image directories"
        )
        self.game_editor.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.catalog_editor = PathListEditor(
            frame, self._settings.catalog_dirs, title="Catalog lookup directories"
        )
        self.catalog_editor.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0)
        )

        options = ttk.LabelFrame(frame, text="Launcher options", padding=8)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.online_var = tk.BooleanVar(value=self._settings.online_enrichment)
        ttk.Checkbutton(
            options,
            text="Online enrichment (box art and metadata)",
            variable=self.online_var,
        ).grid(row=0, column=0, sticky="w")
        self.turbo_var = tk.BooleanVar(value=self._settings.boot_turbo)
        ttk.Checkbutton(
            options,
            text="Turbo boot (32 MHz while booting, then 8 MHz)",
            variable=self.turbo_var,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        urls = ttk.LabelFrame(frame, text="Online URLs", padding=8)
        urls.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        urls.columnconfigure(1, weight=1)
        ttk.Label(urls, text="Metadata JSON URL").grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        self.enrichment_var = tk.StringVar(value=self._settings.enrichment_url)
        ttk.Entry(urls, textvariable=self.enrichment_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 2)
        )
        ttk.Label(urls, text="Box-art URL template").grid(
            row=1, column=0, sticky="w", pady=(0, 2)
        )
        self.art_template_var = tk.StringVar(
            value=self._settings.art_url_template
        )
        ttk.Entry(urls, textvariable=self.art_template_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 2)
        )
        self._url_hint = ttk.Label(
            urls,
            text=(
                "Templates support {id}, {title}, {filename} and {stem}. "
                "Leave the metadata URL blank to use box art only; leave the "
                "art template blank to disable web art."
            ),
            wraplength=300,
            justify="left",
        )
        self._url_hint.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )

        self._defaults_hint = ttk.Label(
            frame,
            text=(
                "Defaults are read from defaults.json. Folder lists accept "
                "multiple entries; Hatari uses the binary configured here."
            ),
            wraplength=300,
            justify="left",
        )
        self._defaults_hint.grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(10, 6)
        )
        self._url_frame = urls
        self._content_frame = frame
        urls.bind("<Configure>", self._sync_wraplengths)
        frame.bind("<Configure>", self._sync_wraplengths)
        self.after_idle(self._sync_wraplengths)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(
            buttons, text="Restore defaults", command=self._restore_defaults
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(
            row=0, column=1, sticky="e", padx=(0, 6)
        )
        ttk.Button(buttons, text="Save", command=self._save).grid(
            row=0, column=2, sticky="e"
        )

        self.grab_set()
        self.focus_set()

    def _sync_wraplengths(self, _event: Any = None) -> None:
        url_width = self._url_frame.winfo_width() - 28
        if url_width > 200:
            self._url_hint.configure(wraplength=url_width)
        content_width = self._content_frame.winfo_width() - 28
        if content_width > 200:
            self._defaults_hint.configure(wraplength=content_width)

    def _browse_hatari(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self,
            title="Select the Hatari executable",
            initialdir=str(Path(self.hatari_var.get()).parent)
            if self.hatari_var.get()
            else str(Path.home()),
        )
        if filename:
            self.hatari_var.set(filename)

    def _restore_defaults(self) -> None:
        default = load_defaults()
        self.hatari_var.set(default.hatari_bin)
        self.tos_editor.set_values(default.tos_dirs)
        self.game_editor.set_values(default.game_dirs)
        self.catalog_editor.set_values(default.catalog_dirs)
        self.boot_turbo_var.set(default.boot_turbo)
        self.online_var.set(default.online_enrichment)
        self.enrichment_var.set(default.enrichment_url)
        self.art_template_var.set(default.art_url_template)

    def _save(self) -> None:
        settings = PathSettings(
            hatari_bin=self.hatari_var.get(),
            tos_dirs=self.tos_editor.values(),
            game_dirs=self.game_editor.values(),
            catalog_dirs=self.catalog_editor.values(),
            boot_turbo=bool(self.turbo_var.get()),
            online_enrichment=bool(self.online_var.get()),
            enrichment_url=self.enrichment_var.get(),
            art_url_template=self.art_template_var.get(),
        ).normalised()
        binary = Path(settings.hatari_bin) if settings.hatari_bin else None
        if binary is not None:
            if not binary.is_file():
                messagebox.showerror(
                    "Invalid Hatari path",
                    f"The Hatari executable does not exist:\n{binary}",
                    parent=self,
                )
                return
            if not os.access(binary, os.X_OK):
                messagebox.showerror(
                    "Invalid Hatari path",
                    f"The Hatari executable is not executable:\n{binary}",
                    parent=self,
                )
                return
        missing = [
            item
            for item in settings.tos_dirs
            + settings.game_dirs
            + settings.catalog_dirs
            if not Path(item).is_dir()
        ]
        if missing:
            messagebox.showwarning(
                "Some folders do not exist",
                "These paths will be kept, but nothing can be found there yet:\n\n"
                + "\n".join(missing[:8]),
                parent=self,
            )
        try:
            self._on_save(settings)
        except Exception as exc:  # keep the dialog open on a failed apply
            messagebox.showerror("Could not save settings", str(exc), parent=self)
            return
        self.destroy()
