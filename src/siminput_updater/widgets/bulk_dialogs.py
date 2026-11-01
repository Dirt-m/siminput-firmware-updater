"""Dialogs for bulk edits on the Rules tab: duplicate with offset, add a
range of rules, find and replace.

Each one only computes rules (with the pure helpers in `rule_list`) and
hands them to the editor, which applies them as one undo step.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import RULE_TYPE_LABELS, Rule
from ..rule_list import (
    BUTTON_REFS, count_matches, default_offsets, next_free, pin_order, pin_range,
    range_rules, replace_refs, shifted_copies, variables_in,
)

if TYPE_CHECKING:
    from .rule_editor import RuleEditor

RANGE_TYPES = ("MAP", "TOGGLE", "PULSE")
PREVIEW_CHARS = 70


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _refs(rules: list[Rule]) -> str:
    """'D21, D22 → B21, B22; …' for a preview line."""
    parts = []
    for r in rules:
        ins = [x for x in (r.input, *r.inputs) if x]
        outs = [x for x in (r.output, r.cw, r.ccw) if x]
        parts.append(f"{', '.join(ins)} → {', '.join(outs)}")
    text = "; ".join(parts)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS - 1] + "…"


class _Dialog(ctk.CTkToplevel):
    """Title, a body grid for the subclass, a status line and the buttons."""

    def __init__(self, editor: RuleEditor, title: str, action: str, modal: bool = True):
        super().__init__(editor)
        self.editor = editor
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=t.SURFACE)
        self.transient(editor.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Return>", lambda _e: self._run())

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, padx=24, pady=(20, 8), sticky="nsew")
        self.body.grid_columnconfigure(2, weight=1)
        self._row = 0

        self.status = ctk.CTkLabel(self, text="", font=t.font(12), text_color=t.TEXT_DIM,
                                   anchor="w", justify="left", wraplength=400)
        self.status.grid(row=1, column=0, padx=24, pady=(0, 8), sticky="w")
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=2, column=0, padx=24, pady=(4, 20), sticky="e")
        t.ghost_button(btns, "Close" if not modal else "Cancel", self.close, width=90).grid(
            row=0, column=0, padx=(0, 8))
        self.ok = t.primary_button(btns, action, self._run, width=120)
        self.ok.grid(row=0, column=1)

        self.after(10, self._place)
        if modal:
            self.after(120, self._grab)

    def _place(self):
        self.update_idletasks()
        top = self.editor.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass   # not viewable yet on some window managers; modality is a nicety

    def field(self, label: str, widget_factory, hint: str = ""):
        ctk.CTkLabel(self.body, text=label, font=t.font(12), text_color=t.TEXT_DIM, anchor="w").grid(
            row=self._row, column=0, sticky="w", padx=(0, 12), pady=5)
        w = widget_factory(self.body)
        # Without a hint the widget may take the hint's column too, so a wide
        # one (the type selector) never pushes the hints away from their fields.
        w.grid(row=self._row, column=1, columnspan=1 if hint else 2, sticky="w", pady=5)
        if hint:
            ctk.CTkLabel(self.body, text=hint, font=t.font(11), text_color=t.TEXT_MUTED, anchor="w").grid(
                row=self._row, column=2, sticky="w", padx=(8, 0))
        self._row += 1
        return w

    def entry(self, value: str = "", width: int = 90):
        def make(parent):
            e = ctk.CTkEntry(parent, width=width, height=30, corner_radius=t.RADIUS,
                             fg_color=t.SURFACE_3, border_color=t.BORDER, font=t.mono(12))
            if value:
                e.insert(0, value)
            e.bind("<KeyRelease>", lambda _e: self.refresh())
            return e
        return make

    def say(self, text: str, error: bool = False):
        self.status.configure(text=text, text_color=t.ERROR if error else t.TEXT_DIM)
        self.ok.configure(state="disabled" if error else "normal")

    def refresh(self):
        raise NotImplementedError

    def _run(self):
        if self.ok.cget("state") != "disabled":
            self.run()

    def run(self):
        raise NotImplementedError

    def close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.editor._dialog = None
        self.destroy()


class OffsetDialog(_Dialog):
    """Copies of the selected block, each moved along by a number of pins and
    buttons: one encoder on D19/D20 → B19/B20 becomes D21/D22 → B21/B22, …"""

    def __init__(self, editor: RuleEditor, rules: list[Rule],
                 on_apply: Callable[[list[Rule], list[str]], None]):
        super().__init__(editor, "Duplicate with offset", "Add copies")
        self.rules, self.on_apply = rules, on_apply
        pin_off, btn_off = default_offsets(rules, editor.is_pin)
        self.copies = self.field("Copies", self.entry("1", 60))
        self.pin_off = self.field("Shift pins by", self.entry(str(pin_off), 60), "D19 → D21 is 2")
        self.btn_off = self.field("Shift buttons by", self.entry(str(btn_off), 60), "B19 → B21 is 2")
        self.vars = variables_in(rules, editor.is_pin)
        self.number_vars = tk.BooleanVar(value=True)
        if self.vars:
            example = self.vars[0]
            cb = ctk.CTkCheckBox(self.body, text=f"Number variables too ({example} → next)",
                                 variable=self.number_vars, command=self.refresh, font=t.font(12),
                                 fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM,
                                 checkbox_width=18, checkbox_height=18)
            cb.grid(row=self._row, column=0, columnspan=3, sticky="w", pady=(6, 0))
            self._row += 1
        self.refresh()
        self.copies.focus_set()
        self.copies.select_range(0, "end")

    def _build(self) -> tuple[list[Rule], list[str]] | str:
        copies, pin_off, btn_off = (_int(w.get()) for w in (self.copies, self.pin_off, self.btn_off))
        if copies is None or not 1 <= copies <= 64:
            return "Copies must be 1-64"
        if pin_off is None or btn_off is None:
            return "Offsets must be whole numbers"
        number_vars = bool(self.vars) and self.number_vars.get()
        out = shifted_copies(self.rules, copies, pin_off, btn_off, number_vars, self.editor.is_pin)
        new_vars = variables_in(out, self.editor.is_pin) if number_vars else []
        return out, new_vars

    def refresh(self):
        built = self._build()
        if isinstance(built, str):
            self.say(built, error=True)
            return
        out, new_vars = built
        k = len(self.rules)
        lines = [f"Adds {_n(len(out), 'rule')} below the selection.",
                 f"First copy: {_refs(out[:k])}"]
        if len(out) > k:
            lines.append(f"Last copy: {_refs(out[-k:])}")
        board = set(self.editor.board_pins())
        off_board = sorted({x for r in out for x in (r.input, *r.inputs)
                            if x and x not in board and self.editor.is_pin(x)}, key=pin_order)
        too_high = [x for r in out for x in (r.output, r.cw, r.ccw)
                    if x and x.startswith("B") and x[1:].isdigit() and int(x[1:]) > 127]
        if off_board:
            lines.append(f"Not on this board: {', '.join(off_board[:6])}")
        if too_high:
            lines.append("Buttons only go up to B127")
        if new_vars:
            lines.append(f"Creates variables that don't exist yet: {', '.join(new_vars[:6])}")
        self.say("\n".join(lines))

    def run(self):
        built = self._build()
        if isinstance(built, str):
            return
        self.close()
        self.on_apply(*built)


class RangeDialog(_Dialog):
    """One rule per pin over a range: D1-D16 → B1-B16 in one go."""

    def __init__(self, editor: RuleEditor, on_apply: Callable[[list[Rule]], None]):
        super().__init__(editor, "Add a range of rules", "Add rules")
        self.on_apply = on_apply
        model = editor.model
        self.pins = sorted(editor.board_pins(), key=pin_order)
        used = model.used_inputs()
        first = next_free(self.pins, used) or (self.pins[0] if self.pins else "")
        last = first
        # Default to the run of free pins that starts there.
        if first:
            for p in self.pins[self.pins.index(first):]:
                if p in used or p.rstrip("0123456789") != first.rstrip("0123456789"):
                    break
                last = p
        button = next_free(BUTTON_REFS, model.used_outputs()) or "B1"

        self.labels = {RULE_TYPE_LABELS[k]: k for k in RANGE_TYPES}
        self.kind = self.field("Rule type", lambda p: ctk.CTkSegmentedButton(
            p, values=list(self.labels), command=lambda _v: self.refresh(), font=t.font(12),
            selected_color=t.ACCENT, selected_hover_color=t.ACCENT_HOVER))
        self.kind.set(RULE_TYPE_LABELS["MAP"])
        self.first = self.field("From pin", self.entry(first))
        self.last = self.field("To pin", self.entry(last))
        self.button = self.field("First button", self.entry(button, 60), "the rest count up from it")
        self.refresh()
        self.first.focus_set()

    def _build(self) -> list[Rule] | str:
        pins = pin_range(self.pins, self.first.get(), self.last.get())
        if not pins:
            return "Pick two pins with the same letter on this board, like D1 and D16"
        start = _int(self.button.get().lstrip("Bb"))
        if start is None or start < 1:
            return "The first button is B1 to B127"
        if start + len(pins) - 1 > 127:
            return f"{len(pins)} buttons from B{start} would pass B127"
        return range_rules(self.labels[self.kind.get()], pins, start)

    def refresh(self):
        built = self._build()
        if isinstance(built, str):
            self.say(built, error=True)
            return
        a, b = built[0], built[-1]
        lines = [f"{_n(len(built), 'rule')}: {a.input} → {a.output}"
                 + (f"  …  {b.input} → {b.output}" if len(built) > 1 else "")]
        usage = self.editor.model.pin_usage()
        taken = [r.input for r in built if r.input in usage]
        if taken:
            lines.append(f"Already used by other rules: {', '.join(taken[:8])}")
        buttons = self.editor.model.used_outputs()
        dup = [r.output for r in built if r.output in buttons]
        if dup:
            lines.append(f"Already assigned: {', '.join(dup[:8])}")
        self.say("\n".join(lines))

    def run(self):
        built = self._build()
        if isinstance(built, str):
            return
        self.close()
        self.on_apply(built)


class FindDialog(_Dialog):
    """Rename a pin, button, variable or axis across the rules. Stays open
    (not modal) so a run of renames needs one open."""

    def __init__(self, editor: RuleEditor):
        super().__init__(editor, "Find and replace", "Replace all", modal=False)
        self.find = self.field("Find", self.entry("", 160))
        self.repl = self.field("Replace with", self.entry("", 160))
        self.whole = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.body, text="Whole names only (D1 never matches D10)", variable=self.whole,
                        command=self.refresh, font=t.font(12), fg_color=t.ACCENT,
                        hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM,
                        checkbox_width=18, checkbox_height=18).grid(
            row=self._row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._row += 1
        self.scope = None
        if len(editor.selected_indices()) > 1:
            self.scope = self.field("In", lambda p: ctk.CTkSegmentedButton(
                p, values=["Selected rules", "All rules"], command=lambda _v: self.refresh(),
                font=t.font(12), selected_color=t.ACCENT, selected_hover_color=t.ACCENT_HOVER))
            self.scope.set("Selected rules")
        self.refresh()
        self.find.focus_set()

    def _indices(self) -> list[int]:
        if self.scope is not None and self.scope.get() == "Selected rules":
            return self.editor.selected_indices()
        return list(range(len(self.editor.model)))

    def refresh(self):
        find = self.find.get().strip()
        if not find:
            self.say("Type a pin, button, variable or axis name", error=True)
            return
        model = self.editor.model
        fields, rules = count_matches([model[i] for i in self._indices()], find, self.whole.get())
        if not fields:
            self.say(f"No rule uses {find}", error=True)
            return
        self.say(f"{_n(fields, 'field')} in {_n(rules, 'rule')}")

    def run(self):
        find, repl = self.find.get().strip(), self.repl.get().strip()
        idx = self._indices()
        model = self.editor.model
        fields, _ = count_matches([model[i] for i in idx], find, self.whole.get())
        self.editor.replace_rules(idx, [replace_refs(model[i], find, repl, self.whole.get()) for i in idx])
        self.say(f"Replaced {_n(fields, 'field')}. Ctrl+Z on the rules undoes it.")
        self.ok.configure(state="disabled")
