"""Calibration window for an ANALOG rule.

Shows the live raw sample of one analog pin from the shared `LiveMonitor`
(it subscribes like a page; it never opens a second stream), tracks the
lowest and highest readings while the user moves the sensor through its
travel, and can capture the rest position as `center` with a deadzone
suggested from the wobble seen while resting. Apply hands the numbers back
to the rule card; nothing is written to the device here.

The firmware only samples pins the *saved* config claims as analog, so a
freshly added rule has no data until the config is saved. The window says
so instead of sitting on a dash forever.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import ANALOG_MAX

REST_SAMPLE_MS = 1500      # how long "Capture center" listens for wobble
NOISE_MARGIN = 64          # added to the observed wobble (the ADC noise floor)
BAR_H = 22


def suggest_deadzone(low: int, high: int) -> int:
    """Deadzone that swallows the wobble seen at rest, with the ADC noise
    floor on top, rounded up to a tidy multiple of 16."""
    span = max(0, high - low) + NOISE_MARGIN
    return ((span + 15) // 16) * 16


class CalibrateDialog(ctk.CTkToplevel):
    def __init__(self, master, app, pin: str, current: dict,
                 on_apply: Callable[[dict], None]):
        super().__init__(master)
        self.app = app
        self.pin = pin
        self._on_apply = on_apply
        self._raw: int | None = None
        self._seen_low: int | None = None
        self._seen_high: int | None = None
        self._center: int | None = current.get("center")
        self._deadzone: int | None = current.get("deadzone")
        self._rest: list[int] | None = None
        self._rest_after: str | None = None

        self.title(f"Calibrate {pin}")
        self.resizable(False, False)
        self.configure(fg_color=t.SURFACE)
        try:
            self.transient(master.winfo_toplevel())
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, padx=24, pady=(20, 16), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(body, text=f"{pin}  ·  raw sample", font=t.font(11, "bold"),
                     text_color=t.TEXT_MUTED, anchor="w").grid(row=0, column=0, sticky="w")
        self._value = ctk.CTkLabel(body, text="—", font=t.mono(30, "bold"), text_color=t.TEXT, anchor="w")
        self._value.grid(row=1, column=0, sticky="w", pady=(0, 6))

        self._bar = tk.Canvas(body, height=BAR_H, width=420, highlightthickness=0,
                              bg=t.resolve(t.CANVAS_BG))
        self._bar.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self._bar_bg = self._bar.create_rectangle(0, 0, 420, BAR_H, fill=t.resolve(t.CELL_OFF),
                                                  outline=t.resolve(t.CELL_BORDER))
        self._bar_range = self._bar.create_rectangle(0, 0, 0, BAR_H, fill=t.resolve(t.ACCENT_SOFT), outline="")
        self._bar_center = self._bar.create_line(0, 0, 0, BAR_H, fill=t.resolve(t.WARN), width=2, state="hidden")
        self._bar_now = self._bar.create_line(0, 0, 0, BAR_H, fill=t.resolve(t.ACCENT), width=3)

        self._status = ctk.CTkLabel(body, text="", font=t.font(12), text_color=t.TEXT_DIM,
                                    anchor="w", justify="left", wraplength=420)
        self._status.grid(row=3, column=0, sticky="w", pady=(0, 12))

        # -- range --
        rng = ctk.CTkFrame(body, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
        rng.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        rng.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(rng, text="Travel", font=t.font(12, "bold"), text_color=t.TEXT, anchor="w").grid(
            row=0, column=0, padx=12, pady=(10, 0), sticky="w")
        ctk.CTkLabel(rng, text="Move the sensor through its full travel. The lowest and highest "
                               "readings become min and max.",
                     font=t.font(12), text_color=t.TEXT_DIM, anchor="w", justify="left",
                     wraplength=380).grid(row=1, column=0, columnspan=2, padx=12, pady=(2, 6), sticky="w")
        self._range_lbl = ctk.CTkLabel(rng, text="min —   max —", font=t.mono(12), text_color=t.TEXT, anchor="w")
        self._range_lbl.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="w")
        t.ghost_button(rng, "Reset travel", self._reset_range, width=110, height=28).grid(
            row=2, column=1, padx=12, pady=(0, 10), sticky="e")

        # -- center --
        ctr = ctk.CTkFrame(body, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
        ctr.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        ctr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ctr, text="Center (optional)", font=t.font(12, "bold"), text_color=t.TEXT, anchor="w").grid(
            row=0, column=0, padx=12, pady=(10, 0), sticky="w")
        ctk.CTkLabel(ctr, text="For a sensor that rests in the middle (steering, a joystick axis): "
                               "let go of it, then capture. It listens for a moment and suggests "
                               "a deadzone from the wobble.",
                     font=t.font(12), text_color=t.TEXT_DIM, anchor="w", justify="left",
                     wraplength=380).grid(row=1, column=0, columnspan=2, padx=12, pady=(2, 6), sticky="w")
        self._center_lbl = ctk.CTkLabel(ctr, text="", font=t.mono(12), text_color=t.TEXT, anchor="w")
        self._center_lbl.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="w")
        btns = ctk.CTkFrame(ctr, fg_color="transparent")
        btns.grid(row=2, column=1, padx=12, pady=(0, 10), sticky="e")
        self._capture_btn = t.ghost_button(btns, "Capture center", self._capture_center, width=130, height=28)
        self._capture_btn.grid(row=0, column=0, padx=(0, 6))
        t.ghost_button(btns, "Clear", self._clear_center, width=64, height=28).grid(row=0, column=1)

        # -- actions --
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.grid(row=6, column=0, sticky="e")
        t.ghost_button(actions, "Cancel", self._close, width=90).grid(row=0, column=0, padx=(0, 8))
        self._apply_btn = t.primary_button(actions, "Apply", self._apply, width=110)
        self._apply_btn.grid(row=0, column=1)

        self._render_center()
        self._render_range()
        self._set_status("Waiting for data from the box…")
        self.app.monitor.subscribe(self._on_state)
        self.after(50, self._place)

    # ------------------------------------------------------------ plumbing

    def _place(self):
        try:
            root = self.master.winfo_toplevel()
            self.update_idletasks()
            x = root.winfo_rootx() + (root.winfo_width() - self.winfo_width()) // 2
            y = root.winfo_rooty() + (root.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _close(self):
        try:
            self.app.monitor.unsubscribe(self._on_state)
        except Exception:
            pass
        if self._rest_after is not None:
            try:
                self.after_cancel(self._rest_after)
            except Exception:
                pass
        self.destroy()

    def _set_status(self, text: str):
        self._status.configure(text=text)

    # ------------------------------------------------------------- samples

    def _on_state(self, state: dict):
        raw = (state.get("an") or {}).get(self.pin)
        if raw is None:
            if self._raw is None:
                self._set_status(
                    f"The box is not sampling {self.pin} yet. It only reads pins the saved config "
                    "claims as analog: save the config, then calibrate.")
            return
        self.feed(int(raw))

    def feed(self, raw: int):
        """One raw sample (also the test hook)."""
        first = self._raw is None
        self._raw = raw
        if first:
            self._set_status("Live.")
        if self._seen_low is None or raw < self._seen_low:
            self._seen_low = raw
        if self._seen_high is None or raw > self._seen_high:
            self._seen_high = raw
        if self._rest is not None:
            self._rest.append(raw)
        self._value.configure(text=str(raw))
        self._render_range()
        self._render_bar()

    # -------------------------------------------------------------- range

    def _reset_range(self):
        self._seen_low = self._seen_high = self._raw
        self._render_range()
        self._render_bar()

    def _render_range(self):
        lo = "—" if self._seen_low is None else str(self._seen_low)
        hi = "—" if self._seen_high is None else str(self._seen_high)
        self._range_lbl.configure(text=f"min {lo}   max {hi}")

    # ------------------------------------------------------------- center

    def _capture_center(self):
        if self._raw is None:
            return
        self._rest = [self._raw]
        self._capture_btn.configure(state="disabled", text="Listening…")
        self._rest_after = self.after(REST_SAMPLE_MS, self._finish_capture)

    def _finish_capture(self):
        self._rest_after = None
        samples = self._rest or []
        self._rest = None
        self._capture_btn.configure(state="normal", text="Capture center")
        if not samples:
            return
        self._center = int(sum(samples) / len(samples))
        self._deadzone = suggest_deadzone(min(samples), max(samples))
        self._render_center()
        self._render_bar()

    def _clear_center(self):
        self._center = None
        self._deadzone = None
        self._render_center()
        self._render_bar()

    def _render_center(self):
        if self._center is None:
            self._center_lbl.configure(text="no center")
        else:
            dz = "" if self._deadzone is None else f"   deadzone {self._deadzone}"
            self._center_lbl.configure(text=f"center {self._center}{dz}")

    # ---------------------------------------------------------------- bar

    def _render_bar(self):
        w = max(1, self._bar.winfo_width())
        def x(v):
            return int(w * max(0, min(ANALOG_MAX, v)) / ANALOG_MAX)
        self._bar.coords(self._bar_bg, 0, 0, w, BAR_H)
        if self._seen_low is not None and self._seen_high is not None:
            self._bar.coords(self._bar_range, x(self._seen_low), 0, x(self._seen_high), BAR_H)
        if self._center is not None:
            cx = x(self._center)
            self._bar.coords(self._bar_center, cx, 0, cx, BAR_H)
            self._bar.itemconfigure(self._bar_center, state="normal")
        else:
            self._bar.itemconfigure(self._bar_center, state="hidden")
        if self._raw is not None:
            nx = x(self._raw)
            self._bar.coords(self._bar_now, nx, 0, nx, BAR_H)

    # -------------------------------------------------------------- apply

    def result(self) -> dict:
        return {
            "min": self._seen_low,
            "max": self._seen_high,
            "center": self._center,
            "deadzone": self._deadzone if self._center is not None else None,
        }

    def _apply(self):
        if self._seen_low is None or self._seen_high is None or self._seen_high <= self._seen_low:
            self._set_status("Move the sensor through its travel first: min and max are still the same.")
            return
        self._on_apply(self.result())
        self._close()
