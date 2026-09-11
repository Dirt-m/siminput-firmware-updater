"""The Rules tab: an ordered list of editable rule cards.

Design notes (these are what keep the list fast — keep them true):

* `RuleList` is the source of truth; cards mirror it one-to-one and every
  structural edit (add, delete, duplicate, move) touches only the cards
  involved. Nothing rebuilds the whole list except a config load.
* A card resolves its own position with `editor.index_of(self)` at click
  time. Never bake an index into a callback — cards outlive reorders.
* Card chrome (handle, number, captions, glyph buttons, the type picker) is
  plain tk. customtkinter widgets each cost a canvas plus a draw engine, so
  they are used only where the user types or toggles. Plain-tk colours are
  resolved with `t.resolve()` and re-applied in `retheme()`.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import RULE_TYPE_LABELS, Rule
from ..rule_list import RuleList

if TYPE_CHECKING:
    from ..pages.configure_page import ConfigurePage

RULE_TYPES = list(RULE_TYPE_LABELS.keys())
HANDLE_GLYPH = "⠿"
DUPLICATE_GLYPH = "❐"
DELETE_GLYPH = "✕"
SYNC_BUILD_COUNT = 12   # cards built before the first paint on a config load
BUILD_CHUNK = 6         # cards per idle callback after that
AUTOSCROLL_MARGIN = 28  # px from the viewport edge that starts auto-scroll


def _entry(master, bg: str, width=78):
    return ctk.CTkEntry(master, width=width, height=30, corner_radius=t.RADIUS, bg_color=bg,
                        fg_color=t.SURFACE_3, border_color=t.BORDER, font=t.mono(12))


class Picker(tk.Label):
    """A dropdown drawn as a plain label; the choices open in a tk.Menu.

    Replaces CTkOptionMenu on the card (its single largest cost). The menu is
    built fresh per click with resolved colours, so only the label needs
    re-theming.
    """

    def __init__(self, master, values: list[str], command: Callable[[str], None], width_chars: int = 16):
        super().__init__(master, anchor="w", padx=8, cursor="hand2", width=width_chars)
        self._values = list(values)
        self._command = command
        self._value = values[0] if values else ""
        self.bind("<Button-1>", self._open)
        self.retheme()
        self._render()

    def get(self) -> str:
        return self._value

    def set(self, value: str):
        self._value = value
        self._render()

    def configure_values(self, values: list[str]):
        self._values = list(values)

    def _render(self):
        self.configure(text=f"{self._value}  ▾")

    def retheme(self):
        self.configure(
            bg=t.resolve(t.SURFACE_3), fg=t.resolve(t.TEXT),
            font=t.tk_font(self, 12),
        )

    def _open(self, _e=None):
        menu = tk.Menu(
            self, tearoff=0, relief="flat", borderwidth=0,
            bg=t.resolve(t.SURFACE_2), fg=t.resolve(t.TEXT),
            activebackground=t.resolve(t.HOVER), activeforeground=t.resolve(t.TEXT),
            font=t.tk_font(self, 12),
        )
        for v in self._values:
            menu.add_command(label=v, command=lambda v=v: self._choose(v))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _choose(self, value: str):
        if value != self._value:
            self.set(value)
            self._command(value)


class Glyph(tk.Label):
    """A flat icon button drawn as a label with a hover fill."""

    def __init__(self, master, text: str, command: Callable[[], None],
                 color, hover, bg: str, size: int = 13):
        super().__init__(master, text=text, padx=6, cursor="hand2", bg=bg,
                         fg=t.resolve(color), font=t.tk_font(master, size))
        self._color, self._hover, self._size = color, hover, size
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self.configure(bg=t.resolve(self._hover)))
        self.bind("<Leave>", lambda _e: self.configure(bg=self.master.cget("bg")))

    def retheme(self):
        self.configure(bg=self.master.cget("bg"), fg=t.resolve(self._color),
                       font=t.tk_font(self, self._size))


class RuleCard(tk.Frame):
    """One rule, fully editable inline. One row for most types; PULSE and
    ENCODER need a second row because their fields do not fit the window."""

    def __init__(self, parent, editor: RuleEditor, rule: Rule):
        super().__init__(parent, highlightthickness=1)
        self.editor = editor
        self.rule = rule
        self._widgets: dict[str, object] = {}
        self._plain: list[tk.Widget] = []     # plain-tk children to retheme
        self._ctk_children: list = []          # CTk children needing bg_color sync
        self._rows: list[tk.Frame] = []

        self.grid_columnconfigure(1, weight=1)

        bg = self._bg = t.resolve(t.SURFACE_2)
        self._dim = t.resolve(t.TEXT_DIM)
        self._muted = t.resolve(t.TEXT_MUTED)
        self._cap_font = t.tk_font(parent, 12)
        self.configure(bg=bg, highlightbackground=t.resolve(t.BORDER), highlightcolor=t.resolve(t.BORDER))

        # -- left rail: drag handle + number --
        rail = tk.Frame(self, bg=bg)
        rail.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(6, 4), pady=6)
        self._plain.append(rail)
        self.handle = tk.Label(rail, text=HANDLE_GLYPH, cursor="fleur", padx=2, bg=bg,
                               fg=self._muted, font=t.tk_font(parent, 14))
        self.handle.pack(side="left")
        self.num = tk.Label(rail, width=3, anchor="e", bg=bg, fg=self._muted,
                            font=t.tk_font(parent, 12, "bold", mono=True))
        self.num.pack(side="left")
        for w in (self.handle, self.num):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._context_menu)
            w.bind("<Button-2>", self._context_menu)

        # -- body: type picker + fields --
        self.body = tk.Frame(self, bg=bg)
        self.body.grid(row=0, column=1, sticky="ew", pady=(6, 6))
        self.body.grid_columnconfigure(1, weight=1)
        self._plain.append(self.body)

        if rule.comment:
            lbl = self._caption(self.body, "Comment", anchor="w")
            lbl.grid(row=0, column=0, padx=(0, 12), sticky="w")
            self.type_picker = None
            self.summary = tk.Label(self.body, text=rule.summary(), anchor="w", bg=bg,
                                    fg=self._muted, font=self._cap_font)
            self.summary.grid(row=0, column=1, sticky="w")
        else:
            self.type_picker = Picker(self.body, [RULE_TYPE_LABELS[k] for k in RULE_TYPES],
                                      self._on_type_change)
            self.type_picker.set(RULE_TYPE_LABELS.get(rule.type, RULE_TYPE_LABELS["MAP"]))
            self.type_picker.grid(row=0, column=0, padx=(0, 14), sticky="nw")
            self._plain.append(self.type_picker)
            self.fields = tk.Frame(self.body, bg=bg)
            self.fields.grid(row=0, column=1, sticky="ew")
            self.fields.grid_columnconfigure(0, weight=1)
            self._plain.append(self.fields)
            self.summary = None
            self._build_fields()

        # -- right: duplicate / delete --
        actions = tk.Frame(self, bg=bg)
        actions.grid(row=0, column=2, sticky="ne", padx=(4, 6), pady=6)
        self._plain.append(actions)
        Glyph(actions, DUPLICATE_GLYPH, self._duplicate, t.TEXT_DIM, t.HOVER, bg).pack(side="left")
        Glyph(actions, DELETE_GLYPH, self._delete, t.ERROR, t.ERROR_SOFT, bg).pack(side="left")
        self._glyphs = [w for w in actions.winfo_children()]

    def _caption(self, parent, text: str, **kw) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=self._bg, fg=self._dim, font=self._cap_font, **kw)
        self._plain.append(lbl)
        return lbl

    # ------------------------------------------------------------- fields

    def _build_fields(self):
        for w in self.fields.winfo_children():
            w.destroy()
        self._widgets.clear()
        self._ctk_children = [c for c in self._ctk_children if c.winfo_exists()]
        self._plain = [w for w in self._plain if w.winfo_exists()]
        self._rows = []
        self.summary = None

        tp = self.rule.type
        if tp in ("MAP", "TOGGLE"):
            r = self._row()
            self._field(r, "Input", lambda p: self._pin(p, "input", self.rule.input))
            self._arrow(r)
            self._field(r, "Output", lambda p: self._pin(p, "output", self.rule.output))
            self._invert(r)
        elif tp == "PULSE":
            r = self._row()
            self._field(r, "Input", lambda p: self._pin(p, "input", self.rule.input))
            self._arrow(r)
            self._field(r, "Output", lambda p: self._pin(p, "output", self.rule.output))
            self._invert(r)
            r2 = self._row()
            self._field(r2, "Pulse (ms)", lambda p: self._num(p, "pulse_ms", self.rule.pulse_ms))
            self._field(r2, "Delay (ms)", lambda p: self._num(p, "delay_ms", self.rule.delay_ms))
            self._summary()
        elif tp == "NOR":
            r = self._row()
            self._field(r, "Inputs (any of)", lambda p: self._list(p, "inputs", self.rule.inputs))
            self._arrow(r)
            self._field(r, "Output", lambda p: self._pin(p, "output", self.rule.output))
            self._invert(r)
            self._summary()
        elif tp == "ENCODER":
            pins = (list(self.rule.inputs) + ["", ""])[:2]
            r = self._row()
            self._field(r, "Pin A", lambda p: self._pin(p, "pin_a", pins[0]))
            self._field(r, "Pin B", lambda p: self._pin(p, "pin_b", pins[1]))
            self._field(r, "CW", lambda p: self._pin(p, "cw", self.rule.cw))
            self._field(r, "CCW", lambda p: self._pin(p, "ccw", self.rule.ccw))
            r2 = self._row()
            self._field(r2, "Pulse (ms)", lambda p: self._num(p, "pulse_ms", self.rule.pulse_ms))
            self._field(r2, "Steps/detent", lambda p: self._num(p, "divisor", self.rule.divisor))
            self._invert(r2)
            self._summary()
        elif tp in ("AXIS_INC", "AXIS_DEC"):
            r = self._row()
            self._field(r, "Input", lambda p: self._pin(p, "input", self.rule.input))
            self._field(r, "Axis", lambda p: self._axis(p))
            self._field(r, "Step", lambda p: self._num(p, "step", self.rule.step))
        self._sync_summary()

    def _row(self, gap: int = 6) -> tk.Frame:
        row = tk.Frame(self.fields, bg=self._bg)
        row.grid(row=len(self._rows), column=0, sticky="ew", pady=(0 if not self._rows else gap, 0))
        self._rows.append(row)
        return row

    def _field(self, row, label, builder):
        self._caption(row, label).pack(side="left", padx=(0, 6))
        widget = builder(row)
        widget.pack(side="left", padx=(0, 18))
        return widget

    def _arrow(self, row):
        self._caption(row, "→").pack(side="left", padx=(0, 18))

    def _summary(self):
        """A one-line reading of the rule on its own row, for the types whose
        fields alone don't tell the story (NOR, PULSE, ENCODER)."""
        row = self._row(gap=3)
        self.summary = tk.Label(row, anchor="w", bg=self._bg, fg=self._muted, font=self._cap_font)
        self.summary.pack(side="left")

    def _bind_entry(self, e):
        e.bind("<KeyRelease>", lambda _e: self._sync())
        e.bind("<Alt-Up>", lambda _e: self._nudge(-1))
        e.bind("<Alt-Down>", lambda _e: self._nudge(1))
        self._ctk_children.append(e)

    def _pin(self, parent, key, current):
        e = _entry(parent, self._bg, width=78)
        if current:
            e.insert(0, current)
        self._bind_entry(e)
        self._widgets[key] = e
        return e

    def _num(self, parent, key, current):
        e = _entry(parent, self._bg, width=64)
        e.insert(0, str(current))
        self._bind_entry(e)
        self._widgets[key] = e
        return e

    def _list(self, parent, key, current):
        e = _entry(parent, self._bg, width=200)
        if current:
            e.insert(0, ", ".join(current))
        self._bind_entry(e)
        self._widgets[key] = e
        return e

    def _axis(self, parent):
        choices = self.editor.axis_choices() or [""]
        current = self.rule.axis
        if current and current not in choices:
            choices = [current, *choices]
        picker = Picker(parent, choices, lambda _v: self._sync(), width_chars=12)
        picker.set(current or choices[0])
        self._widgets["axis"] = picker
        self._plain.append(picker)
        return picker

    def refresh_axis_choices(self):
        picker = self._widgets.get("axis")
        if picker is None:
            return
        choices = self.editor.axis_choices() or [""]
        current = picker.get()
        if current and current not in choices:
            choices = [current, *choices]
        picker.configure_values(choices)

    def _invert(self, row):
        cb = ctk.CTkCheckBox(
            row, text="Invert", command=self._sync, font=t.font(12), bg_color=self._bg,
            fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM,
            checkbox_width=20, checkbox_height=20, width=80,
            variable=tk.IntVar(value=1 if self.rule.invert else 0))
        cb.pack(side="left", padx=(6, 0))
        self._widgets["invert"] = cb
        self._ctk_children.append(cb)

    # -------------------------------------------------------------- theme

    def retheme(self):
        bg = self._bg = t.resolve(t.SURFACE_2)
        self._dim = t.resolve(t.TEXT_DIM)
        self._muted = t.resolve(t.TEXT_MUTED)
        border = t.resolve(t.BORDER)
        self.configure(bg=bg, highlightbackground=border, highlightcolor=border)
        for w in self._plain:
            if not w.winfo_exists():
                continue
            if isinstance(w, Picker):
                w.retheme()
            elif isinstance(w, tk.Label):
                w.configure(bg=bg, fg=self._dim)
            else:
                w.configure(bg=bg)
        for row in self._rows:
            row.configure(bg=bg)
        self.handle.configure(bg=bg, fg=self._muted)
        self.num.configure(bg=bg, fg=self._muted)
        if self.summary is not None and self.summary.winfo_exists():
            self.summary.configure(bg=bg, fg=self._muted)
        for g in self._glyphs:
            g.retheme()
        for c in self._ctk_children:
            if c.winfo_exists():
                c.configure(bg_color=bg)

    def set_number(self, n: int):
        self.num.configure(text=str(n))

    # --------------------------------------------------------------- sync

    def _on_type_change(self, display_name: str):
        self._apply_edits()  # keep any compatible field values
        for key, label in RULE_TYPE_LABELS.items():
            if label == display_name:
                self.rule.type = key
                break
        # Per-type pulse default, matching the firmware: 100 ms for PULSE,
        # single-cycle (0) for everything else including ENCODER.
        self.rule.pulse_ms = 100 if self.rule.type == "PULSE" else 0
        self._build_fields()
        self._sync()

    def _sync(self):
        self._apply_edits()
        self._sync_summary()
        self.editor.page._mark_dirty()

    def _sync_summary(self):
        if self.summary is not None and self.summary.winfo_exists() and not self.rule.comment:
            self.summary.configure(text=self.rule.summary())

    def _apply_edits(self):
        w = self._widgets
        if "input" in w:
            self.rule.input = w["input"].get().strip()
        if "inputs" in w:
            self.rule.inputs = [s.strip() for s in w["inputs"].get().split(",") if s.strip()]
        if "pin_a" in w or "pin_b" in w:
            # Positional: clearing Pin A must not shift Pin B into its slot on
            # the next rebuild. Empty slots are validation errors, not gaps.
            self.rule.inputs = [
                w["pin_a"].get().strip() if "pin_a" in w else "",
                w["pin_b"].get().strip() if "pin_b" in w else "",
            ]
        if "output" in w:
            self.rule.output = w["output"].get().strip()
        if "cw" in w:
            self.rule.cw = w["cw"].get().strip()
        if "ccw" in w:
            self.rule.ccw = w["ccw"].get().strip()
        if "axis" in w:
            self.rule.axis = w["axis"].get()
        if "invert" in w:
            self.rule.invert = bool(w["invert"].get())
        for key in ("pulse_ms", "delay_ms", "step", "divisor"):
            if key in w:
                try:
                    setattr(self.rule, key, int(w[key].get()))
                except (ValueError, AttributeError):
                    pass

    def focus_first(self):
        for key in ("input", "inputs", "pin_a"):
            e = self._widgets.get(key)
            if e is not None:
                e.focus_set()
                try:
                    e.select_range(0, "end")
                except Exception:
                    pass
                return

    # ------------------------------------------------------------ actions

    def _duplicate(self):
        self.editor.duplicate_card(self)

    def _delete(self):
        self.editor.remove_card(self)

    def _nudge(self, delta: int):
        i = self.editor.index_of(self)
        self.editor.move_card(self, i + delta)
        return "break"

    def _context_menu(self, event):
        i = self.editor.index_of(self)
        last = len(self.editor) - 1
        menu = tk.Menu(
            self, tearoff=0, relief="flat", borderwidth=0,
            bg=t.resolve(t.SURFACE_2), fg=t.resolve(t.TEXT),
            activebackground=t.resolve(t.HOVER), activeforeground=t.resolve(t.TEXT),
            disabledforeground=t.resolve(t.TEXT_MUTED), font=t.tk_font(self, 12),
        )
        menu.add_command(label="Move up", accelerator="Alt+↑", command=lambda: self._nudge(-1),
                         state="normal" if i > 0 else "disabled")
        menu.add_command(label="Move down", accelerator="Alt+↓", command=lambda: self._nudge(1),
                         state="normal" if i < last else "disabled")
        menu.add_command(label="Move to top", command=lambda: self.editor.move_card(self, 0),
                         state="normal" if i > 0 else "disabled")
        menu.add_command(label="Move to bottom", command=lambda: self.editor.move_card(self, last),
                         state="normal" if i < last else "disabled")
        menu.add_command(label="Move to position…", command=self._move_to_prompt)
        menu.add_separator()
        menu.add_command(label="Duplicate", command=self._duplicate)
        menu.add_command(label="Delete", command=self._delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _move_to_prompt(self):
        n = len(self.editor)
        dialog = ctk.CTkInputDialog(title="Move rule", text=f"Move rule {self.editor.index_of(self) + 1} to position (1–{n}):")
        text = dialog.get_input()
        if not text:
            return
        try:
            pos = int(text.strip())
        except ValueError:
            self.editor.page.app.show_status(f"'{text}' is not a position", "error")
            return
        self.editor.move_card(self, max(1, min(n, pos)) - 1)

    # --------------------------------------------------------------- drag

    def _drag_start(self, event):
        self.editor.drag_begin(self, event.y_root)

    def _drag_motion(self, event):
        self.editor.drag_update(event.y_root)

    def _drag_end(self, event):
        self.editor.drag_finish(event.y_root)


class RuleEditor(ctk.CTkFrame):
    def __init__(self, parent, page: ConfigurePage):
        super().__init__(parent, fg_color="transparent")
        self.page = page
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Top bar: the primary action lives here, never below the fold.
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 8))
        bar.grid_columnconfigure(1, weight=1)
        t.primary_button(bar, "+ Add rule", self.add_rule, width=120).grid(row=0, column=0, sticky="w")
        self._count = ctk.CTkLabel(bar, text="", font=t.font(12), text_color=t.TEXT_MUTED, anchor="e")
        self._count.grid(row=0, column=1, sticky="e", padx=(12, 4))

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=1, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        self._model = RuleList()
        self._cards: list[RuleCard] = []
        self._build_pending: list[Rule] = []
        self._build_after: str | None = None

        self._empty = ctk.CTkLabel(
            self.scroll, text="No rules yet. Add a rule to map a pin to a button or axis.",
            font=t.font(12), text_color=t.TEXT_MUTED)
        self._empty.grid(row=0, column=0, pady=20)

        # Drop indicator shown between cards while dragging.
        self._indicator = tk.Frame(self.scroll, height=3, bg=t.resolve(t.ACCENT))
        self._drag: dict | None = None
        self._autoscroll_after: str | None = None

        try:
            page.app.register_theme_listener(self.retheme)
        except Exception:
            pass
        self._update_count()

    # --------------------------------------------------------------- api

    def __len__(self) -> int:
        return len(self._model)

    def axis_choices(self) -> list[str]:
        """Current axis names from the Axes tab, for the AXIS_* target dropdown."""
        try:
            return [w["name"].get().strip() for w in self.page._axis_widgets if w["name"].get().strip()]
        except Exception:
            return []

    def device_pins(self) -> list[str] | None:
        try:
            return self.page.app.device_pins
        except Exception:
            return None

    def load_rules(self, rules: list[Rule]):
        """Replace the whole list. The first screenful is built before this
        returns; the rest is built in idle-time chunks so a long config
        never freezes the window."""
        self._cancel_build()
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        self._model = RuleList(rules)
        self._build_pending = list(self._model.rules)
        self._build_some(SYNC_BUILD_COUNT)
        self._schedule_build()
        self._update_count()

    def collect_rules(self) -> list[Rule]:
        for card in self._cards:
            if not card.rule.comment:
                card._apply_edits()
        return [r.copy() for r in self._model.rules]

    def refresh_axis_menus(self):
        for card in self._cards:
            card.refresh_axis_choices()

    def retheme(self):
        self._indicator.configure(bg=t.resolve(t.ACCENT))
        for card in self._cards:
            card.retheme()

    def index_of(self, card: RuleCard) -> int:
        return self._cards.index(card)

    # ----------------------------------------------------- incremental build

    def _build_some(self, n: int):
        while self._build_pending and n > 0:
            rule = self._build_pending.pop(0)
            self._place_card(RuleCard(self.scroll, self, rule), len(self._cards))
            n -= 1
        self._sync_empty()

    def _schedule_build(self):
        if self._build_pending:
            self._build_after = self.after(1, self._build_step)

    def _build_step(self):
        self._build_after = None
        self._build_some(BUILD_CHUNK)
        self._schedule_build()

    def _cancel_build(self):
        if self._build_after is not None:
            try:
                self.after_cancel(self._build_after)
            except Exception:
                pass
            self._build_after = None
        self._build_pending = []

    def _flush_build(self):
        """Finish any pending idle build before a structural edit, so card
        positions and model positions agree."""
        if self._build_pending:
            if self._build_after is not None:
                self.after_cancel(self._build_after)
                self._build_after = None
            self._build_some(len(self._build_pending))

    def _place_card(self, card: RuleCard, index: int):
        self._cards.insert(index, card)
        card.grid(row=index, column=0, sticky="ew", padx=4, pady=3)
        self._regrid(index + 1)
        self._renumber(index)

    def _regrid(self, start: int, end: int | None = None):
        end = len(self._cards) if end is None else end
        for i in range(start, end):
            self._cards[i].grid_configure(row=i)

    def _renumber(self, start: int = 0):
        for i in range(start, len(self._cards)):
            self._cards[i].set_number(i + 1)

    def _sync_empty(self):
        if self._model.rules:
            self._empty.grid_remove()
        else:
            self._empty.grid()

    def _update_count(self):
        n = len(self._model)
        self._count.configure(text=f"{n} rule" if n == 1 else f"{n} rules")

    def _changed(self):
        self._sync_empty()
        self._update_count()
        self.page._mark_dirty()

    def _scroll_to(self, card: RuleCard):
        self.update_idletasks()
        canvas = self.scroll._parent_canvas
        inner_h = max(1, self.scroll.winfo_height())
        view_h = max(1, canvas.winfo_height())
        if inner_h <= view_h:
            return
        top = card.winfo_y()
        bottom = top + card.winfo_height()
        first, last = canvas.yview()
        vis_top, vis_bottom = first * inner_h, last * inner_h
        if top < vis_top:
            canvas.yview_moveto(top / inner_h)
        elif bottom > vis_bottom:
            canvas.yview_moveto(max(0.0, (bottom - view_h) / inner_h))

    # --------------------------------------------------------- operations

    def add_rule(self):
        self._flush_build()
        rule = self._model.new_rule(self.device_pins())
        index = self._model.append(rule)
        card = RuleCard(self.scroll, self, rule)
        self._place_card(card, index)
        self._changed()
        self._scroll_to(card)
        card.focus_first()

    def duplicate_card(self, card: RuleCard):
        self._flush_build()
        src = self.index_of(card)
        card._apply_edits()
        index = self._model.duplicate(src)
        new = RuleCard(self.scroll, self, self._model[index])
        self._place_card(new, index)
        self._changed()
        self._scroll_to(new)
        new.focus_first()

    def remove_card(self, card: RuleCard):
        self._flush_build()
        index = self.index_of(card)
        self._model.remove(index)
        self._cards.pop(index)
        card.destroy()
        self._regrid(index)
        self._renumber(index)
        self._changed()

    def move_card(self, card: RuleCard, dst: int):
        self._flush_build()
        src = self.index_of(card)
        dst = max(0, min(dst, len(self._cards) - 1))
        if src == dst:
            return
        self._model.move(src, dst)
        self._cards.pop(src)
        self._cards.insert(dst, card)
        lo, hi = min(src, dst), max(src, dst) + 1
        self._regrid(lo, hi)
        self._renumber(lo)
        self._changed()
        self._scroll_to(card)

    # --------------------------------------------------------------- drag

    def drag_begin(self, card: RuleCard, y_root: int):
        self._flush_build()
        self._drag = {"card": card, "gap": None, "y": y_root}
        card.configure(highlightbackground=t.resolve(t.ACCENT), highlightcolor=t.resolve(t.ACCENT))

    def drag_update(self, y_root: int):
        if not self._drag:
            return
        self._drag["y"] = y_root
        gap = self._gap_at(y_root)
        if gap != self._drag["gap"]:
            self._drag["gap"] = gap
            self._show_indicator(gap)
        self._autoscroll(y_root)

    def drag_finish(self, y_root: int):
        if not self._drag:
            return
        drag, self._drag = self._drag, None
        self._stop_autoscroll()
        self._indicator.place_forget()
        card = drag["card"]
        card.configure(highlightbackground=t.resolve(t.BORDER), highlightcolor=t.resolve(t.BORDER))
        gap = drag["gap"] if drag["gap"] is not None else self._gap_at(y_root)
        src = self.index_of(card)
        if gap in (src, src + 1):
            return
        dst = gap - 1 if gap > src else gap
        self.move_card(card, dst)

    def _gap_at(self, y_root: int) -> int:
        """Insertion gap index for a pointer y: 0 = before the first card,
        len = after the last. Root coordinates, so window scaling is moot."""
        for i, c in enumerate(self._cards):
            top = c.winfo_rooty()
            if y_root < top + c.winfo_height() / 2:
                return i
        return len(self._cards)

    def _show_indicator(self, gap: int):
        if not self._cards:
            return
        if gap < len(self._cards):
            y = self._cards[gap].winfo_y() - 3
        else:
            last = self._cards[-1]
            y = last.winfo_y() + last.winfo_height() + 1
        self._indicator.place(x=4, y=max(0, y), relwidth=1.0, width=-8)
        self._indicator.lift()

    def _autoscroll(self, y_root: int):
        canvas = self.scroll._parent_canvas
        top = canvas.winfo_rooty()
        bottom = top + canvas.winfo_height()
        if y_root < top + AUTOSCROLL_MARGIN:
            self._drag["dir"] = -1
        elif y_root > bottom - AUTOSCROLL_MARGIN:
            self._drag["dir"] = 1
        else:
            self._drag["dir"] = 0
            self._stop_autoscroll()
            return
        if self._autoscroll_after is None:
            self._autoscroll_tick()

    def _autoscroll_tick(self):
        self._autoscroll_after = None
        if not self._drag or not self._drag.get("dir"):
            return
        canvas = self.scroll._parent_canvas
        first, last = canvas.yview()
        if (self._drag["dir"] < 0 and first > 0.0) or (self._drag["dir"] > 0 and last < 1.0):
            canvas.yview_scroll(self._drag["dir"], "units")
            gap = self._gap_at(self._drag["y"])
            if gap != self._drag["gap"]:
                self._drag["gap"] = gap
                self._show_indicator(gap)
        self._autoscroll_after = self.after(60, self._autoscroll_tick)

    def _stop_autoscroll(self):
        if self._autoscroll_after is not None:
            try:
                self.after_cancel(self._autoscroll_after)
            except Exception:
                pass
            self._autoscroll_after = None
