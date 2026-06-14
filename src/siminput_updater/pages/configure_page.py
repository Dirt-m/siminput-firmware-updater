from __future__ import annotations

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
    validate,
)
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


def _slider(master, **kw):
    opts = dict(progress_color=t.ACCENT, button_color=t.ACCENT, button_hover_color=t.ACCENT_HOVER,
                fg_color=t.SURFACE_3)
    opts.update(kw)
    return ctk.CTkSlider(master, **opts)


class ConfigurePage(ctk.CTkFrame):
    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.config = Config()
        self._dirty = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._create_toolbar()
        self._create_tabs()

        self.app.register_connection_listener(lambda c: self._refresh_connection_state())
        self._refresh_connection_state()

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
        strip.grid(row=1, column=0, sticky="ew", pady=(12, 0))
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
        self._tab_host.grid(row=2, column=0, sticky="nsew")
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

        ctk.CTkLabel(
            tab, text="Variables hold on/off state that rules can read and write — handy for latches and modes.",
            font=t.font(12), text_color=t.TEXT_MUTED, anchor="w",
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

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

        del_btn = ctk.CTkButton(
            frame, text="✕", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.ERROR_SOFT, text_color=t.ERROR,
            command=lambda f=frame: self._remove_var(f))
        del_btn.grid(row=0, column=4, padx=(6, 12), pady=10)

        self._var_widgets.append({"frame": frame, "name": name_entry, "default": default_switch, "store": store_cb})
        self._mark_dirty()

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

        ctk.CTkLabel(
            tab, text="Axes map analog values to HID joystick outputs (X, Y, Z, …) or device backlight.",
            font=t.font(12), text_color=t.TEXT_MUTED, anchor="w",
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

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
        slot_options = [f"{v} ({k})" if isinstance(k, int) else v for k, v in AXIS_SLOT_LABELS.items()]
        if isinstance(ax.output, int):
            current_slot = f"{AXIS_SLOT_LABELS.get(ax.output, 'X')} ({ax.output})"
        else:
            current_slot = AXIS_SLOT_LABELS.get(ax.output, "Backlight Only")
        slot_menu = _option(frame, slot_options, command=lambda v: self._mark_dirty(), width=140)
        slot_menu.set(current_slot)
        slot_menu.grid(row=0, column=3, padx=6, pady=10)

        del_btn = ctk.CTkButton(
            frame, text="✕", width=30, height=30, corner_radius=t.RADIUS,
            fg_color="transparent", hover_color=t.ERROR_SOFT, text_color=t.ERROR,
            command=lambda f=frame: self._remove_axis(f))
        del_btn.grid(row=0, column=4, columnspan=2, padx=(6, 12), pady=10, sticky="e")

        ctk.CTkLabel(frame, text="Default", font=t.font(12), text_color=t.TEXT_DIM).grid(
            row=1, column=0, padx=(12, 6), pady=10)
        default_slider = _slider(frame, from_=0, to=65535, number_of_steps=256)
        default_slider.set(ax.default)
        default_slider.grid(row=1, column=1, columnspan=2, padx=6, pady=10, sticky="ew")
        default_lbl = ctk.CTkLabel(frame, text=str(ax.default), width=56, font=t.mono(11), text_color=t.TEXT)
        default_lbl.grid(row=1, column=3, padx=6, pady=10, sticky="w")
        default_slider.configure(command=lambda v, l=default_lbl: self._on_axis_default(v, l))

        store_cb = _check(frame, "Remember", command=self._mark_dirty)
        store_cb.grid(row=1, column=4, padx=12, pady=10)
        if ax.store:
            store_cb.select()
        bl_cb = _check(frame, "Backlight", command=self._mark_dirty)
        bl_cb.grid(row=1, column=5, padx=12, pady=10)
        if ax.backlight:
            bl_cb.select()

        self._axis_widgets.append({
            "frame": frame, "name": name_entry, "slot": slot_menu,
            "default": default_slider, "default_lbl": default_lbl, "store": store_cb, "backlight": bl_cb,
        })
        self._mark_dirty()

    def _on_axis_default(self, value, label):
        label.configure(text=str(int(value)))
        self._mark_dirty()

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

        ctk.CTkLabel(
            tab, text="Rules run top to bottom. A rule can read the output of any rule above it.",
            font=t.font(12), text_color=t.TEXT_MUTED, anchor="w",
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

        self.rule_editor = RuleEditor(tab, self)
        self.rule_editor.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

    # ----------------------------------------------------- config sync

    def _collect_config(self) -> Config:
        name = self.name_entry.get() or "SimInput Button Box"
        pid_text = self.pid_entry.get().strip()
        try:
            pid = int(pid_text, 16) if pid_text else 0xF000
        except ValueError:
            pid = 0xF000

        debounce = int(self.debounce_slider.get())
        if self.refresh_disable.get():
            refresh: float | bool = False
        else:
            try:
                refresh = float(self.refresh_entry.get() or "1.0")
            except ValueError:
                refresh = 1.0

        device = DeviceSettings(name=name, pid=pid, debounce_ms=debounce, inactivity_refresh=refresh)

        bools = [BoolVar(
            id=w["name"].get().strip(), default=bool(w["default"].get()), store=bool(w["store"].get()),
        ) for w in self._var_widgets]

        axes = []
        for w in self._axis_widgets:
            slot_text = w["slot"].get()
            if "Backlight" in slot_text:
                output: int | str = "BACKLIGHT"
            else:
                try:
                    output = int(slot_text.split("(")[1].rstrip(")"))
                except (IndexError, ValueError):
                    output = 1
            axes.append(Axis(
                id=w["name"].get().strip(), output=output, default=int(w["default"].get()),
                store=bool(w["store"].get()), backlight=bool(w["backlight"].get()),
            ))

        rules = self.rule_editor.collect_rules()
        return Config(device=device, bools=bools, axes=axes, rules=rules)

    def _load_config(self, config: Config):
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, config.device.name)
        self.pid_entry.delete(0, "end")
        self.pid_entry.insert(0, f"{config.device.pid:04X}")
        self.debounce_slider.set(config.device.debounce_ms)
        self.debounce_value.configure(text=f"{config.device.debounce_ms} ms")

        if config.device.inactivity_refresh is False:
            self.refresh_disable.select()
            self.refresh_entry.configure(state="normal")
            self.refresh_entry.delete(0, "end")
            self.refresh_entry.configure(state="disabled")
        else:
            self.refresh_disable.deselect()
            self.refresh_entry.configure(state="normal")
            self.refresh_entry.delete(0, "end")
            self.refresh_entry.insert(0, str(config.device.inactivity_refresh))

        for w in self._var_widgets:
            w["frame"].destroy()
        self._var_widgets.clear()
        for bv in config.bools:
            self._add_var(bv)
        if not config.bools:
            self._vars_empty.grid()

        for w in self._axis_widgets:
            w["frame"].destroy()
        self._axis_widgets.clear()
        for ax in config.axes:
            self._add_axis(ax)
        if not config.axes:
            self._axes_empty.grid()

        self.rule_editor.load_rules(config.rules)
        self._clear_dirty()

    def _mark_dirty(self, *_args):
        if not self._dirty:
            self._dirty = True
            self.dirty_label.configure(text="Unsaved changes")

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

    def on_show(self):
        self._refresh_connection_state()

    # ----------------------------------------------------------- actions

    def _read_config(self):
        if not self.app.device.connected:
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
        config = self._collect_config()
        errors = validate(config, board_map=self.app.board_map)
        if errors:
            msg = "; ".join(str(e) for e in errors[:3])
            if len(errors) > 3:
                msg += f" (+{len(errors) - 3} more)"
            self.app.show_status(f"Validation errors: {msg}", "error", 8000)
            return
        if not self.app.device.connected:
            return

        payload = config.to_dict()

        def work(ctx):
            ctx.status("Writing configuration to device…")
            ctx.log("set_config")
            ctx.check_cancel()
            self.app.device.set_config(payload)
            ctx.log("Configuration written")

        def on_success(_):
            self._clear_dirty()
            self.app.show_status("Configuration saved to device", "success")

        self.app.run_operation("Saving configuration", work, on_success=on_success, success_message="Saved")

    def _import_json(self):
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
