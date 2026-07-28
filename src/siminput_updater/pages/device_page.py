from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from .. import ui_theme as t
from ..widgets.button_grid import ButtonGrid
from ..widgets.axis_bar import AxisBar

if TYPE_CHECKING:
    from ..app import App

AXIS_LABELS = ["X", "Y", "Z", "Rx", "Ry", "Rz", "Slider", "Dial"]


class DevicePage(ctk.CTkFrame):
    """Device info + live input monitor.

    Connection is handled by the dropdown in the top-right header; this page
    just reports what the connected device is and streams its live input. Both
    panels show an empty state until a device is connected.
    """

    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._streaming = False
        self._pending_state: dict | None = None
        self._update_scheduled = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_info_panel()
        self._build_monitor()

        self.app.register_connection_listener(self._on_connection_changed)
        self.app.register_theme_listener(self._retheme_canvases)
        self._sync_connection_view()

    def _retheme_canvases(self):
        self.button_grid.retheme()
        for bar in self.axis_bars:
            bar.retheme()

    # --------------------------------------------------------------- info

    def _build_info_panel(self):
        panel = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        panel.grid_columnconfigure(0, weight=1)

        # empty state — no device connected
        self._info_empty = ctk.CTkFrame(panel, fg_color="transparent")
        self._info_empty.grid(row=0, column=0, sticky="ew", padx=18, pady=18)
        ctk.CTkLabel(self._info_empty, text="●", font=t.font(11), text_color=t.TEXT_MUTED).grid(
            row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(
            self._info_empty,
            text="No device connected — pick one from the connection menu in the top-right.",
            font=t.font(13), text_color=t.TEXT_DIM, anchor="w",
        ).grid(row=0, column=1, sticky="w")

        # connected state — stat chips
        self._stats_row = ctk.CTkFrame(panel, fg_color="transparent")
        self._stats_row.grid(row=0, column=0, sticky="ew", padx=18, pady=16)
        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        stats = [
            ("name", "Device"), ("version", "Firmware"), ("board_rev", "Board"),
            ("cp", "CircuitPython"), ("pid", "USB PID"), ("nvm", "NVM"),
        ]
        for i, (key, label) in enumerate(stats):
            self._stats_row.grid_columnconfigure(i, weight=1)
            chip = ctk.CTkFrame(self._stats_row, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
            chip.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0), sticky="ew")
            ctk.CTkLabel(chip, text=label.upper(), font=t.font(9, "bold"), text_color=t.TEXT_MUTED, anchor="w").grid(
                row=0, column=0, padx=12, pady=(10, 0), sticky="w")
            val = ctk.CTkLabel(chip, text="—", font=t.font(13, "bold"), text_color=t.TEXT, anchor="w")
            val.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")
            self._stat_labels[key] = val
        self._stats_row.grid_remove()

    # -------------------------------------------------------------- monitor

    def _build_monitor(self):
        wrap = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)

        # empty state
        self._empty = ctk.CTkFrame(wrap, fg_color="transparent")
        self._empty.grid(row=0, column=0, sticky="nsew")
        self._empty.grid_columnconfigure(0, weight=1)
        self._empty.grid_rowconfigure((0, 2), weight=1)
        ctk.CTkLabel(self._empty, text="Live monitor", font=t.font(15, "bold"), text_color=t.TEXT_DIM).grid(
            row=1, column=0, pady=(0, 4))
        ctk.CTkLabel(
            self._empty, text="Connect a device to watch buttons, axes and pins in real time.",
            font=t.font(13), text_color=t.TEXT_MUTED,
        ).grid(row=2, column=0, sticky="n")

        # live content — buttons on the left, axes on the right
        self._mon = ctk.CTkFrame(wrap, fg_color="transparent")
        self._mon.grid(row=0, column=0, sticky="nsew", padx=14, pady=12)
        self._mon.grid_columnconfigure(0, weight=0)
        self._mon.grid_columnconfigure(1, weight=1)
        self._mon.grid_rowconfigure(1, weight=1)
        self._mon.grid_remove()

        self._section(self._mon, "BUTTONS", 0, 0)
        btn_wrap = ctk.CTkFrame(self._mon, fg_color="transparent")
        btn_wrap.grid(row=1, column=0, padx=(0, 24), pady=(0, 4), sticky="nw")
        self.button_grid = ButtonGrid(btn_wrap)
        self.button_grid.grid(row=0, column=0, sticky="nw")

        self._section(self._mon, "AXES", 0, 1)
        axes_frame = ctk.CTkFrame(self._mon, fg_color="transparent")
        axes_frame.grid(row=1, column=1, pady=(0, 4), sticky="new")
        axes_frame.grid_columnconfigure(1, weight=1)
        self.axis_bars: list[AxisBar] = []
        for i, name in enumerate(AXIS_LABELS):
            ctk.CTkLabel(axes_frame, text=name, width=52, anchor="e", font=t.mono(11), text_color=t.TEXT_DIM).grid(
                row=i, column=0, padx=(0, 10), pady=4)
            bar = AxisBar(axes_frame)
            bar.grid(row=i, column=1, pady=4, sticky="ew")
            self.axis_bars.append(bar)

    def _section(self, parent, title: str, row: int, col: int):
        ctk.CTkLabel(parent, text=title, font=t.font(11, "bold"), text_color=t.TEXT_MUTED, anchor="w").grid(
            row=row, column=col, padx=2, pady=(2, 8), sticky="w")

    # ------------------------------------------------------------- lifecycle

    def on_show(self):
        if self.app.device.connected and not self._streaming:
            self._start_stream()

    def on_hide(self):
        self._stop_stream()

    def _on_connection_changed(self, connected: bool):
        self._sync_connection_view()
        if connected:
            # Only stream while this page is actually visible. Starting a
            # hidden stream leaves (on Windows, where monitoring runs over
            # serial) a reader thread consuming the very responses the other
            # pages' commands are waiting for.
            if self.app._current_page == "device":
                self._start_stream()
        else:
            self._stop_stream()

    def _sync_connection_view(self):
        if self.app.device.connected and self.app.device.info:
            self._info_empty.grid_remove()
            self._stats_row.grid()
            self._empty.grid_remove()
            self._mon.grid()
            self._fill_stats()
        else:
            self._stats_row.grid_remove()
            self._info_empty.grid()
            self._mon.grid_remove()
            self._empty.grid()

    def _fill_stats(self):
        info = self.app.device.info
        full = self.app.full_info
        self._stat_labels["name"].configure(text=info.name)
        self._stat_labels["version"].configure(text=info.version)
        self._stat_labels["board_rev"].configure(text=info.board_map or "—")
        self._stat_labels["pid"].configure(text=f"0x{info.pid:04X}")
        if full:
            self._stat_labels["cp"].configure(text=full.circuitpython or "—")
            self._stat_labels["nvm"].configure(text=f"{full.nvm_size}B" if full.nvm_size else "—")
        else:
            # Don't let a previous device's values masquerade as this one's.
            self._stat_labels["cp"].configure(text="—")
            self._stat_labels["nvm"].configure(text="—")

    # --------------------------------------------------------------- stream

    def _start_stream(self):
        if self._streaming or not self.app.device.connected:
            return
        try:
            self.app.device.start_stream(
                self._on_state_update, interval_ms=50, on_end=self._on_stream_died,
            )
            self._streaming = True
        except Exception as e:
            self.app.show_status(f"Stream error: {e}", "error")

    def _stop_stream(self):
        if not self._streaming:
            return
        try:
            self.app.device.stop_stream()
        except Exception:
            pass
        self._streaming = False

    def _on_stream_died(self):
        """The reader thread exited unexpectedly (device vanished, evdev node
        closed). Reset so the monitor can restart instead of freezing."""
        def retry():
            if self.app.device.connected and self.app._current_page == "device":
                self._start_stream()

        def apply():
            self._streaming = False
            if self.app.device.connected and self.app._current_page == "device":
                self.app.show_status("Live monitor stopped — restarting…", "info")
                # Delayed retry, not immediate: a dead device would otherwise
                # spin start/die cycles as fast as the event loop allows.
                self.after(1000, retry)
        try:
            self.after(0, apply)
        except RuntimeError:
            pass

    def _on_state_update(self, state: dict):
        # Coalesce: the evdev reader can fire per input event (hundreds of Hz);
        # keep only the latest state and at most one queued UI update.
        self._pending_state = state
        if not self._update_scheduled:
            self._update_scheduled = True
            try:
                self.after(0, self._apply_pending)
            except RuntimeError:
                self._update_scheduled = False

    def _apply_pending(self):
        self._update_scheduled = False
        state = self._pending_state
        if state is None or not self._streaming:
            return
        self.button_grid.update_buttons(set(state.get("b", [])))

        for i, val in enumerate(state.get("a", [])):
            if i < len(self.axis_bars):
                self.axis_bars[i].set_value(val)
