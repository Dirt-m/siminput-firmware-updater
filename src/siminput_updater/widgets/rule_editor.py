from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import RULE_TYPE_LABELS, Rule

if TYPE_CHECKING:
    from ..pages.configure_page import ConfigurePage

RULE_TYPES = list(RULE_TYPE_LABELS.keys())
RULE_TYPE_DISPLAY = [RULE_TYPE_LABELS[k] for k in RULE_TYPES]


def _option(master, values, command=None, width=150):
    return ctk.CTkOptionMenu(
        master, values=values, command=command, width=width, height=30,
        corner_radius=t.RADIUS, fg_color=t.SURFACE_3, button_color=t.SURFACE_3,
        button_hover_color=t.HOVER, text_color=t.TEXT, font=t.font(12),
        dropdown_fg_color=t.SURFACE_2, dropdown_hover_color=t.HOVER, dropdown_text_color=t.TEXT,
    )


def _entry(master, width=78):
    return ctk.CTkEntry(master, width=width, height=30, corner_radius=t.RADIUS,
                        fg_color=t.SURFACE_3, border_color=t.BORDER, font=t.mono(12))


class RuleCard(ctk.CTkFrame):
    """A single rule, fully editable inline — no expand/collapse step.

    Pin/button refs are free-text entries (there are 128 possible buttons, so a
    dropdown is useless); only the rule type and axis target are dropdowns.
    """

    def __init__(self, parent, editor: RuleEditor, rule: Rule, index: int):
        super().__init__(parent, border_width=1, border_color=t.BORDER, corner_radius=t.RADIUS, fg_color=t.SURFACE_2)
        self.editor = editor
        self.rule = rule
        self.index = index
        self._widgets: dict[str, object] = {}

        self.grid_columnconfigure(0, weight=1)

        # -- header: number · type · live summary · reorder/delete --
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            header, text=str(index + 1), width=20,
            font=t.mono(12, "bold"), text_color=t.TEXT_MUTED,
        ).grid(row=0, column=0, padx=(2, 8))

        self.type_menu = _option(header, RULE_TYPE_DISPLAY, command=self._on_type_change, width=150)
        self.type_menu.set(RULE_TYPE_LABELS.get(rule.type, RULE_TYPE_DISPLAY[0]))
        self.type_menu.grid(row=0, column=1, padx=(0, 12))

        self.summary_label = ctk.CTkLabel(
            header, text=rule.summary(), anchor="w", font=t.font(12), text_color=t.TEXT_MUTED)
        self.summary_label.grid(row=0, column=2, padx=4, sticky="w")

        btns = ctk.CTkFrame(header, fg_color="transparent")
        btns.grid(row=0, column=3)
        for col, (sym, delta) in enumerate(((" ▲ ", -1), (" ▼ ", 1))):
            ctk.CTkButton(
                btns, text=sym, width=28, height=28, corner_radius=t.RADIUS,
                fg_color="transparent", hover_color=t.HOVER, text_color=t.TEXT_DIM, font=t.font(11),
                command=lambda d=delta: editor.move_rule(self.index, d),
            ).grid(row=0, column=col, padx=1)
        ctk.CTkButton(
            btns, text="✕", width=28, height=28, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.ERROR_SOFT, text_color=t.ERROR, font=t.font(12),
            command=lambda: editor.remove_rule(self.index),
        ).grid(row=0, column=2, padx=(1, 2))

        # -- fields: always visible, laid out inline --
        self.fields = ctk.CTkFrame(self, fg_color="transparent")
        self.fields.grid(row=1, column=0, sticky="ew", padx=(34, 14), pady=(4, 16))
        self.fields.grid_columnconfigure(0, weight=1)
        self._row_count = 0
        self._build_fields()

    # ------------------------------------------------------------- fields

    def _build_fields(self):
        for w in self.fields.winfo_children():
            w.destroy()
        self._widgets.clear()
        self._row_count = 0

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
            self._field(r, "Pulse (ms)", lambda p: self._num(p, "pulse_ms", self.rule.pulse_ms))
            self._field(r, "Delay (ms)", lambda p: self._num(p, "delay_ms", self.rule.delay_ms))
            self._invert(r)
        elif tp == "NOR":
            r = self._row()
            self._field(r, "Inputs (any of)", lambda p: self._list(p, "inputs", self.rule.inputs))
            self._arrow(r)
            self._field(r, "Output", lambda p: self._pin(p, "output", self.rule.output))
            self._invert(r)
        elif tp == "ENCODER":
            pins = (list(self.rule.inputs) + ["", ""])[:2]
            r = self._row()
            self._field(r, "Pin A", lambda p: self._pin(p, "pin_a", pins[0]))
            self._field(r, "Pin B", lambda p: self._pin(p, "pin_b", pins[1]))
            self._field(r, "CW out", lambda p: self._pin(p, "cw", self.rule.cw))
            self._field(r, "CCW out", lambda p: self._pin(p, "ccw", self.rule.ccw))
            r2 = self._row()
            self._field(r2, "Pulse (ms)", lambda p: self._num(p, "pulse_ms", self.rule.pulse_ms))
            self._field(r2, "Steps/detent", lambda p: self._num(p, "divisor", self.rule.divisor))
            self._invert(r2)
        elif tp in ("AXIS_INC", "AXIS_DEC"):
            r = self._row()
            self._field(r, "Input", lambda p: self._pin(p, "input", self.rule.input))
            self._field(r, "Axis", lambda p: self._axis(p))
            self._field(r, "Step", lambda p: self._num(p, "step", self.rule.step))

    def _row(self) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self.fields, fg_color="transparent")
        row.grid(row=self._row_count, column=0, sticky="ew", pady=7)
        self._row_count += 1
        return row

    def _field(self, row, label, builder):
        """A label + its input, grouped so the gap between fields is clear."""
        group = ctk.CTkFrame(row, fg_color="transparent")
        group.pack(side="left", padx=(0, 24))
        ctk.CTkLabel(group, text=label, font=t.font(12), text_color=t.TEXT_DIM).pack(side="left", padx=(0, 8))
        widget = builder(group)
        widget.pack(side="left")
        return widget

    def _arrow(self, row):
        ctk.CTkLabel(row, text="→", font=t.font(14), text_color=t.TEXT_MUTED).pack(side="left", padx=(0, 24))

    def _pin(self, parent, key, current):
        e = _entry(parent, width=78)
        if current:
            e.insert(0, current)
        e.bind("<KeyRelease>", lambda _e: self._sync())
        self._widgets[key] = e
        return e

    def _num(self, parent, key, current):
        e = _entry(parent, width=64)
        e.insert(0, str(current))
        e.bind("<KeyRelease>", lambda _e: self._sync())
        self._widgets[key] = e
        return e

    def _list(self, parent, key, current):
        e = _entry(parent, width=200)
        if current:
            e.insert(0, ", ".join(current))
        e.bind("<KeyRelease>", lambda _e: self._sync())
        self._widgets[key] = e
        return e

    def _axis(self, parent):
        choices = self.editor.axis_choices() or [""]
        current = self.rule.axis
        if current and current not in choices:
            choices = [current, *choices]
        menu = _option(parent, choices, command=lambda _v: self._sync(), width=120)
        menu.set(current or choices[0])
        self._widgets["axis"] = menu
        return menu

    def _invert(self, row):
        cb = ctk.CTkCheckBox(
            row, text="Invert", command=self._sync, font=t.font(12),
            fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM,
            checkbox_width=20, checkbox_height=20)
        cb.pack(side="right", padx=(24, 0))
        if self.rule.invert:
            cb.select()
        self._widgets["invert"] = cb

    # ------------------------------------------------------------- sync

    def _on_type_change(self, display_name: str):
        self._apply_edits()  # keep any compatible field values
        for key, label in RULE_TYPE_LABELS.items():
            if label == display_name:
                self.rule.type = key
                break
        self._build_fields()
        self._sync()

    def _sync(self):
        self._apply_edits()
        self.summary_label.configure(text=self.rule.summary())
        self.editor.page._mark_dirty()

    def _apply_edits(self):
        w = self._widgets
        if "input" in w:
            self.rule.input = w["input"].get().strip()
        if "inputs" in w:
            self.rule.inputs = [s.strip() for s in w["inputs"].get().split(",") if s.strip()]
        if "pin_a" in w or "pin_b" in w:
            self.rule.inputs = [w[k].get().strip() for k in ("pin_a", "pin_b") if k in w and w[k].get().strip()]
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


