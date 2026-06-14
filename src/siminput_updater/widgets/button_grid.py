from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk

from .. import ui_theme as t

COLS = 16
ROWS = 8
PAD = 4
FONT_SIZE = 11
MARGIN = 14  # padding around the widest label ("128") inside a cell


class ButtonGrid(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self._active: set[int] = set()

        # Size cells to the rendered text so 3-digit numbers always fit,
        # regardless of the display's font scaling.
        self._font = tkfont.Font(family="TkDefaultFont", size=FONT_SIZE)
        text_w = self._font.measure("128")
        text_h = self._font.metrics("linespace")
        cell = max(text_w, text_h) + MARGIN

        width = COLS * (cell + PAD) + PAD
        height = ROWS * (cell + PAD) + PAD

        self.canvas = tk.Canvas(
            self, width=width, height=height,
            bg=t.resolve(t.CANVAS_BG), highlightthickness=0,
        )
        self.canvas.pack(padx=2, pady=2)

        self._cells: dict[int, int] = {}
        for row in range(ROWS):
            for col in range(COLS):
                btn_num = row * COLS + col + 1
                x = PAD + col * (cell + PAD)
                y = PAD + row * (cell + PAD)
                rect = self.canvas.create_rectangle(
                    x, y, x + cell, y + cell,
                    fill=t.resolve(t.CELL_OFF), outline=t.resolve(t.CELL_BORDER),
                )
                self.canvas.create_text(
                    x + cell / 2, y + cell / 2,
                    text=str(btn_num), fill=t.resolve(t.TEXT_DIM), font=self._font,
                    tags=f"text_{btn_num}",
                )
                self._cells[btn_num] = rect

    def _paint(self, btn: int, on: bool):
        if on:
            self.canvas.itemconfig(self._cells[btn], fill=t.resolve(t.CELL_ON), outline=t.resolve(t.CELL_ON))
            self.canvas.itemconfig(f"text_{btn}", fill=t.resolve(t.CELL_ON_TEXT))
        else:
            self.canvas.itemconfig(self._cells[btn], fill=t.resolve(t.CELL_OFF), outline=t.resolve(t.CELL_BORDER))
            self.canvas.itemconfig(f"text_{btn}", fill=t.resolve(t.TEXT_DIM))

    def retheme(self):
        """Re-resolve canvas colours after a light/dark switch."""
        self.canvas.configure(bg=t.resolve(t.CANVAS_BG))
        for btn in self._cells:
            self._paint(btn, btn in self._active)

    def update_buttons(self, active: set[int]):
        for btn in active - self._active:
            if btn in self._cells:
                self._paint(btn, True)
        for btn in self._active - active:
            if btn in self._cells:
                self._paint(btn, False)
        self._active = set(active)
