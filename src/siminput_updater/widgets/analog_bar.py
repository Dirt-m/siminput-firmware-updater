"""A raw ADC sample drawn as a bar: pin name, 0–65535 fill, and the number.

Plain tk (a Canvas and two Labels) so a handful of them cost nothing in
the live panel; colours come from `t.resolve()` and are re-applied in
`retheme()` like the other canvases.
"""

from __future__ import annotations

import tkinter as tk

from .. import ui_theme as t
from ..config_model import ANALOG_MAX

BAR_H = 12
BAR_W = 160


class AnalogBar(tk.Frame):
    def __init__(self, master, pin: str, bar_width: int = BAR_W, bg=None):
        self._surface = bg or t.resolve(t.SURFACE)
        super().__init__(master, bg=self._surface)
        self.pin = pin
        self._value = 0
        self.name = tk.Label(self, text=pin, width=4, anchor="w", bg=self._surface,
                             fg=t.resolve(t.TEXT_DIM), font=t.tk_font(master, 12, mono=True))
        self.name.pack(side="left")
        self.canvas = tk.Canvas(self, width=t.px(master, bar_width), height=t.px(master, BAR_H),
                                bg=t.resolve(t.CANVAS_BG), highlightthickness=0)
        self.canvas.pack(side="left", padx=(2, 8))
        w, h = t.px(master, bar_width), t.px(master, BAR_H)
        self._bg_rect = self.canvas.create_rectangle(0, 0, w, h, fill=t.resolve(t.CELL_OFF),
                                                     outline=t.resolve(t.CELL_BORDER))
        self._fill_rect = self.canvas.create_rectangle(0, 0, 0, h, fill=t.resolve(t.ACCENT), outline="")
        self.value = tk.Label(self, text="—", width=5, anchor="e", bg=self._surface,
                              fg=t.resolve(t.TEXT), font=t.tk_font(master, 12, mono=True))
        self.value.pack(side="left")

    def set_value(self, raw: int):
        self._value = max(0, min(ANALOG_MAX, int(raw)))
        w = int(self.canvas.cget("width"))
        h = int(self.canvas.cget("height"))
        self.canvas.coords(self._fill_rect, 0, 0, int(w * self._value / ANALOG_MAX), h)
        self.value.configure(text=str(self._value))

    def retheme(self, bg=None):
        self._surface = bg or t.resolve(t.SURFACE)
        self.configure(bg=self._surface)
        self.name.configure(bg=self._surface, fg=t.resolve(t.TEXT_DIM))
        self.value.configure(bg=self._surface, fg=t.resolve(t.TEXT))
        self.canvas.configure(bg=t.resolve(t.CANVAS_BG))
        self.canvas.itemconfig(self._bg_rect, fill=t.resolve(t.CELL_OFF), outline=t.resolve(t.CELL_BORDER))
        self.canvas.itemconfig(self._fill_rect, fill=t.resolve(t.ACCENT))
