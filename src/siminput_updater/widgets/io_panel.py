"""Side panel on the Rules tab: every board pin and HID button at a glance.

Each cell is free, used by a rule, on right now (live from the box), or
part of the rule under the mouse. Clicking a free pin adds a Direct Map for
it; clicking a used pin or assigned button scrolls to the rule.

Both grids are one canvas, and state changes repaint only the cells whose
state changed, so live frames and edits stay cheap. `RulesSidebar` owns the
docking: beside the cards when the tab is wide enough, otherwise a thin
strip that opens the panel as an overlay.
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

COLS = 10            # decimal rows: B1-B10, B11-B20, … and D1-D10, …
BUTTON_COUNT = 127   # B1..B127; B0 exists but the firmware never drives it
CELL_MARGIN = 10     # logical px around the widest label inside a cell
GAP = 3
PAD = 12
CARDS_MIN_W = 640    # logical width the rule cards need before the panel docks
REFRESH_DELAY_MS = 150
STRIP_W = 26

# Light mode needs a stronger tint than ACCENT_SOFT to read apart from free cells.
USED_BG = ("#C9CEF5", "#2C2F52")
USED_FG = ("#3E4AB8", "#AEB4F2")
HOVER_FILL = t.ACCENT
HOVER_TEXT = ("#FFFFFF", "#FFFFFF")

DEFAULT_INFO = "Hover a rule to see its pins and buttons here. Click a free pin to add a rule for it."


def _rules_text(indices: list[int]) -> str:
    nums = [str(i + 1) for i in indices]
    if len(nums) == 1:
        return f"rule {nums[0]}"
    if len(nums) > 6:
        nums = nums[:6] + ["…"]
    return "rules " + ", ".join(nums)


class IOPanel(tk.Frame):
    """The pin grid and the button grid, drawn on one scrollable canvas."""

    def __init__(self, parent, on_pin_click: Callable[[str], None],
                 on_button_click: Callable[[int], None], on_collapse: Callable[[], None]):
        super().__init__(parent, highlightthickness=1, bd=0)
        self._on_pin_click = on_pin_click
        self._on_button_click = on_button_click
        self._pins: list[str] = []
        self._pin_usage: dict[str, list[int]] = {}
        self._button_usage: dict[int, list[int]] = {}
        self._live: dict[str, set] = {"p": set(), "b": set()}
        self._hl: dict[str, set] = {"p": set(), "b": set()}
        self._items: dict[tuple, tuple[int, int]] = {}   # (kind, name) → (rect, text)
        self._tag_key: dict[str, tuple] = {}
        self._hover_key: tuple | None = None

        self._font = t.tk_font(self, 11, mono=True)
        self._cap_font = t.tk_font(self, 11, "bold")
        self._small = t.tk_font(self, 11)
        self._cell = max(self._font.measure("127"), self._font.measure("D38"),
                         self._font.metrics("linespace")) + t.px(self, CELL_MARGIN)
        self._gap, self._pad = t.px(self, GAP), t.px(self, PAD)
        self._width = 2 * self._pad + COLS * self._cell + (COLS - 1) * self._gap

        self._header = tk.Frame(self, bd=0)
        self._header.pack(fill="x", padx=(self._pad, t.px(self, 4)), pady=(t.px(self, 6), 0))
        self._title = tk.Label(self._header, text="PINS & BUTTONS", font=self._cap_font, anchor="w")
        self._title.pack(side="left")
        self._collapse = Glyph(self._header, "▸", on_collapse, t.TEXT_DIM, t.HOVER,
                               t.resolve(t.CANVAS_BG), size=12)
        self._collapse.pack(side="right")

        self._info = tk.Label(self, text=DEFAULT_INFO, anchor="nw", justify="left", font=self._small,
                              wraplength=self._width - 2 * self._pad, height=3)
        self._info.pack(side="bottom", fill="x", padx=self._pad, pady=(t.px(self, 4), t.px(self, 8)))

        body = tk.Frame(self, bd=0)
        body.pack(fill="both", expand=True)
        self._body = body
        self.canvas = tk.Canvas(body, width=self._width, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="y", expand=True)
        self._scrollbar = ctk.CTkScrollbar(body, orientation="vertical", width=12,
                                           command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._on_wheel)
        self.canvas.tag_bind("cell", "<Enter>", self._cell_enter)
        self.canvas.tag_bind("cell", "<Leave>", self._cell_leave)
        self.canvas.tag_bind("cell", "<Button-1>", self._cell_click)
        self.retheme()

    # --------------------------------------------------------------- api

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
        # The inset canvas tone, like the Device page's button grid: free
        # cells only stand out against it in dark mode.
        surface = t.resolve(t.CANVAS_BG)
        self.configure(bg=surface, highlightbackground=t.resolve(t.BORDER),
                       highlightcolor=t.resolve(t.BORDER))
        for w in (self._header, self._body):
            w.configure(bg=surface)
        self._title.configure(bg=surface, fg=t.resolve(t.TEXT_MUTED))
        self._info.configure(bg=surface, fg=t.resolve(t.TEXT_DIM))
        self._collapse.retheme()
        self.canvas.configure(bg=surface)
        self._draw()

    # ---------------------------------------------------------- drawing

    def _set_marks(self, marks: dict[str, set], pins: set[str], buttons: set[int]):
        pins, buttons = set(pins), set(buttons)
        changed = {("p", p) for p in pins ^ marks["p"]} | {("b", b) for b in buttons ^ marks["b"]}
        if not changed:
            return
        marks["p"], marks["b"] = pins, buttons
        self._repaint(changed)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        self._items.clear()
        self._tag_key.clear()
        y = self._pad // 2
        y = self._draw_legend(y)

        y = self._section(y, "PINS", "sum_p")
        rows: list[list[str]] = []
        group = None
        for p in self._pins:
            g = pin_order(p)[:3]
            # A new row per pin group (D, A, …) so D1-D10 lines up with B1-B10.
            if g != group or len(rows[-1]) == COLS:
                rows.append([])
                group = g
            rows[-1].append(p)
        for row in rows:
            for col, p in enumerate(row):
                self._make_cell(("p", p), p, col, y)
            y += self._cell + self._gap
        if not rows:
            c.create_text(self._pad, y, text="No pins known for this board.", anchor="nw",
                          font=self._small, fill=t.resolve(t.TEXT_MUTED))
            y += self._small.metrics("linespace")

        y += t.px(self, 12)
        y = self._section(y, "BUTTONS", "sum_b")
        for n in range(1, BUTTON_COUNT + 1):
            row, col = divmod(n - 1, COLS)
            self._make_cell(("b", n), str(n), col, y + row * (self._cell + self._gap))
        y += -(-BUTTON_COUNT // COLS) * (self._cell + self._gap)

        c.configure(scrollregion=(0, 0, self._width, y + self._pad))
        self._draw_summaries()

    def _draw_legend(self, y: int) -> int:
        c = self.canvas
        sw = t.px(self, 11)
        x = self._pad
        line = self._small.metrics("linespace")
        for label, fill, outline in (("On now", t.CELL_ON, t.CELL_ON), ("Used", USED_BG, USED_BG),
                                     ("Free", t.CELL_OFF, t.CELL_BORDER), ("Hovered rule", HOVER_FILL, HOVER_FILL)):
            c.create_rectangle(x, y + (line - sw) / 2, x + sw, y + (line + sw) / 2,
                               fill=t.resolve(fill), outline=t.resolve(outline))
            x += sw + t.px(self, 5)
            c.create_text(x, y, text=label, anchor="nw", font=self._small, fill=t.resolve(t.TEXT_DIM))
            x += self._small.measure(label) + t.px(self, 12)
        return y + line + t.px(self, 12)

    def _section(self, y: int, title: str, summary_tag: str) -> int:
        c = self.canvas
        c.create_text(self._pad, y, text=title, anchor="nw", font=self._cap_font,
                      fill=t.resolve(t.TEXT_MUTED))
        c.create_text(self._width - self._pad, y, text="", anchor="ne", font=self._small,
                      fill=t.resolve(t.TEXT_MUTED), tags=summary_tag)
        return y + self._cap_font.metrics("linespace") + t.px(self, 6)

    def _draw_summaries(self):
        pins_used = sum(1 for p in self._pins if p in self._pin_usage)
        self.canvas.itemconfigure("sum_p", text=f"{pins_used} of {len(self._pins)} used")
        self.canvas.itemconfigure("sum_b", text=f"{len(self._button_usage)} of {BUTTON_COUNT} assigned")

    def _make_cell(self, key: tuple, label: str, col: int, y: int):
        x = self._pad + col * (self._cell + self._gap)
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
        if self._info.cget("text") != text:
            self._info.configure(text=text)

    def _on_yscroll(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self._scrollbar.winfo_manager():
                self._scrollbar.pack_forget()
        elif not self._scrollbar.winfo_manager():
            self._scrollbar.pack(side="right", fill="y")
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

    Docked in the tab's second column when the cards still get
    CARDS_MIN_W beside it; otherwise a thin strip, and the panel opens over
    the right side of the cards (Esc or the strip closes it)."""

    def __init__(self, tab, page: ConfigurePage):
        self.tab, self.page = tab, page
        self.editor = page.rule_editor
        self.panel = IOPanel(tab, self._pin_click, self._button_click, self._collapse)
        self.strip = tk.Canvas(tab, width=t.px(tab, STRIP_W), highlightthickness=1, bd=0, cursor="hand2")
        self.strip.bind("<Button-1>", lambda _e: self._expand())
        self.strip.bind("<Configure>", lambda _e: self._draw_strip())
        self.strip.bind("<Enter>", lambda _e: self.strip.configure(bg=t.resolve(t.HOVER)))
        self.strip.bind("<Leave>", lambda _e: self.strip.configure(bg=t.resolve(t.SURFACE_2)))
        self._want_open = True   # docked whenever there is room
        self._overlay = False    # opened over the cards in a narrow window
        self._mode: str | None = None
        self._hover_rule: Rule | None = None
        self._refresh_after: str | None = None

        self.editor.hover_listener = self._on_rule_hover
        tab.bind("<Configure>", lambda _e: self._layout(), add="+")
        page.app.bind("<Escape>", lambda _e: self._close_overlay(), add="+")
        page.app.register_theme_listener(self.retheme)
        self.retheme()
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
        # The overlay would cover the card the click is about.
        self._close_overlay()
        if users:
            self.editor.reveal(users[0])
        else:
            self.editor.add_rule(input_pin=pin)
            self.refresh()

    def _button_click(self, n: int):
        users = self.editor.model.button_usage().get(n)
        if users:
            self._close_overlay()
            self.editor.reveal(users[0])

    # ------------------------------------------------------------- layout

    def _wide(self) -> bool:
        return self.tab.winfo_width() >= t.px(self.tab, CARDS_MIN_W) + self.panel.winfo_reqwidth()

    def _layout(self):
        mode = "docked" if (self._wide() and self._want_open) else ("overlay" if self._overlay else "strip")
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "docked":
            self._overlay = False
            self.panel.place_forget()
            self.strip.grid_remove()
            self.panel.grid(row=1, column=1, sticky="ns", padx=(t.px(self.tab, 6) // 2, 0), pady=4)
            return
        self.panel.grid_remove()
        self.strip.grid(row=1, column=1, sticky="ns", padx=(4, 0), pady=4)
        if mode == "overlay":
            self.panel.place(in_=self.tab, relx=1.0, x=-(t.px(self.tab, STRIP_W) + 6), y=4,
                             relheight=1.0, height=-8, anchor="ne")
            self.panel.lift()
        else:
            self.panel.place_forget()
        self._draw_strip()

    def _expand(self):
        if self._wide():
            self._want_open = True
        else:
            self._overlay = not self._overlay
        self._layout()

    def _collapse(self):
        if self._mode == "overlay":
            self._overlay = False
        else:
            self._want_open = False
        self._layout()

    def _close_overlay(self):
        if self._overlay:
            self._overlay = False
            self._layout()

    # -------------------------------------------------------------- theme

    def retheme(self):
        self.panel.retheme()
        self.strip.configure(bg=t.resolve(t.SURFACE_2), highlightbackground=t.resolve(t.BORDER),
                             highlightcolor=t.resolve(t.BORDER))
        self._draw_strip()

    def _draw_strip(self):
        s = self.strip
        s.delete("all")
        w = s.winfo_width() if s.winfo_width() > 1 else t.px(s, STRIP_W)
        s.create_text(w / 2, t.px(s, 16), text="▸" if self._mode == "overlay" else "◂", font=t.tk_font(s, 12), fill=t.resolve(t.TEXT_DIM))
        s.create_text(w / 2, t.px(s, 32), text="PINS & BUTTONS", angle=270, anchor="w",
                      font=t.tk_font(s, 11, "bold"), fill=t.resolve(t.TEXT_DIM))
