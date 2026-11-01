"""Pins and buttons panel on the Rules tab: every board pin and HID button
at a glance.

Each cell is free, used by a rule, on right now (live from the box), or
part of the rule under the mouse. Clicking a free pin adds a Direct Map for
it; clicking a used pin or assigned button scrolls to the rule.

It never covers or squeezes the rule cards. When the tab is wide enough for
the widest card plus the panel, it docks beside the cards (`side`);
otherwise it lies across the bottom of the tab (`band`), where the full
width gives long rows and the cards keep theirs. Either way it collapses to
a one-line header.

Both grids are one canvas, and state changes repaint only the cells whose
state changed, so live frames and edits stay cheap.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import pins_for_board
from ..rule_list import pin_order, rule_buttons, rule_inputs
from .rule_editor import Glyph

if TYPE_CHECKING:
    from ..config_model import Rule
    from ..pages.configure_page import ConfigurePage

BUTTON_COUNT = 127   # B1..B127; B0 exists but the firmware never drives it
CELL_MARGIN = 6      # logical px around the widest label inside a cell
GAP = 2
PAD = 10
SIDE_COLS = 10       # decimal rows: B1-B10, B11-B20, … and D1-D10, …
CARDS_MIN_W = 900    # logical width the widest card (an encoder) needs, scrollbar included
BAND_MAX_ROWS_H = 0.35  # the band never takes more than this share of the tab's height; it scrolls past that
REFRESH_DELAY_MS = 150

USED_BG = ("#C9CEF5", "#2C2F52")   # stronger than ACCENT_SOFT so it reads apart from free in light mode
USED_FG = ("#3E4AB8", "#AEB4F2")
HOVER_FILL = t.ACCENT
HOVER_TEXT = ("#FFFFFF", "#FFFFFF")
LEGEND = (("On now", t.CELL_ON), ("Used", USED_BG), ("Free", t.CELL_OFF), ("Hovered rule", HOVER_FILL))

DEFAULT_INFO = "Hover a rule to see its pins and buttons. Click a free pin to add a rule for it."


def _rules_text(indices: list[int]) -> str:
    nums = [str(i + 1) for i in indices]
    if len(nums) == 1:
        return f"rule {nums[0]}"
    if len(nums) > 6:
        nums = nums[:6] + ["…"]
    return "rules " + ", ".join(nums)


class IOPanel(tk.Frame):
    """The pin grid and the button grid on one canvas, laid out as a narrow
    column (`side`) or as wide rows (`band`)."""

    def __init__(self, parent, on_pin_click: Callable[[str], None],
                 on_button_click: Callable[[int], None], on_toggle: Callable[[], None]):
        super().__init__(parent, highlightthickness=1, bd=0)
        self._on_pin_click = on_pin_click
        self._on_button_click = on_button_click
        self.mode: str | None = None   # set by the first set_layout, which also packs the body
        self.collapsed = False
        self._cols = SIDE_COLS
        self._pins: list[str] = []
        self._pin_usage: dict[str, list[int]] = {}
        self._button_usage: dict[int, list[int]] = {}
        self._live: dict[str, set] = {"p": set(), "b": set()}
        self._hl: dict[str, set] = {"p": set(), "b": set()}
        self._items: dict[tuple, tuple[int, int]] = {}   # (kind, name) → (rect, text)
        self._tag_key: dict[str, tuple] = {}
        self._hover_key: tuple | None = None

        self._font = t.tk_font(self, 10, mono=True)
        self._cap_font = t.tk_font(self, 11, "bold")
        self._small = t.tk_font(self, 11)
        self._cell = max(self._font.measure("127"), self._font.measure("D38"),
                         self._font.metrics("linespace")) + t.px(self, CELL_MARGIN)
        self._gap, self._pad = t.px(self, GAP), t.px(self, PAD)
        self._step = self._cell + self._gap
        self._gutter = max(self._cap_font.measure("BUTTONS"), self._small.measure("127 of 127")) + 2 * self._pad

        # Header: title, legend, and the info line in band mode; the toggle
        # collapses the panel to just this row.
        self._header = tk.Frame(self, bd=0)
        self._header.pack(side="top", fill="x", padx=(self._pad, t.px(self, 4)), pady=(t.px(self, 5), 0))
        self._title = tk.Label(self._header, text="PINS & BUTTONS", font=self._cap_font, anchor="w", cursor="hand2")
        self._title.pack(side="left")
        self._title.bind("<Button-1>", lambda _e: on_toggle())
        self._toggle = Glyph(self._header, "▾", on_toggle, t.TEXT_DIM, t.HOVER, t.resolve(t.CANVAS_BG), size=12)
        self._toggle.pack(side="right")
        self._legend = tk.Canvas(self._header, highlightthickness=0, bd=0, height=self._small.metrics("linespace"))
        self._header_info = tk.Label(self._header, anchor="w", font=self._small)

        self._info = tk.Label(self, anchor="nw", justify="left", font=self._small, height=3)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        # A small requested height: packed beside the band, the default 200
        # would make the band tall enough to need no scrollbar, which then
        # unpacks and shrinks it again, forever.
        self._scrollbar = ctk.CTkScrollbar(self, orientation="vertical", width=12, height=20,
                                           command=self.canvas.yview)
        self._yview: tuple | None = None
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._on_wheel)
        self.canvas.tag_bind("cell", "<Enter>", self._cell_enter)
        self.canvas.tag_bind("cell", "<Leave>", self._cell_leave)
        self.canvas.tag_bind("cell", "<Button-1>", self._cell_click)
        self.retheme()

    # --------------------------------------------------------------- api

    def side_width(self) -> int:
        """Physical width the side layout needs."""
        return 2 * self._pad + SIDE_COLS * self._step - self._gap + t.px(self, 14)

    def set_layout(self, mode: str, collapsed: bool, width: int):
        """`width` is the room in band mode; columns come in tens so rows
        stay B1-B10, B11-B20, …"""
        cols = SIDE_COLS
        if mode == "band":
            fit = (width - self._gutter - 2 * self._pad + self._gap) // self._step
            cols = max(SIDE_COLS, fit // 10 * 10)
        if (mode, collapsed, cols) == (self.mode, self.collapsed, self._cols):
            return
        self.mode, self.collapsed, self._cols = mode, collapsed, cols
        self._pack_body()
        self._draw()

    def set_pins(self, pins: list[str]):
        pins = sorted(dict.fromkeys(pins), key=pin_order)
        if pins != self._pins:
            self._pins = pins
            self._draw()

    def set_usage(self, pin_usage: dict[str, list[int]], button_usage: dict[int, list[int]]):
        changed = {("p", p) for p in set(pin_usage) ^ set(self._pin_usage)}
        changed |= {("b", b) for b in set(button_usage) ^ set(self._button_usage)}
        self._pin_usage, self._button_usage = pin_usage, button_usage
        self._repaint(changed)
        self._draw_summaries()
        self._show_info()

    def set_live(self, pins: set[str], buttons: set[int]):
        self._set_marks(self._live, pins, buttons)

    def set_highlight(self, pins: set[str], buttons: set[int]):
        self._set_marks(self._hl, pins, buttons)

    def retheme(self):
        # The inset canvas tone, like the Device page's button grid.
        surface = t.resolve(t.CANVAS_BG)
        self.configure(bg=surface, highlightbackground=t.resolve(t.BORDER),
                       highlightcolor=t.resolve(t.BORDER))
        for w in (self._header, self._legend, self.canvas):
            w.configure(bg=surface)
        self._title.configure(bg=surface, fg=t.resolve(t.TEXT_MUTED))
        for w in (self._info, self._header_info):
            w.configure(bg=surface, fg=t.resolve(t.TEXT_DIM))
        self._toggle.retheme()
        self._draw_legend()
        self._draw()

    # ----------------------------------------------------------- layout

    def _pack_body(self):
        for w in (self._legend, self._header_info, self._info, self.canvas, self._scrollbar):
            w.pack_forget()
        self._toggle.configure(text="▸" if self.collapsed else "▾")
        if self.collapsed:
            self._header.pack_configure(pady=t.px(self, 5))
            self._header_info.pack(side="left", fill="x", expand=True, padx=(t.px(self, 12), 0))
            return
        self._header.pack_configure(pady=(t.px(self, 5), 0))
        if self.mode == "band":
            self._legend.pack(side="left", padx=(t.px(self, 16), 0))
            self._header_info.pack(side="left", fill="x", expand=True, padx=(t.px(self, 16), 0))
            self.canvas.pack(side="left", fill="both", expand=True)
        else:
            self._info.configure(wraplength=self.side_width() - 2 * self._pad)
            self._info.pack(side="bottom", fill="x", padx=self._pad, pady=(t.px(self, 4), t.px(self, 6)))
            self.canvas.configure(width=self.side_width() - t.px(self, 14))
            self.canvas.pack(side="left", fill="y", expand=True)
        self._show_info()

    def content_height(self) -> int:
        """Physical height the band needs to show everything without scrolling."""
        region = self.canvas.cget("scrollregion")
        return int(float(region.split()[-1])) if region else 0

    # ---------------------------------------------------------- drawing

    def _set_marks(self, marks: dict[str, set], pins: set[str], buttons: set[int]):
        pins, buttons = set(pins), set(buttons)
        changed = {("p", p) for p in pins ^ marks["p"]} | {("b", b) for b in buttons ^ marks["b"]}
        if not changed:
            return
        marks["p"], marks["b"] = pins, buttons
        self._repaint(changed)

    def _pin_rows(self) -> list[list[str]]:
        """A new row per pin group (D, A, …), and every `cols` pins, so
        D1-D10 lines up with B1-B10."""
        rows: list[list[str]] = []
        group = None
        for p in self._pins:
            g = pin_order(p)[:3]
            if g != group or len(rows[-1]) == self._cols:
                rows.append([])
                group = g
            rows[-1].append(p)
        return rows

    def _draw(self):
        c = self.canvas
        c.delete("all")
        self._items.clear()
        self._tag_key.clear()
        if self.collapsed:
            self._draw_summaries()
            return
        pad = self._pad // 2
        if self.mode == "band":
            # Section labels sit in a gutter left of their rows.
            x0, y = self._gutter, pad
            y = self._section(y, "PINS", "sum_p", x0, self._pin_rows())
            y += t.px(self, 6)
            rows = [list(range(n, min(n + self._cols, BUTTON_COUNT + 1)))
                    for n in range(1, BUTTON_COUNT + 1, self._cols)]
            y = self._section(y, "BUTTONS", "sum_b", x0, rows)
            width = x0 + self._cols * self._step
        else:
            x0, y = self._pad, pad
            y = self._draw_canvas_legend(y)
            y = self._section(y, "PINS", "sum_p", x0, self._pin_rows())
            y += t.px(self, 10)
            rows = [list(range(n, min(n + SIDE_COLS, BUTTON_COUNT + 1)))
                    for n in range(1, BUTTON_COUNT + 1, SIDE_COLS)]
            y = self._section(y, "BUTTONS", "sum_b", x0, rows)
            width = self.side_width()
        c.configure(scrollregion=(0, 0, width, y + pad))
        self._draw_summaries()

    def _section(self, y: int, title: str, summary_tag: str, x0: int, rows: list[list]) -> int:
        c = self.canvas
        muted = t.resolve(t.TEXT_MUTED)
        if self.mode == "band":
            c.create_text(self._pad, y, text=title, anchor="nw", font=self._cap_font, fill=muted)
            c.create_text(self._pad, y + self._cap_font.metrics("linespace"), text="", anchor="nw",
                          font=self._small, fill=muted, tags=summary_tag)
        else:
            c.create_text(x0, y, text=title, anchor="nw", font=self._cap_font, fill=muted)
            c.create_text(self.side_width() - self._pad - t.px(self, 14), y, text="", anchor="ne",
                          font=self._small, fill=muted, tags=summary_tag)
            y += self._cap_font.metrics("linespace") + t.px(self, 4)
        if not rows:
            c.create_text(x0, y, text="No pins known for this board.", anchor="nw",
                          font=self._small, fill=muted)
            return y + self._small.metrics("linespace")
        for row in rows:
            for col, name in enumerate(row):
                key = ("p", name) if isinstance(name, str) else ("b", name)
                self._make_cell(key, str(name), x0 + col * self._step, y)
            y += self._step
        return y

    def _draw_canvas_legend(self, y: int) -> int:
        """Side mode: the legend is the canvas's first line."""
        x = self._pad
        for label, fill in LEGEND:
            x = self._swatch(self.canvas, x, y, label, fill)
        return y + self._small.metrics("linespace") + t.px(self, 8)

    def _draw_legend(self):
        """Band mode: the legend sits in the header row."""
        lg = self._legend
        lg.delete("all")
        x = 0
        for label, fill in LEGEND:
            x = self._swatch(lg, x, 0, label, fill)
        lg.configure(width=x)

    def _swatch(self, c: tk.Canvas, x: int, y: int, label: str, fill) -> int:
        sw = t.px(self, 10)
        line = self._small.metrics("linespace")
        c.create_rectangle(x, y + (line - sw) / 2, x + sw, y + (line + sw) / 2,
                           fill=t.resolve(fill), outline=t.resolve(fill))
        x += sw + t.px(self, 4)
        c.create_text(x, y, text=label, anchor="nw", font=self._small, fill=t.resolve(t.TEXT_DIM))
        return x + self._small.measure(label) + t.px(self, 10)

    def _draw_summaries(self):
        pins_used = sum(1 for p in self._pins if p in self._pin_usage)
        pins, buttons = f"{pins_used} of {len(self._pins)}", f"{len(self._button_usage)} of {BUTTON_COUNT}"
        if self.mode == "band":
            self.canvas.itemconfigure("sum_p", text=pins)
            self.canvas.itemconfigure("sum_b", text=buttons)
        else:
            self.canvas.itemconfigure("sum_p", text=f"{pins} used")
            self.canvas.itemconfigure("sum_b", text=f"{buttons} assigned")
        if self.collapsed:
            self._header_info.configure(text=f"{pins} pins used, {buttons} buttons assigned")

    def _make_cell(self, key: tuple, label: str, x: int, y: int):
        tag = f"k{len(self._tag_key)}"
        self._tag_key[tag] = key
        rect = self.canvas.create_rectangle(x, y, x + self._cell, y + self._cell, tags=("cell", tag))
        # Disabled: the label never takes the pointer, so hovering a cell is
        # one Enter/Leave pair on its rectangle.
        text = self.canvas.create_text(x + self._cell / 2, y + self._cell / 2, text=label,
                                       font=self._font, state="disabled")
        self._items[key] = (rect, text)
        self._paint(key)

    def _repaint(self, keys):
        for key in keys:
            if key in self._items:
                self._paint(key)

    def _paint(self, key: tuple):
        kind, name = key
        rect, text = self._items[key]
        live, hl = name in self._live[kind], name in self._hl[kind]
        used = name in (self._pin_usage if kind == "p" else self._button_usage)
        width = 1
        if live:
            fill, fg, outline = t.CELL_ON, t.CELL_ON_TEXT, (HOVER_FILL if hl else t.CELL_ON)
            width = 2 if hl else 1
        elif hl:
            fill, fg, outline = HOVER_FILL, HOVER_TEXT, HOVER_FILL
        elif used:
            fill, fg, outline = USED_BG, USED_FG, USED_BG
        else:
            fill, fg, outline = t.CELL_OFF, t.TEXT_DIM, t.CELL_BORDER
        self.canvas.itemconfigure(rect, fill=t.resolve(fill), outline=t.resolve(outline), width=width)
        self.canvas.itemconfigure(text, fill=t.resolve(fg))

    # ----------------------------------------------------------- events

    def _key_at_pointer(self) -> tuple | None:
        for tag in self.canvas.gettags("current"):
            if tag in self._tag_key:
                return self._tag_key[tag]
        return None

    def _cell_enter(self, _e=None):
        self._hover_key = self._key_at_pointer()
        self.canvas.configure(cursor="hand2")
        self._show_info()

    def _cell_leave(self, _e=None):
        self._hover_key = None
        self.canvas.configure(cursor="")
        self._show_info()

    def _cell_click(self, _e=None):
        key = self._key_at_pointer()
        if key is None:
            return
        kind, name = key
        if kind == "p":
            self._on_pin_click(name)
        else:
            self._on_button_click(name)

    def _show_info(self):
        if self.collapsed:
            return
        key = self._hover_key
        if key is None:
            text = DEFAULT_INFO
        elif key[0] == "p":
            pin = key[1]
            users = self._pin_usage.get(pin)
            text = (f"{pin} is read by {_rules_text(users)}. Click to show it." if users
                    else f"{pin} is free. Click to add a Direct Map rule for it.")
        else:
            n = key[1]
            users = self._button_usage.get(n)
            text = (f"Button {n} is set by {_rules_text(users)}. Click to show it." if users
                    else f"Button {n} is free.")
        label = self._header_info if self.mode == "band" else self._info
        if label.cget("text") != text:
            label.configure(text=text)

    def _on_yscroll(self, first, last):
        # CTkScrollbar.set redraws through update_idletasks, which can call
        # back in here; only act on a real change.
        if (first, last) == self._yview:
            return
        self._yview = (first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self._scrollbar.winfo_manager():
                self._scrollbar.pack_forget()
        elif not self._scrollbar.winfo_manager() and self.canvas.winfo_manager():
            self._scrollbar.pack(side="right", fill="y", before=self.canvas)
        self._scrollbar.set(first, last)

    def _on_wheel(self, event):
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 2, "units")