class RuleEditor(ctk.CTkFrame):
    def __init__(self, parent, page: ConfigurePage):
        super().__init__(parent, fg_color="transparent")
        self.page = page
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        self._cards: list[RuleCard] = []
        self._rules: list[Rule] = []

        self._empty = ctk.CTkLabel(
            self.scroll, text="No rules yet. Add a rule to map a pin to a button or axis.",
            font=t.font(12), text_color=t.TEXT_MUTED)
        self._empty.grid(row=0, column=0, pady=20)

        t.primary_button(self, "+ Add rule", self._add_rule, width=120).grid(
            row=1, column=0, padx=4, pady=10, sticky="w")

    def axis_choices(self) -> list[str]:
        """Current axis names from the Axes tab, for the AXIS_* target dropdown."""
        try:
            return [w["name"].get().strip() for w in self.page._axis_widgets if w["name"].get().strip()]
        except Exception:
            return []

    def load_rules(self, rules: list[Rule]):
        self._rules = [Rule(**r.__dict__) for r in rules]
        self._rebuild()

    def collect_rules(self) -> list[Rule]:
        for card in self._cards:
            card._apply_edits()
        return [Rule(**r.__dict__) for r in self._rules]

    def _rebuild(self):
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        if not self._rules:
            self._empty.grid()
            return
        self._empty.grid_remove()
        for i, rule in enumerate(self._rules):
            card = RuleCard(self.scroll, self, rule, i)
            card.grid(sticky="ew", padx=4, pady=4)
            self._cards.append(card)

    def _add_rule(self):
        self._rules.append(Rule(type="MAP", input="D1", output="B1"))
        self._rebuild()
        self.page._mark_dirty()

    def remove_rule(self, index: int):
        if 0 <= index < len(self._rules):
            self._rules.pop(index)
            self._rebuild()
            self.page._mark_dirty()

    def move_rule(self, index: int, direction: int):
        new_index = index + direction
        if 0 <= new_index < len(self._rules):
            self._rules[index], self._rules[new_index] = self._rules[new_index], self._rules[index]
            self._rebuild()
            self.page._mark_dirty()
