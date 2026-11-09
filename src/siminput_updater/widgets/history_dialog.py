"""Config history: every backup of the box's config, newest first, each
loadable into the editor. Nothing is written to the box from here; the
loaded config is an unsaved edit until Save to device."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable

import customtkinter as ctk

from .. import ui_theme as t
from ..config_history import Backup, backup_dirs, list_backups


LOAD_INTRO = ("A copy of the box's config is kept before every save and every firmware "
              "update. Load one into the editor, check it, then Save to device to restore it.")


class HistoryDialog(ctk.CTkToplevel):
    def __init__(self, master, on_load: Callable[[Backup], None], *, title: str = "Config history",
                 intro: str = LOAD_INTRO, action: str = "Load",
                 extra: tuple[str, Callable[[], None]] | None = None):
        super().__init__(master)
        self._on_load = on_load
        self._action = action
        self.title(title)
        self.configure(fg_color=t.SURFACE)
        self.transient(master.winfo_toplevel())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self, text=intro,
            font=t.font(12), text_color=t.TEXT_DIM, anchor="w", justify="left", wraplength=520,
        ).grid(row=0, column=0, padx=20, pady=(18, 10), sticky="w")

        self.list = ctk.CTkScrollableFrame(self, fg_color=t.SURFACE_2, corner_radius=t.RADIUS,
                                           width=540, height=320)
        self.list.grid(row=1, column=0, padx=20, sticky="nsew")
        self.list.grid_columnconfigure(1, weight=1)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=2, column=0, padx=20, pady=(12, 18), sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        t.ghost_button(btns, "Open folder", self._open_folder, width=120).grid(row=0, column=0, sticky="w")
        if extra is not None:
            label, fn = extra
            t.ghost_button(btns, label, lambda: (self.destroy(), fn()), width=170).grid(
                row=0, column=1, padx=(0, 8), sticky="e")
        t.ghost_button(btns, "Close", self.destroy, width=90).grid(row=0, column=2, sticky="e")

        self.backups = list_backups()
        self._fill()
        self.after(10, self._place)

    def _place(self):
        self.update_idletasks()
        top = self.master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _fill(self):
        if not self.backups:
            ctk.CTkLabel(self.list, text="No backups yet. One is made the first time you save to the box.",
                         font=t.font(12), text_color=t.TEXT_MUTED).grid(row=0, column=0, padx=12, pady=16)
            return
        for i, b in enumerate(self.backups):
            when = b.when.strftime("%a %d %b %Y, %H:%M:%S")
            ctk.CTkLabel(self.list, text=when, font=t.font(12, "bold"), text_color=t.TEXT,
                         anchor="w").grid(row=i, column=0, padx=(12, 16), pady=6, sticky="w")
            detail = (f"{b.name or 'Unnamed box'}  ·  {b.rules} rule{'' if b.rules == 1 else 's'}"
                      if b.readable else "Can't be read")
            ctk.CTkLabel(self.list, text=detail, font=t.font(12),
                         text_color=t.TEXT_DIM if b.readable else t.ERROR,
                         anchor="w").grid(row=i, column=1, pady=6, sticky="w")
            btn = t.ghost_button(self.list, self._action, lambda b=b: self._load(b), width=70, height=28)
            btn.grid(row=i, column=2, padx=(8, 12), pady=6)
            if not b.readable:
                btn.configure(state="disabled")

    def _load(self, backup: Backup):
        self.destroy()
        self._on_load(backup)

    def _open_folder(self):
        folder = next((d for d in backup_dirs() if d.is_dir()), backup_dirs()[0])
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # noqa: S606 - opening a folder the app writes
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass
