"""Full-window blocking overlay shown while a device operation runs.

Covers the entire window (scrim + centred card) so nothing underneath can be
clicked. Shows a live status line, progress bar, a scrolling feedback log, and
an Abort button. Every long operation (read config, write config, firmware
update) routes through here.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .. import ui_theme as t


class BusyOverlay(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color=t.BG, corner_radius=t.RADIUS)
        self._on_cancel: Callable[[], None] | None = None
        self._spinner_running = False

        # Centred card
        self.card = ctk.CTkFrame(
            self, fg_color=t.SURFACE_2, border_width=1,
            border_color=t.BORDER, corner_radius=t.RADIUS,
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        self.card.grid_columnconfigure(0, weight=1)

        self.title_lbl = ctk.CTkLabel(
            self.card, text="", anchor="w",
            font=t.font(18, "bold"), text_color=t.TEXT,
        )
        self.title_lbl.grid(row=0, column=0, padx=28, pady=(26, 2), sticky="ew")

        self.status_lbl = ctk.CTkLabel(
            self.card, text="", anchor="w",
            font=t.font(13), text_color=t.TEXT_DIM,
        )
        self.status_lbl.grid(row=1, column=0, padx=28, pady=(0, 16), sticky="ew")

        self.progress = ctk.CTkProgressBar(
            self.card, width=460, height=8, corner_radius=t.RADIUS,
            progress_color=t.ACCENT, fg_color=t.SURFACE_3,
        )
        self.progress.grid(row=2, column=0, padx=28, pady=(0, 18), sticky="ew")

        self.log = ctk.CTkTextbox(
            self.card, width=460, height=190,
            fg_color=t.CANVAS_BG, text_color=t.TEXT_DIM,
            border_width=1, border_color=t.BORDER, corner_radius=t.RADIUS,
            font=t.mono(11), wrap="none", state="disabled",
        )
        self.log.grid(row=3, column=0, padx=28, pady=(0, 18), sticky="nsew")

        self.action_btn = ctk.CTkButton(
            self.card, text="Abort", width=120, height=34,
            command=self._cancel_clicked,
            fg_color="transparent", hover_color=t.ERROR_SOFT,
            text_color=t.ERROR, border_width=1, border_color=t.ERROR,
            corner_radius=t.RADIUS, font=t.font(13, "bold"),
        )
        self.action_btn.grid(row=4, column=0, padx=28, pady=(0, 26), sticky="e")

    # -- lifecycle --

    def show(self, title: str, on_cancel: Callable[[], None], indeterminate: bool = True):
        self._on_cancel = on_cancel
        self.title_lbl.configure(text=title, text_color=t.TEXT)
        self.status_lbl.configure(text="Starting…", text_color=t.TEXT_DIM)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.action_btn.configure(
            text="Abort", state="normal",
            fg_color="transparent", hover_color=t.ERROR_SOFT,
            text_color=t.ERROR, border_color=t.ERROR, command=self._cancel_clicked,
        )
        self.progress.configure(progress_color=t.ACCENT)
        if indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self._spinner_running = True
        else:
            self._stop_spinner()
            self.progress.set(0)

        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()

    def hide(self):
        self._stop_spinner()
        self.place_forget()

    # -- live updates (call on main thread) --

    def set_status(self, text: str):
        self.status_lbl.configure(text=text)

    def set_progress(self, frac: float):
        if self._spinner_running:
            self._stop_spinner()
        self.progress.set(max(0.0, min(1.0, frac)))

    def append_log(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- terminal states --

    def finish_success(self, message: str = "Done", auto_hide_ms: int = 650):
        self._stop_spinner()
        self.progress.configure(progress_color=t.SUCCESS)
        self.progress.set(1.0)
        self.status_lbl.configure(text=message, text_color=t.SUCCESS)
        self.action_btn.configure(state="disabled")
        self.after(auto_hide_ms, self.hide)

    def finish_error(self, message: str):
        self._stop_spinner()
        self.progress.configure(progress_color=t.ERROR)
        self.status_lbl.configure(text=message, text_color=t.ERROR)
        self.append_log("ERROR: " + message)
        self._show_close()

    def finish_cancelled(self, message: str = "Aborted"):
        self._stop_spinner()
        self.progress.configure(progress_color=t.WARN)
        self.status_lbl.configure(text=message, text_color=t.WARN)
        self._show_close()

    # -- internals --

    def _show_close(self):
        self.action_btn.configure(
            text="Close", state="normal",
            fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
            text_color="#FFFFFF", border_width=0, command=self.hide,
        )

    def _cancel_clicked(self):
        self.action_btn.configure(text="Aborting…", state="disabled")
        self.status_lbl.configure(text="Aborting…", text_color=t.WARN)
        if self._on_cancel:
            self._on_cancel()

    def _stop_spinner(self):
        if self._spinner_running:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._spinner_running = False
