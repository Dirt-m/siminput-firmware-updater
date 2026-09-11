"""Compact live-input strip for the Configure page.

Shows which physical pins and HID buttons are pressed right now, so a rule's
Input can be checked against the box without leaving the editor. It is a
plain subscriber: the page feeds it monitor state via `update()`.

Chips are plain tk labels rebuilt only when the pressed set changes; a
quiet box costs nothing.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import _pin_sort_key

MAX_CHIPS = 24


class LivePanel(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        self.grid_columnconfigure(1, weight=1)
        self._connected = False
        self._collapsed = False
        self._last: tuple[tuple[str, ...], tuple[int, ...]] = ((), ())

        self._toggle = ctk.CTkButton(
            self, text="", width=28, height=24, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.HOVER, text_color=t.TEXT_DIM,
            font=t.font(12), command=self._on_toggle)
        self._toggle.grid(row=0, column=0, padx=(10, 2), pady=(8, 6))
        ctk.CTkLabel(self, text="LIVE INPUTS", font=t.font(11, "bold"), text_color=t.TEXT_MUTED,
                     anchor="w").grid(row=0, column=1, sticky="w", pady=(8, 6))
        self._status = ctk.CTkLabel(self, text="", font=t.font(12), text_color=t.TEXT_MUTED, anchor="e")
        self._status.grid(row=0, column=2, sticky="e", padx=14, pady=(8, 6))

        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 10))
        self._body.grid_columnconfigure(1, weight=1)
        self._rows: dict[str, tk.Frame] = {}
        for i, (key, caption) in enumerate((("p", "Pins"), ("b", "Buttons"))):
            ctk.CTkLabel(self._body, text=caption, width=58, anchor="w", font=t.font(12),
                         text_color=t.TEXT_DIM).grid(row=i, column=0, sticky="w", pady=2)
            row = tk.Frame(self._body, bg=t.resolve(t.SURFACE))
            row.grid(row=i, column=1, sticky="w", pady=2)
            self._rows[key] = row
        self._sync_toggle()
        self.set_connected(False)

    # ------------------------------------------------------------------ api

    def set_connected(self, connected: bool):
        self._connected = connected
        if not connected:
            self._last = ((), ())
            self._render(("Connect a device to see its pins and buttons here.",), (), muted=True)
            self._status.configure(text="")
        else:
            self._status.configure(text="")
            self._render((), ())

    def update(self, state: dict):
        pins = tuple(sorted((p for p, on in (state.get("p") or {}).items() if on), key=_pin_sort_key))
        buttons = tuple(sorted(state.get("b") or ()))
        if (pins, buttons) == self._last:
            return
        self._last = (pins, buttons)
        self._render(pins, buttons)

    def retheme(self):
        bg = t.resolve(t.SURFACE)
        for row in self._rows.values():
            row.configure(bg=bg)
        pins, buttons = self._last
        if self._connected:
            self._render(pins, buttons)
        else:
            self.set_connected(False)

    # ------------------------------------------------------------- internals

    def _on_toggle(self):
        self._collapsed = not self._collapsed
        self._sync_toggle()

    def _sync_toggle(self):
        self._toggle.configure(text="▸" if self._collapsed else "▾")
        if self._collapsed:
            self._body.grid_remove()
        else:
            self._body.grid()
        self._update_status()

    def _update_status(self):
        pins, buttons = self._last
        if not self._connected:
            return
        if self._collapsed:
            parts = list(pins[:8]) + [f"B{b}" for b in buttons[:8]]
            self._status.configure(text="  ".join(parts) if parts else "nothing pressed")
        else:
            self._status.configure(text="")

    def _render(self, pins, buttons, muted: bool = False):
        self._fill(self._rows["p"], pins, t.ACCENT, t.ACCENT_SOFT, muted, "none pressed")
        self._fill(self._rows["b"], [f"B{b}" for b in buttons], t.SUCCESS, t.SUCCESS_SOFT,
                   muted, "" if muted else "none pressed")
        self._update_status()

    def _fill(self, row: tk.Frame, names, fg, bg, muted: bool, empty_text: str):
        for w in row.winfo_children():
            w.destroy()
        surface = t.resolve(t.SURFACE)
        font = t.tk_font(row, 12, mono=not muted)
        if muted or not names:
            text = names[0] if (muted and names) else empty_text
            if text:
                tk.Label(row, text=text, bg=surface, fg=t.resolve(t.TEXT_MUTED),
                         font=t.tk_font(row, 12)).pack(side="left")
            return
        shown = list(names)[:MAX_CHIPS]
        for name in shown:
            tk.Label(row, text=name, bg=t.resolve(bg), fg=t.resolve(fg), font=font,
                     padx=7, pady=1).pack(side="left", padx=(0, 5))
        if len(names) > MAX_CHIPS:
            tk.Label(row, text=f"+{len(names) - MAX_CHIPS}", bg=surface, fg=t.resolve(t.TEXT_MUTED),
                     font=font).pack(side="left")
