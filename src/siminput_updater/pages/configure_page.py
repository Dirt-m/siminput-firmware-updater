from __future__ import annotations

import re
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING

import customtkinter as ctk

from .. import ui_theme as t
from ..config_model import (
    AXIS_SLOT_LABELS,
    Axis,
    BoolVar,
    Config,
    DeviceSettings,
    ValidationError,
    validate,
)
from ..widgets.live_panel import LivePanel
from ..widgets.rule_editor import RuleEditor

if TYPE_CHECKING:
    from ..app import App


def _entry(master, **kw):
    opts = dict(height=34, corner_radius=t.RADIUS, fg_color=t.SURFACE_3, border_color=t.BORDER, font=t.font(13))
    opts.update(kw)
    return ctk.CTkEntry(master, **opts)


def _switch(master, text, command=None):
    return ctk.CTkSwitch(
        master, text=text, command=command, font=t.font(12),
        progress_color=t.ACCENT, text_color=t.TEXT_DIM,
    )


def _check(master, text, command=None):
    return ctk.CTkCheckBox(
        master, text=text, command=command, font=t.font(12),
        fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM,
        checkbox_width=20, checkbox_height=20,
    )


def _option(master, values, command=None, width=160):
    return ctk.CTkOptionMenu(
        master, values=values, command=command, width=width, height=32,
        corner_radius=t.RADIUS, fg_color=t.SURFACE_3, button_color=t.SURFACE_3,
        button_hover_color=t.HOVER, text_color=t.TEXT, font=t.font(12),
        dropdown_fg_color=t.SURFACE_2, dropdown_hover_color=t.HOVER, dropdown_text_color=t.TEXT,
    )


VALIDATE_DELAY_MS = 250   # debounce after the last keystroke
MAX_PROBLEM_ROWS = 8

_PATH_RE = re.compile(r"^(\w+)(?:\[(\d+)\])?(?:\.(.+))?$")

RULE_FIELD_LABELS = {
    "input": "Input", "inputs": "Inputs", "output": "Output", "cw": "CW", "ccw": "CCW",
    "axis": "Axis", "pulse_ms": "Pulse", "delay_ms": "Delay", "step": "Step",
    "divisor": "Steps/detent", "type": "Type",
}


def parse_path(path: str) -> tuple[str, int | None, str | None]:
    """"rules[3].inputs[1]" → ("rules", 3, "inputs[1]"); "device.pid" →
    ("device", None, "pid"); "config" → ("config", None, None)."""
    m = _PATH_RE.match(path)
    if not m:
        return path, None, None
    section, idx, field = m.groups()
    return section, (int(idx) if idx is not None else None), field


def describe_problem(err: ValidationError) -> tuple[str, str]:
    """(where, what) for the problems panel, numbered from 1 like the cards."""
    section, idx, field = parse_path(err.path)
    if section == "rules" and idx is not None:
        base = field.split("[")[0] if field else ""
        label = RULE_FIELD_LABELS.get(base, base)
        return (f"Rule {idx + 1}" + (f" · {label}" if label else ""), err.message)
    if section == "bools" and idx is not None:
        return (f"Variable {idx + 1}", err.message)
    if section == "axes" and idx is not None:
        return (f"Axis {idx + 1}", err.message)
    if section == "device":
        return ("Device", err.message)
    return ("Config", err.message)


def _slider(master, **kw):
    opts = dict(progress_color=t.ACCENT, button_color=t.ACCENT, button_hover_color=t.ACCENT_HOVER,
                fg_color=t.SURFACE_3)
    opts.update(kw)
    return ctk.CTkSlider(master, **opts)


