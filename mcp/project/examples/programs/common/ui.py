"""Reusable Tkinter helpers for the example applications.

Nothing here creates a ``Tk`` root at import time. Widget classes are thin
compositions that build their widgets when instantiated, which keeps the module
importable in headless tests. ``TaskRunner`` is the only bridge from worker
threads back into Tk and always dispatches callbacks with ``after``.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable


class TaskRunner:
    """Run blocking work off the Tk thread and deliver results via ``after``."""

    def __init__(self, widget: Any):
        self.widget = widget
        self._closed = False
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        name: str = "example-task",
    ) -> threading.Thread:
        with self._lock:
            if self._closed:
                raise RuntimeError("task runner is closed")

        def worker() -> None:
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - delivered to the UI
                if on_error is not None:
                    self._dispatch(on_error, exc)
            else:
                if on_success is not None:
                    self._dispatch(on_success, result)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=worker, name=name, daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _dispatch(self, callback: Callable[..., None], *args: Any) -> None:
        if self._closed:
            return
        try:
            self.widget.after(0, callback, *args)
        except Exception:
            # The widget is gone (window closed); dropping the callback is the
            # only safe option and keeps the worker thread from crashing.
            return

    def close(self, *, wait: float = 0.0) -> None:
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        if wait > 0:
            for thread in threads:
                thread.join(timeout=wait)


class ShutdownHooks:
    """Register idempotent callbacks to run when the app window closes."""

    def __init__(self) -> None:
        self._hooks: list[Callable[[], Any]] = []
        self._lock = threading.Lock()
        self._ran = False

    def add(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        with self._lock:
            if self._ran:
                raise RuntimeError("shutdown hooks already ran")
            self._hooks.append(callback)
        return callback

    def bind_window(self, root: Any) -> None:
        def on_close() -> None:
            self.run()
            with contextlib.suppress(Exception):
                root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

    def run(self) -> None:
        with self._lock:
            if self._ran:
                return
            self._ran = True
            hooks = list(reversed(self._hooks))
            self._hooks.clear()
        for hook in hooks:
            try:
                hook()
            except BaseException:
                continue


class ScrollableList:
    """Listbox with a scrollbar and small paging helpers."""

    def __init__(self, parent: Any, *, height: int = 18) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.Frame(parent)
        self.listbox = tk.Listbox(self.frame, height=height, activestyle="none")
        self.scrollbar = ttk.Scrollbar(
            self.frame, orient="vertical", command=self.listbox.yview
        )
        self.listbox.configure(yscrollcommand=self.scrollbar.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self._items: list[Any] = []

    def set_items(self, items: list[Any], *, label: Callable[[Any], str] = str) -> None:
        self.listbox.delete(0, "end")
        self._items = list(items)
        for item in self._items:
            self.listbox.insert("end", label(item))

    def clear(self) -> None:
        self.set_items([])

    def selection(self) -> tuple[int, Any] | None:
        selected = self.listbox.curselection()
        if not selected:
            return None
        index = int(selected[0])
        if 0 <= index < len(self._items):
            return index, self._items[index]
        return None

    def bind_select(self, callback: Callable[[int, Any], None]) -> None:
        def on_select(_event: Any) -> None:
            current = self.selection()
            if current is not None:
                callback(*current)

        self.listbox.bind("<<ListboxSelect>>", on_select)

    def pack(self, **kwargs: Any) -> Any:
        self.frame.pack(**kwargs)
        return self.frame

    def grid(self, **kwargs: Any) -> Any:
        self.frame.grid(**kwargs)
        return self.frame


class StatusLog:
    """Read-only scrolling log pane safe to call from worker callbacks."""

    def __init__(self, parent: Any, *, height: int = 10) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.Frame(parent)
        self.text = tk.Text(self.frame, height=height, wrap="word", state="disabled")
        self.scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def log(self, message: str, *, level: str = "info") -> None:
        line = f"[{level}] {message}\n"
        try:
            self.text.configure(state="normal")
            self.text.insert("end", line)
            self.text.see("end")
            self.text.configure(state="disabled")
        except Exception:
            return

    def clear(self) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except Exception:
            return

    def tail(self, lines: int = 20) -> str:
        try:
            content = self.text.get("1.0", "end-1c")
        except Exception:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def pack(self, **kwargs: Any) -> Any:
        self.frame.pack(**kwargs)
        return self.frame

    def grid(self, **kwargs: Any) -> Any:
        self.frame.grid(**kwargs)
        return self.frame


class ImagePanel:
    """Label that retains the current ``PhotoImage`` reference."""

    def __init__(self, parent: Any, *, width: int = 320, height: int = 200) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.Frame(parent)
        self.label = ttk.Label(
            self.frame,
            anchor="center",
            justify="center",
            width=40,
            text="No artwork",
        )
        self.label.configure(compound="center")
        self.label.pack(fill="both", expand=True)
        self._photo: Any = None
        self.width = width
        self.height = height

    def show_photo(self, photo: Any) -> bool:
        if photo is None:
            return False
        self._photo = photo
        self.label.configure(image=photo, text="")
        return True

    def show_text(self, text: str) -> None:
        self._photo = None
        self.label.configure(image="", text=text)

    def show_entry(self, entry: Any, *, resolver: Any = None) -> dict[str, Any]:
        try:
            from . import art as art_module
        except ImportError:  # direct script import
            import art as art_module  # type: ignore[no-redef]

        resolver = resolver or art_module.ArtResolver()
        rendered = resolver.display(entry)
        if rendered.get("photo") is not None and self.show_photo(rendered["photo"]):
            return rendered
        placeholder = rendered.get("placeholder") or art_module.placeholder_for(entry).as_dict()
        self.show_text(
            f"{placeholder['initials']}\n{placeholder['title']}"
            + (f"\n{placeholder['year']}" if placeholder.get("year") else "")
            + (f"\n{placeholder['publisher']}" if placeholder.get("publisher") else "")
        )
        return rendered

    def pack(self, **kwargs: Any) -> Any:
        self.frame.pack(**kwargs)
        return self.frame

    def grid(self, **kwargs: Any) -> Any:
        self.frame.grid(**kwargs)
        return self.frame


def show_error(parent: Any, title: str, message: str) -> None:
    from tkinter import messagebox

    try:
        messagebox.showerror(title, message, parent=parent)
    except Exception:
        return


def show_info(parent: Any, title: str, message: str) -> None:
    from tkinter import messagebox

    try:
        messagebox.showinfo(title, message, parent=parent)
    except Exception:
        return


def ask_yes_no(parent: Any, title: str, message: str) -> bool:
    from tkinter import messagebox

    try:
        return bool(messagebox.askyesno(title, message, parent=parent))
    except Exception:
        return False
