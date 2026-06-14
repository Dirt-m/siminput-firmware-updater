from __future__ import annotations

import tkinter as tk
import customtkinter as ctk

from .. import ui_theme as t

BAR_HEIGHT = 18
BAR_WIDTH = 400


class AxisBar(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        self._value = 32767

        self.canvas = tk.Canvas(
            self, height=BAR_HEIGHT, bg=t.resolve(t.CANVAS_BG), highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self._bg_rect = self.canvas.create_rectangle(
            0, 0, BAR_WIDTH, BAR_HEIGHT, fill=t.resolve(t.CELL_OFF), outline=t.resolve(t.CELL_BORDER),
        )
        self._fill_rect = self.canvas.create_rectangle(
            0, 0, self._bar_width(), BAR_HEIGHT, fill=t.resolve(t.AXIS_FILL), outline="",
        )
        self._center_line = self.canvas.create_line(
            BAR_WIDTH // 2, 0, BAR_WIDTH // 2, BAR_HEIGHT, fill=t.resolve(t.AXIS_CENTER), width=1,
        )

        self.value_label = ctk.CTkLabel(
            self, text="32767", width=52, anchor="e",
            font=t.mono(11), text_color=t.TEXT_DIM,
        )
        self.value_label.grid(row=0, column=1, sticky="e")

        self.canvas.bind("<Configure>", self._on_resize)

    def _bar_width(self) -> int:
        canvas_w = self.canvas.winfo_width() or BAR_WIDTH
        return int(canvas_w * self._value / 65535)

    def _on_resize(self, event):
        w = event.width
        self.canvas.coords(self._bg_rect, 0, 0, w, BAR_HEIGHT)
        self.canvas.coords(self._center_line, w // 2, 0, w // 2, BAR_HEIGHT)
        fill_w = int(w * self._value / 65535)
        self.canvas.coords(self._fill_rect, 0, 0, fill_w, BAR_HEIGHT)

    def retheme(self):
        """Re-resolve canvas colours after a light/dark switch."""
        self.canvas.configure(bg=t.resolve(t.CANVAS_BG))
        self.canvas.itemconfig(self._bg_rect, fill=t.resolve(t.CELL_OFF), outline=t.resolve(t.CELL_BORDER))
        self.canvas.itemconfig(self._fill_rect, fill=t.resolve(t.AXIS_FILL))
        self.canvas.itemconfig(self._center_line, fill=t.resolve(t.AXIS_CENTER))

    def set_value(self, value: int):
        self._value = max(0, min(65535, value))
        canvas_w = self.canvas.winfo_width() or BAR_WIDTH
        fill_w = int(canvas_w * self._value / 65535)
        self.canvas.coords(self._fill_rect, 0, 0, fill_w, BAR_HEIGHT)
        self.value_label.configure(text=str(self._value))