class RulesSidebar:
    """Places the IOPanel on the Rules tab and keeps it fed.

    Beside the cards (tab column 1) when the cards still get CARDS_MIN_W;
    otherwise across the bottom (tab row 2), sized to its content but never
    more than BAND_MAX_ROWS_H of the tab. The header's toggle collapses it to
    one line in either place."""

    def __init__(self, tab, page: ConfigurePage):
        self.tab, self.page = tab, page
        self.editor = page.rule_editor
        self.panel = IOPanel(tab, self._pin_click, self._button_click, self._toggle)
        self._collapsed = False
        self._placed: str | None = None
        self._hover_rule: Rule | None = None
        self._refresh_after: str | None = None

        self.editor.hover_listener = self._on_rule_hover
        tab.bind("<Configure>", lambda _e: self._layout(), add="+")
        page.app.register_theme_listener(self.panel.retheme)
        self.refresh_pins()
        self._layout()

    # --------------------------------------------------------------- feed

    def refresh_pins(self):
        app = self.page.app
        try:
            pins = app.device_pins or pins_for_board(app.board_map)
        except Exception:
            pins = pins_for_board("")
        self.panel.set_pins(list(pins))
        self._place_band()
        self.refresh()

    def schedule_refresh(self):
        """Debounced: typing in a rule field never waits on the panel."""
        if self._refresh_after is not None:
            self.tab.after_cancel(self._refresh_after)
        self._refresh_after = self.tab.after(REFRESH_DELAY_MS, self.refresh)

    def refresh(self):
        if self._refresh_after is not None:
            try:
                self.tab.after_cancel(self._refresh_after)
            except Exception:
                pass
            self._refresh_after = None
        model = self.editor.model
        self.panel.set_usage(model.pin_usage(), model.button_usage())
        self._on_rule_hover(self._hover_rule)  # the hovered rule may have been edited

    def set_live(self, state: dict):
        pins = {p for p, on in (state.get("p") or {}).items() if on}
        self.panel.set_live(pins, set(state.get("b") or ()))

    def clear_live(self):
        self.panel.set_live(set(), set())

    def _on_rule_hover(self, rule: Rule | None):
        self._hover_rule = rule
        if rule is None:
            self.panel.set_highlight(set(), set())
        else:
            self.panel.set_highlight(rule_inputs(rule), rule_buttons(rule))

    def _pin_click(self, pin: str):
        users = self.editor.model.pin_usage().get(pin)
        if users:
            self.editor.reveal(users[0])
        else:
            self.editor.add_rule(input_pin=pin)
            self.refresh()

    def _button_click(self, n: int):
        users = self.editor.model.button_usage().get(n)
        if users:
            self.editor.reveal(users[0])

    # ------------------------------------------------------------- layout

    def _toggle(self):
        self._collapsed = not self._collapsed
        self._placed = None
        self._layout()

    def _layout(self):
        width = self.tab.winfo_width()
        if width <= 1:
            return
        side = (not self._collapsed
                and width >= t.px(self.tab, CARDS_MIN_W) + self.panel.side_width())
        mode = "side" if side else "band"
        self.panel.set_layout(mode, self._collapsed, width - t.px(self.tab, 8))
        if mode == self._placed:
            if mode == "band":
                self._place_band()
            return
        self._placed = mode
        self.panel.grid_forget()
        if side:
            self.panel.grid(row=1, column=1, rowspan=1, sticky="ns", padx=(t.px(self.tab, 3), 0), pady=4)
        else:
            self.panel.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 4))
            self._place_band()

    def _place_band(self):
        """Band height: all rows when they fit, else a capped, scrolling view."""
        if self._placed != "band" or self._collapsed:
            self.panel.canvas.configure(height=1)
            return
        need = self.panel.content_height()
        cap = int(self.tab.winfo_height() * BAND_MAX_ROWS_H)
        h = max(t.px(self.tab, 60), min(need, cap)) if cap > 0 else need
        if int(float(self.panel.canvas.cget("height"))) != h:
            self.panel.canvas.configure(height=h)