class ConfigurePage(ctk.CTkFrame):
    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._dirty = False
        # Comment entries (id-less bools/axes from hand-written configs) are
        # not editable in the UI; they're held here and re-appended on save so
        # a round trip never destroys them.
        self._bool_comments: list[BoolVar] = []
        self._axis_comments: list[Axis] = []
        self._config_extra: dict = {}
        self._device_extra: dict = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._validate_after: str | None = None
        self._problems: list[ValidationError] = []

        self._create_toolbar()
        self._create_problems_panel()
        self._create_tabs()

        # Live pins/buttons under the editor, fed by the shared monitor while
        # this page is visible; the rule editor also gets the frames for Learn.
        self.live_panel = LivePanel(self)
        self.live_panel.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.app.register_theme_listener(self.live_panel.retheme)

        self.app.register_connection_listener(self._on_connection)
        self._refresh_connection_state()
        self.app.bind("<Control-s>", self._on_ctrl_s, add="+")

    def _on_ctrl_s(self, _event=None):
        if self.winfo_viewable() and self._save_btn.cget("state") == "normal":
            self._save_config()

    # ----------------------------------------------------------- toolbar

    def _create_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS, height=58)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(5, weight=1)

        self._read_btn = t.primary_button(bar, "Read from device", self._read_config, width=150)
        self._read_btn.grid(row=0, column=0, padx=(14, 8), pady=12)

        self._save_btn = t.primary_button(bar, "Save to device", self._save_config, width=140)
        self._save_btn.grid(row=0, column=1, padx=0, pady=12)

        ctk.CTkFrame(bar, width=1, height=26, fg_color=t.BORDER).grid(row=0, column=2, padx=14)

        self._import_btn = t.ghost_button(bar, "Import JSON", self._import_json, width=110)
        self._import_btn.grid(row=0, column=3, padx=(0, 8), pady=12)
        self._export_btn = t.ghost_button(bar, "Export JSON", self._export_json, width=110)
        self._export_btn.grid(row=0, column=4, pady=12)

        self.dirty_label = ctk.CTkLabel(bar, text="", font=t.font(12, "bold"), text_color=t.WARN, anchor="e")
        self.dirty_label.grid(row=0, column=5, padx=16, pady=12, sticky="e")

    def _create_tabs(self):
        self._tab_names = ("Device", "Variables", "Axes", "Rules")

        # Tab strip — a flush row of buttons sitting directly above the content.
        strip = ctk.CTkFrame(self, fg_color="transparent")
        strip.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self._tab_buttons: dict[str, ctk.CTkButton] = {}
        for i, name in enumerate(self._tab_names):
            btn = ctk.CTkButton(
                strip, text=name, width=110, height=36, corner_radius=t.RADIUS,
                fg_color=t.SURFACE_2, hover_color=t.HOVER, text_color=t.TEXT_DIM,
                font=t.font(13, "bold"),
                command=lambda n=name: self._show_tab(n),
            )
            btn.grid(row=0, column=i, padx=(0 if i == 0 else 2, 0))
            self._tab_buttons[name] = btn

        # Content area — one frame per tab, swapped via grid/grid_remove.
        self._tab_host = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        self._tab_host.grid(row=3, column=0, sticky="nsew")
        self._tab_host.grid_columnconfigure(0, weight=1)
        self._tab_host.grid_rowconfigure(0, weight=1)

        self._tab_frames: dict[str, ctk.CTkFrame] = {}
        for name in self._tab_names:
            frame = ctk.CTkFrame(self._tab_host, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
            frame.grid_remove()
            self._tab_frames[name] = frame

        self._build_device_tab()
        self._build_variables_tab()
        self._build_axes_tab()
        self._build_rules_tab()

        self._current_tab: str | None = None
        self._show_tab("Device")

    def _show_tab(self, name: str):
        if self._current_tab == name:
            return
        for key, btn in self._tab_buttons.items():
            if key == name:
                btn.configure(fg_color=t.ACCENT, text_color="#FFFFFF")
            else:
                btn.configure(fg_color=t.SURFACE_2, text_color=t.TEXT_DIM)
        for key, frame in self._tab_frames.items():
            if key == name:
                frame.grid()
            else:
                frame.grid_remove()
        self._current_tab = name
        if name == "Rules":
            # Axes may have been added or renamed since the rule cards were
            # built — refresh the AXIS_* dropdowns so they can be selected.
            self.rule_editor.refresh_axis_menus()

    # -------------------------------------------------------- problems panel

    def _create_problems_panel(self):
        self._problems_panel = ctk.CTkFrame(
            self, fg_color=t.ERROR_SOFT, corner_radius=t.RADIUS, border_width=1, border_color=t.ERROR)
        self._problems_panel.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._problems_panel.grid_columnconfigure(0, weight=1)
        self._problems_panel.grid_remove()
        self._problems_title = ctk.CTkLabel(
            self._problems_panel, text="", anchor="w", font=t.font(12, "bold"), text_color=t.ERROR)
        self._problems_title.grid(row=0, column=0, padx=14, pady=(8, 3), sticky="w")
        self._problem_rows: list[ctk.CTkLabel] = []

    def _show_problems(self, errors: list[ValidationError]):
        self._problems = list(errors)
        for row in self._problem_rows:
            row.destroy()
        self._problem_rows = []
        if not errors:
            self._problems_panel.grid_remove()
            return
        n = len(errors)
        self._problems_title.configure(text=f"{n} problem{'s' if n != 1 else ''} to fix before saving")
        for i, err in enumerate(errors[:MAX_PROBLEM_ROWS]):
            where, what = describe_problem(err)
            row = ctk.CTkLabel(
                self._problems_panel, text=f"{where}:  {what}", anchor="w", cursor="hand2",
                font=t.font(12), text_color=t.TEXT, height=18)
            row.grid(row=i + 1, column=0, padx=14, pady=(0, 8 if i == n - 1 else 1), sticky="w")
            row.bind("<Button-1>", lambda _e, e=err: self._goto_problem(e))
            self._problem_rows.append(row)
        if n > MAX_PROBLEM_ROWS:
            more = ctk.CTkLabel(self._problems_panel, text=f"+{n - MAX_PROBLEM_ROWS} more", anchor="w",
                                font=t.font(12), text_color=t.TEXT_DIM, height=18)
            more.grid(row=MAX_PROBLEM_ROWS + 1, column=0, padx=14, pady=(0, 8), sticky="w")
            self._problem_rows.append(more)
        self._problems_panel.grid()

    def _goto_problem(self, err: ValidationError):
        section, idx, field = parse_path(err.path)
        if section == "rules":
            self._show_tab("Rules")
            if idx is not None:
                self.rule_editor.reveal(idx, field)
        elif section == "bools":
            self._show_tab("Variables")
            if idx is not None and idx < len(self._var_widgets):
                self._var_widgets[idx]["name"].focus_set()
        elif section == "axes":
            self._show_tab("Axes")
            if idx is not None and idx < len(self._axis_widgets):
                self._axis_widgets[idx]["name"].focus_set()
        elif section == "device":
            self._show_tab("Device")
            (self.pid_entry if field == "pid" else self.name_entry).focus_set()

    # ------------------------------------------------------- live validation

    def _schedule_validate(self):
        if self._validate_after is not None:
            self.after_cancel(self._validate_after)
        self._validate_after = self.after(VALIDATE_DELAY_MS, self._validate_now)

    def _validate_now(self) -> list[ValidationError]:
        """Validate the whole editor state, outline the offending fields and
        refresh the problems panel. Returns the errors (empty when clean)."""
        if self._validate_after is not None:
            try:
                self.after_cancel(self._validate_after)
            except Exception:
                pass
            self._validate_after = None
        errors = [ValidationError(path, msg) for path, msg in self._input_errors()]
        if not errors:
            config = self._collect_config()
            errors = validate(config, board_map=self.app.board_map, pins=self.app.device_pins)

        rule_errors: dict[int, dict[str, str]] = {}
        bad_vars: set[int] = set()
        bad_axes: set[int] = set()
        for err in errors:
            section, idx, field = parse_path(err.path)
            if section == "rules" and idx is not None:
                rule_errors.setdefault(idx, {})[field or "type"] = err.message
            elif section == "bools" and idx is not None:
                bad_vars.add(idx)
            elif section == "axes" and idx is not None and field == "id":
                bad_axes.add(idx)
        self.rule_editor.set_errors(rule_errors)
        for i, w in enumerate(self._var_widgets):
            w["name"].configure(border_color=t.ERROR if i in bad_vars else t.BORDER)
        for i, w in enumerate(self._axis_widgets):
            w["name"].configure(border_color=t.ERROR if i in bad_axes else t.BORDER)
        self._show_problems(errors)
        return errors

    # -- Device tab --

    def _build_device_tab(self):
        tab = self._tab_frames["Device"]
        tab.grid_columnconfigure(1, weight=1)

        labels = ["Device name", "USB Product ID (hex)", "Button debounce", "Keep-alive interval"]
        for i, label in enumerate(labels):
            ctk.CTkLabel(tab, text=label, anchor="w", font=t.font(13), text_color=t.TEXT_DIM).grid(
                row=i, column=0, padx=(16, 18), pady=12, sticky="w")

        self.name_entry = _entry(tab, placeholder_text="SimInput Button Box")
        self.name_entry.grid(row=0, column=1, padx=(0, 16), pady=12, sticky="ew")
        self.name_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        self.pid_entry = _entry(tab, placeholder_text="F000")
        self.pid_entry.grid(row=1, column=1, padx=(0, 16), pady=12, sticky="ew")
        self.pid_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        deb = ctk.CTkFrame(tab, fg_color="transparent")
        deb.grid(row=2, column=1, padx=(0, 16), pady=12, sticky="ew")
        deb.grid_columnconfigure(0, weight=1)
        self.debounce_slider = _slider(deb, from_=0, to=100, number_of_steps=100, command=self._on_debounce)
        self.debounce_slider.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.debounce_value = ctk.CTkLabel(deb, text="10 ms", width=52, font=t.mono(12), text_color=t.TEXT)
        self.debounce_value.grid(row=0, column=1)

        refresh = ctk.CTkFrame(tab, fg_color="transparent")
        refresh.grid(row=3, column=1, padx=(0, 16), pady=12, sticky="ew")
        self.refresh_entry = _entry(refresh, placeholder_text="1.0", width=100)
        self.refresh_entry.grid(row=0, column=0, sticky="w")
        self.refresh_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())
        self.refresh_disable = _check(refresh, "Disabled", command=self._toggle_refresh)
        self.refresh_disable.grid(row=0, column=1, padx=(14, 0))

    def _on_debounce(self, value):
        self.debounce_value.configure(text=f"{int(value)} ms")
        self._mark_dirty()

    def _toggle_refresh(self):
        self.refresh_entry.configure(state="disabled" if self.refresh_disable.get() else "normal")
        self._mark_dirty()

    # -- Variables tab --

    def _build_variables_tab(self):
        tab = self._tab_frames["Variables"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.vars_scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.vars_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.vars_scroll.grid_columnconfigure(0, weight=1)
        self._var_widgets: list[dict] = []
        self._vars_empty = ctk.CTkLabel(
            self.vars_scroll, text="No variables yet. Variables hold toggle/latch state across rules.",
            font=t.font(12), text_color=t.TEXT_MUTED)
        self._vars_empty.grid(row=0, column=0, pady=20)

        t.primary_button(tab, "+ Add variable", self._add_var, width=140).grid(
            row=2, column=0, padx=8, pady=10, sticky="w")

    def _add_var(self, bv: BoolVar | None = None):
        self._vars_empty.grid_remove()
        bv = bv or BoolVar()
        frame = ctk.CTkFrame(self.vars_scroll, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
        frame.grid(sticky="ew", padx=4, pady=4)
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="Name", width=44, font=t.font(12), text_color=t.TEXT_DIM).grid(
            row=0, column=0, padx=(12, 6), pady=10)
        name_entry = _entry(frame, placeholder_text="e.g. TOGGLE1")
        name_entry.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        if bv.id:
            name_entry.insert(0, bv.id)
        name_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        default_switch = _switch(frame, "Starts on", command=self._mark_dirty)
        default_switch.grid(row=0, column=2, padx=12, pady=10)
        if bv.default:
            default_switch.select()

        store_cb = _check(frame, "Remember", command=self._mark_dirty)
        store_cb.grid(row=0, column=3, padx=12, pady=10)
        if bv.store:
            store_cb.select()

        dup_btn = ctk.CTkButton(
            frame, text="❐", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.HOVER, text_color=t.TEXT_DIM,
            command=lambda f=frame: self._duplicate_var(f))
        dup_btn.grid(row=0, column=4, padx=(6, 0), pady=10)
        del_btn = ctk.CTkButton(
            frame, text="✕", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.ERROR_SOFT, text_color=t.ERROR,
            command=lambda f=frame: self._remove_var(f))
        del_btn.grid(row=0, column=5, padx=(0, 12), pady=10)

        self._var_widgets.append({"frame": frame, "name": name_entry, "default": default_switch,
                                  "store": store_cb, "extra": dict(bv.extra)})
        self._mark_dirty()

    def _var_from_widgets(self, w: dict) -> BoolVar:
        return BoolVar(id=w["name"].get().strip(), default=bool(w["default"].get()),
                       store=bool(w["store"].get()), extra=dict(w.get("extra", {})))

    def _duplicate_var(self, frame):
        for w in self._var_widgets:
            if w["frame"] is frame:
                self._add_var(self._var_from_widgets(w))
                self._var_widgets[-1]["name"].focus_set()
                break

    def _remove_var(self, frame):
        for i, w in enumerate(self._var_widgets):
            if w["frame"] is frame:
                frame.destroy()
                self._var_widgets.pop(i)
                self._mark_dirty()
                break
        if not self._var_widgets:
            self._vars_empty.grid()

    # -- Axes tab --

    def _build_axes_tab(self):
        tab = self._tab_frames["Axes"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.axes_scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.axes_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.axes_scroll.grid_columnconfigure(0, weight=1)
        self._axis_widgets: list[dict] = []
        self._axes_empty = ctk.CTkLabel(
            self.axes_scroll, text="No axes yet. Axes map analog values to HID joystick outputs.",
            font=t.font(12), text_color=t.TEXT_MUTED)
        self._axes_empty.grid(row=0, column=0, pady=20)

        t.primary_button(tab, "+ Add axis", self._add_axis, width=120).grid(
            row=2, column=0, padx=8, pady=10, sticky="w")

    def _add_axis(self, ax: Axis | None = None):
        self._axes_empty.grid_remove()
        ax = ax or Axis()
        frame = ctk.CTkFrame(self.axes_scroll, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
        frame.grid(sticky="ew", padx=4, pady=4)
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="Name", width=44, font=t.font(12), text_color=t.TEXT_DIM).grid(
            row=0, column=0, padx=(12, 6), pady=10)
        name_entry = _entry(frame, placeholder_text="e.g. AX1", width=120)
        name_entry.grid(row=0, column=1, padx=6, pady=10, sticky="w")
        if ax.id:
            name_entry.insert(0, ax.id)
        name_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        ctk.CTkLabel(frame, text="Output", font=t.font(12), text_color=t.TEXT_DIM).grid(
            row=0, column=2, padx=(14, 6), pady=10)
        # Explicit label→value map, so the slot survives any future label
        # wording instead of being re-parsed out of the display string.
        slot_map: dict[str, int | str] = {}
        for k, v in AXIS_SLOT_LABELS.items():
            label = f"{v} ({k})" if isinstance(k, int) else v
            slot_map[label] = k
        current_slot = next(
            (label for label, val in slot_map.items() if val == ax.output),
            f"{AXIS_SLOT_LABELS[1]} (1)",
        )
        slot_menu = _option(frame, list(slot_map), command=lambda v: self._mark_dirty(), width=140)
        slot_menu.set(current_slot)
        slot_menu.grid(row=0, column=3, padx=6, pady=10)

        dup_btn = ctk.CTkButton(
            frame, text="❐", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.HOVER, text_color=t.TEXT_DIM,
            command=lambda f=frame: self._duplicate_axis(f))
        dup_btn.grid(row=0, column=4, padx=(6, 0), pady=10, sticky="e")
        del_btn = ctk.CTkButton(
            frame, text="✕", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.ERROR_SOFT, text_color=t.ERROR,
            command=lambda f=frame: self._remove_axis(f))
        del_btn.grid(row=0, column=5, padx=(0, 12), pady=10, sticky="e")

        ctk.CTkLabel(frame, text="Default", font=t.font(12), text_color=t.TEXT_DIM).grid(
            row=1, column=0, padx=(12, 6), pady=10)
        # No number_of_steps: a stepped slider snaps set() values to multiples
        # of 65535/steps, silently corrupting loaded defaults on every
        # read/save round trip (30000 became 29951). The authoritative value
        # lives in the widget dict ("default_val"); the slider is input only.
        default_slider = _slider(frame, from_=0, to=65535)
        default_slider.set(ax.default)
        default_slider.grid(row=1, column=1, columnspan=2, padx=6, pady=10, sticky="ew")
        default_lbl = ctk.CTkLabel(frame, text=str(ax.default), width=56, font=t.mono(11), text_color=t.TEXT)
        default_lbl.grid(row=1, column=3, padx=6, pady=10, sticky="w")

        store_cb = _check(frame, "Remember", command=self._mark_dirty)
        store_cb.grid(row=1, column=4, padx=12, pady=10)
        if ax.store:
            store_cb.select()
        bl_cb = _check(frame, "Backlight", command=self._mark_dirty)
        bl_cb.grid(row=1, column=5, padx=12, pady=10)
        if ax.backlight:
            bl_cb.select()

        widgets = {
            "frame": frame, "name": name_entry, "slot": slot_menu, "slot_map": slot_map,
            "default": default_slider, "default_val": int(ax.default),
            "default_lbl": default_lbl, "store": store_cb, "backlight": bl_cb,
            "extra": dict(ax.extra),
        }
        default_slider.configure(command=lambda v, w=widgets: self._on_axis_default(v, w))
        self._axis_widgets.append(widgets)
        self._mark_dirty()

    def _on_axis_default(self, value, widgets):
        widgets["default_val"] = round(value)
        widgets["default_lbl"].configure(text=str(widgets["default_val"]))
        self._mark_dirty()

    def _axis_from_widgets(self, w: dict) -> Axis:
        return Axis(
            id=w["name"].get().strip(), output=w["slot_map"].get(w["slot"].get(), 1),
            default=w["default_val"], store=bool(w["store"].get()),
            backlight=bool(w["backlight"].get()), extra=dict(w.get("extra", {})),
        )

    def _duplicate_axis(self, frame):
        for w in self._axis_widgets:
            if w["frame"] is frame:
                self._add_axis(self._axis_from_widgets(w))
                self._axis_widgets[-1]["name"].focus_set()
                break

    def _remove_axis(self, frame):
        for i, w in enumerate(self._axis_widgets):
            if w["frame"] is frame:
                frame.destroy()
                self._axis_widgets.pop(i)
                self._mark_dirty()
                break
        if not self._axis_widgets:
            self._axes_empty.grid()

    # -- Rules tab --

    def _build_rules_tab(self):
        tab = self._tab_frames["Rules"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.rule_editor = RuleEditor(tab, self)
        self.rule_editor.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

    # ----------------------------------------------------- config sync

    def _input_errors(self) -> list[tuple[str, str]]:
        """Malformed free-text fields, as (path, message). Reported instead of
        being silently replaced with defaults — a typo'd PID must not save
        as 0xF000."""
        errors = []
        pid_text = self.pid_entry.get().strip()
        if pid_text:
            try:
                int(pid_text, 16)
            except ValueError:
                errors.append(("device.pid", f"USB Product ID '{pid_text}' is not valid hex"))
        if not self.refresh_disable.get():
            refresh_text = self.refresh_entry.get().strip()
            if refresh_text:
                try:
                    float(refresh_text)
                except ValueError:
                    errors.append(("device.inactivity_refresh",
                                   f"Keep-alive interval '{refresh_text}' is not a number"))
        return errors

    def _collect_config(self) -> Config:
        name = self.name_entry.get() or "SimInput Button Box"
        pid_text = self.pid_entry.get().strip()
        try:
            pid = int(pid_text, 16) if pid_text else 0xF000
        except ValueError:
            pid = 0xF000  # _input_errors() blocks the save before this matters

        debounce = int(self.debounce_slider.get())
        if self.refresh_disable.get():
            refresh: float | bool = False
        else:
            try:
                refresh = float(self.refresh_entry.get() or "1.0")
            except ValueError:
                refresh = 1.0

        device = DeviceSettings(name=name, pid=pid, debounce_ms=debounce,
                                inactivity_refresh=refresh, extra=dict(self._device_extra))

        bools = [self._var_from_widgets(w) for w in self._var_widgets]
        bools += [b for b in self._bool_comments]

        axes = [self._axis_from_widgets(w) for w in self._axis_widgets]
        axes += [a for a in self._axis_comments]

        rules = self.rule_editor.collect_rules()
        return Config(device=device, bools=bools, axes=axes, rules=rules,
                      extra=dict(self._config_extra))

    def _load_config(self, config: Config):
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, config.device.name)
        self.pid_entry.delete(0, "end")
        self.pid_entry.insert(0, f"{config.device.pid:04X}")
        self.debounce_slider.set(config.device.debounce_ms)
        self.debounce_value.configure(text=f"{config.device.debounce_ms} ms")

        # The firmware treats null the same as false (keep-alive disabled) —
        # both must load as "Disabled", not as the literal string "None" that
        # would later be coerced to 1.0 and silently re-enable keep-alive.
        if config.device.inactivity_refresh in (False, None):
            self.refresh_disable.select()
            self.refresh_entry.configure(state="normal")
            self.refresh_entry.delete(0, "end")
            self.refresh_entry.configure(state="disabled")
        else:
            self.refresh_disable.deselect()
            self.refresh_entry.configure(state="normal")
            self.refresh_entry.delete(0, "end")
            self.refresh_entry.insert(0, str(config.device.inactivity_refresh))

        self._config_extra = dict(config.extra)
        self._device_extra = dict(config.device.extra)

        for w in self._var_widgets:
            w["frame"].destroy()
        self._var_widgets.clear()
        self._bool_comments = [b for b in config.bools if b.comment]
        editable_bools = [b for b in config.bools if not b.comment]
        for bv in editable_bools:
            self._add_var(bv)
        if not editable_bools:
            self._vars_empty.grid()

        for w in self._axis_widgets:
            w["frame"].destroy()
        self._axis_widgets.clear()
        self._axis_comments = [a for a in config.axes if a.comment]
        editable_axes = [a for a in config.axes if not a.comment]
        for ax in editable_axes:
            self._add_axis(ax)
        if not editable_axes:
            self._axes_empty.grid()

        self.rule_editor.load_rules(config.rules)
        self._clear_dirty()
        self._validate_now()

    def _mark_dirty(self, *_args):
        if not self._dirty:
            self._dirty = True
            self.dirty_label.configure(text="Unsaved changes")
        self._schedule_validate()

    def _clear_dirty(self):
        self._dirty = False
        self.dirty_label.configure(text="")

    # ------------------------------------------------------- connection

    def _refresh_connection_state(self):
        if self.app.device.connected:
            self._read_btn.configure(state="normal", fg_color=t.ACCENT, text_color="#FFFFFF")
            self._save_btn.configure(state="normal", fg_color=t.ACCENT, text_color="#FFFFFF")
        else:
            for b in (self._read_btn, self._save_btn):
                b.configure(state="disabled", fg_color=t.SURFACE_3, text_color=t.TEXT_MUTED)

    def _on_connection(self, connected: bool):
        self._refresh_connection_state()
        self.live_panel.set_connected(connected)
        if not connected:
            self.rule_editor.cancel_learn()

    def _on_live(self, state: dict):
        self.live_panel.update(state)
        self.rule_editor.on_live_state(state)

    def on_show(self):
        self._refresh_connection_state()
        self.live_panel.set_connected(bool(self.app.device.connected))
        self.app.monitor.subscribe(self._on_live)
        self.app.monitor.acquire()

    def on_hide(self):
        self.rule_editor.cancel_learn()
        self.app.monitor.unsubscribe(self._on_live)
        self.app.monitor.release()

    # ----------------------------------------------------------- actions

    def _confirm_discard(self, what: str) -> bool:
        if not self._dirty:
            return True
        from tkinter import messagebox
        return messagebox.askyesno(
            "Unsaved changes",
            f"The configuration has unsaved changes.\n\n{what} will discard them — continue?",
        )

    def _read_config(self):
        if not self.app.device.connected:
            self.app.show_status("No device connected", "error")
            return
        if not self._confirm_discard("Reading from the device"):
            return

        def work(ctx):
            ctx.status("Requesting configuration from device…")
            ctx.log("get_config")
            data = self.app.device.get_config()
            ctx.check_cancel()
            ctx.log("Parsing configuration")
            return Config.from_dict(data)

        def on_success(config):
            self._load_config(config)
            self.app.show_status("Configuration loaded from device", "success")

        self.app.run_operation("Reading configuration", work, on_success=on_success, success_message="Loaded")

    def _save_config(self):
        errors = self._validate_now()
        if errors:
            n = len(errors)
            self.app.show_status(
                f"Fix the {n} problem{'s' if n != 1 else ''} listed above before saving", "error", 6000)
            self._goto_problem(errors[0])
            return
        config = self._collect_config()
        if not self.app.device.connected:
            self.app.show_status("No device connected", "error")
            return

        payload = config.to_dict()
        device = self.app.device
        old_info = device.info
        port_name = old_info.port if old_info else ""
        identity_changed = bool(old_info) and (
            payload.get("device", {}).get("name") != old_info.name
            or payload.get("device", {}).get("pid") != old_info.pid
        )

        def work(ctx):
            ctx.status("Writing configuration to device…")
            ctx.log("set_config")
            ctx.check_cancel()
            resp = device.set_config(payload)
            ctx.log("Configuration written")
            # The device reboots to apply the config. Ride out the restart
            # inside the operation so the scan loop (paused while an operation
            # runs) never sees the drop and reports a spurious disconnect.
            if isinstance(resp, dict) and resp.get("rebooting") and port_name:
                ctx.status("Device is rebooting to apply the configuration…")
                device.handle_reboot_disconnect()
                info = device.wait_for_reconnect(port_name)
                ctx.log(f"Device reconnected: {info.name}")
                full = None
                try:
                    full = device.get_info()
                except Exception:
                    pass
                return info, full
            return None

        def on_success(result):
            self._clear_dirty()
            if result is not None:
                info, full = result
                self.app.notify_connected(info, full)
            self.app.show_status("Configuration saved to device", "success")
            if identity_changed:
                self.app.show_status(
                    "USB name/PID changes take effect after unplugging the device",
                    "info", 8000,
                )

        self.app.run_operation("Saving configuration", work, on_success=on_success, success_message="Saved")

    def _import_json(self):
        if not self._confirm_discard("Importing a file"):
            return
        path = filedialog.askopenfilename(
            title="Import Configuration",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            config = Config.from_json(Path(path).read_text(encoding="utf-8"))
            self._load_config(config)
            self._mark_dirty()
            self.app.show_status(f"Imported {Path(path).name}", "success")
        except Exception as e:
            self.app.show_status(f"Import failed: {e}", "error", 6000)

    def _export_json(self):
        config = self._collect_config()
        path = filedialog.asksaveasfilename(
            title="Export Configuration", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(config.to_json(), encoding="utf-8")
            self.app.show_status(f"Exported to {Path(path).name}", "success")
        except Exception as e:
            self.app.show_status(f"Export failed: {e}", "error", 6000)
