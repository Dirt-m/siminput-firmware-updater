from __future__ import annotations

import threading
import webbrowser
from typing import Callable

import customtkinter as ctk

from . import ui_theme as t
from .operations import OperationCancelled, OperationContext
from .widgets.overlay import BusyOverlay
from .pages.device_page import DevicePage
from .pages.configure_page import ConfigurePage
from .pages.update_page import UpdatePage

NAV_ITEMS = [
    ("device", "Device", "Monitor & info"),
    ("configure", "Configure", "Buttons, axes, rules"),
    ("update", "Update", "Flash firmware"),
]

SCAN_INTERVAL_MS = 1000        # cheap port-presence tick
RESCAN_TICKS = 3               # full re-probe cadence while unconnected, in ticks
CONNECT_COOLDOWN_TICKS = 3     # ticks a port sits out of auto-connect after failing
LABEL_SEARCHING = "Searching for devices…"
FIRMWARE_RELEASES_URL = "https://github.com/Dirt-m/siminput-firmware-v2/releases"
CONNECTABLE = ("ok", "no_response")


class App(ctk.CTk):
    def __init__(self, device):
        super().__init__()
        self.device = device
        self.full_info = None

        ctk.set_appearance_mode("system")

        self.title("SIMINPUT Configurator")
        self._set_initial_geometry()
        self.minsize(940, 620)
        self.configure(fg_color=t.BG)

        self._connection_listeners: list[Callable[[bool], None]] = []
        self._theme_listeners: list[Callable[[], None]] = []
        self._status_after_id: str | None = None
        self._toast = None

        self._found: list = []                # connectable devices from last scan
        self._discovered: dict[str, str] = {}  # dropdown label -> port
        self._scanning = False
        self._connecting = False
        self._known_ports: set[str] = set()    # ports seen by the last presence tick
        self._rescan_in = 0                    # ticks until the next forced full probe
        self._cooldown: dict[str, int] = {}    # port -> ticks to skip auto-connect

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_sidebar()
        self._build_header()
        self._build_content()

        self.overlay = BusyOverlay(self)

        self._show_page("device")
        self._refresh_connection_view()
        self._scan_tick()

    def _set_initial_geometry(self):
        """Size to ~70% of the screen (never below 1120x760) and centre.

        customtkinter multiplies the width/height of geometry() by its window
        scaling factor while winfo_screen*() reports physical pixels, so the
        desired physical size is divided back out. Position offsets are not
        scaled by customtkinter.
        """
        try:
            from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker
            ws = ScalingTracker.get_window_scaling(self)
        except Exception:
            ws = 1.0
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        phys_w = min(max(1120, sw * 0.70), max(sw - 60, 640))
        phys_h = min(max(760, sh * 0.75), max(sh - 100, 480))
        x = max(0, int((sw - phys_w) / 2))
        y = max(0, int((sh - phys_h) / 2.5))
        self.geometry(f"{int(phys_w / ws)}x{int(phys_h / ws)}+{x}+{y}")

    # ------------------------------------------------------------------ shell

    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, width=210, corner_radius=t.RADIUS, fg_color=t.SURFACE)
        bar.grid(row=0, column=0, rowspan=2, sticky="nsw")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(len(NAV_ITEMS) + 1, weight=1)

        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.grid(row=0, column=0, padx=22, pady=(24, 20), sticky="w")
        ctk.CTkLabel(
            brand, text="SIMINPUT", anchor="w",
            font=t.font(19, "bold"), text_color=t.TEXT,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            brand, text="CONFIGURATOR", anchor="w",
            font=t.font(10, "bold"), text_color=t.ACCENT,
        ).grid(row=1, column=0, pady=(2, 0), sticky="w")

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for i, (key, label, _sub) in enumerate(NAV_ITEMS):
            btn = ctk.CTkButton(
                bar, text=f"  {label}", anchor="w",
                font=t.font(14, "bold"), height=42, corner_radius=t.RADIUS,
                fg_color="transparent", text_color=t.TEXT_DIM,
                hover_color=t.HOVER,
                command=lambda k=key: self._show_page(k),
            )
            btn.grid(row=i + 1, column=0, padx=12, pady=2, sticky="ew")
            self._nav_buttons[key] = btn

        self._side_device = ctk.CTkLabel(
            bar, text="No device", anchor="w", justify="left",
            font=t.font(11), text_color=t.TEXT_MUTED,
        )
        self._side_device.grid(row=len(NAV_ITEMS) + 2, column=0, padx=22, pady=(0, 18), sticky="sw")

    def _build_header(self):
        header = ctk.CTkFrame(self, height=64, corner_radius=t.RADIUS, fg_color=t.BG)
        header.grid(row=0, column=1, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        self._page_title = ctk.CTkLabel(
            header, text="", anchor="w",
            font=t.font(22, "bold"), text_color=t.TEXT,
        )
        self._page_title.grid(row=0, column=0, padx=28, pady=18, sticky="w")

        pill = ctk.CTkFrame(header, fg_color=t.SURFACE_2, corner_radius=t.RADIUS, height=36)
        pill.grid(row=0, column=1, padx=(0, 10), pady=14, sticky="e")
        self._pill_dot = ctk.CTkLabel(pill, text="●", font=t.font(14), text_color=t.TEXT_MUTED)
        self._pill_dot.grid(row=0, column=0, padx=(12, 4), pady=4)
        self._conn_menu = ctk.CTkOptionMenu(
            pill, values=[LABEL_SEARCHING], command=self._on_select_device,
            width=210, height=30, corner_radius=t.RADIUS,
            fg_color=t.SURFACE_3, button_color=t.SURFACE_3, button_hover_color=t.HOVER,
            text_color=t.TEXT, font=t.font(12, "bold"),
            dropdown_fg_color=t.SURFACE_2, dropdown_hover_color=t.HOVER, dropdown_text_color=t.TEXT,
        )
        self._conn_menu.set(LABEL_SEARCHING)
        self._conn_menu.grid(row=0, column=1, padx=(0, 4), pady=3)

        self._firmware_btn = ctk.CTkButton(
            header, text="Find the latest firmware", height=36, corner_radius=t.RADIUS,
            fg_color=t.SURFACE_2, hover_color=t.HOVER, text_color=t.TEXT,
            font=t.font(12, "bold"), command=self._open_firmware_releases,
        )
        self._firmware_btn.grid(row=0, column=2, padx=(0, 10), pady=14)

        self._theme_btn = ctk.CTkButton(
            header, text="", width=36, height=36, corner_radius=t.RADIUS,
            fg_color=t.SURFACE_2, hover_color=t.HOVER, text_color=t.TEXT,
            font=t.font(16), command=self._toggle_theme,
        )
        self._theme_btn.grid(row=0, column=3, padx=(0, 24), pady=14)
        self._sync_theme_button()

    def _open_firmware_releases(self):
        webbrowser.open(FIRMWARE_RELEASES_URL)

    def _build_content(self):
        self.content = ctk.CTkFrame(self, fg_color=t.BG)
        self.content.grid(row=1, column=1, sticky="nsew", padx=20, pady=(0, 16))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.pages: dict[str, ctk.CTkFrame] = {
            "device": DevicePage(self.content, self),
            "configure": ConfigurePage(self.content, self),
            "update": UpdatePage(self.content, self),
        }
        self._current_page: str | None = None

    def _show_page(self, name: str):
        if self._current_page == name:
            return
        if self._current_page and hasattr(self.pages.get(self._current_page), "on_hide"):
            self.pages[self._current_page].on_hide()

        for key in self._nav_buttons:
            btn = self._nav_buttons[key]
            if key == name:
                btn.configure(fg_color=t.ACCENT_SOFT, text_color=t.TEXT)
            else:
                btn.configure(fg_color="transparent", text_color=t.TEXT_DIM)

        for key, page in self.pages.items():
            if key == name:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()

        self._page_title.configure(text=next(label for k, label, _ in NAV_ITEMS if k == name))
        self._current_page = name

        if hasattr(self.pages[name], "on_show"):
            self.pages[name].on_show()

    # --------------------------------------------------------------- theme

    def register_theme_listener(self, fn: Callable[[], None]):
        """Called after light/dark switches — for plain-tk widgets that
        customtkinter can't recolour automatically."""
        self._theme_listeners.append(fn)

    def _toggle_theme(self):
        mode = "light" if ctk.get_appearance_mode() == "Dark" else "dark"
        ctk.set_appearance_mode(mode)
        self._sync_theme_button()
        for fn in self._theme_listeners:
            fn()

    def _sync_theme_button(self):
        # The glyph shows the mode the button switches to.
        self._theme_btn.configure(text="☀" if ctk.get_appearance_mode() == "Dark" else "☾")

    # ---------------------------------------------------------- connection

    def register_connection_listener(self, fn: Callable[[bool], None]):
        self._connection_listeners.append(fn)

    def notify_connected(self, info, full_info):
        self.full_info = full_info
        self._refresh_connection_view()
        for fn in self._connection_listeners:
            fn(True)

    def notify_disconnected(self):
        self.full_info = None
        self._refresh_connection_view()
        for fn in self._connection_listeners:
            fn(False)

    @property
    def connected(self) -> bool:
        return bool(self.device.connected)

    @property
    def board_map(self) -> str:
        info = self.device.info if self.device.connected else None
        return info.board_map if info else ""

    # ------------------------------------------------------- background scan

    def _scan_tick(self):
        """Watch ports every second; probe (open + ping) only when needed.

        A cheap port enumeration spots plugs and unplugs immediately — losing
        the connected port triggers an instant disconnect. The expensive full
        probe runs only when the port set changes, plus on a slow cadence
        while unconnected so a device that was busy booting gets re-tried.
        """
        if self._scanning:
            return  # a scan is already running; its result handler reschedules
        if self._connecting:
            self.after(SCAN_INTERVAL_MS, self._scan_tick)
            return
        self._scanning = True

        for port in list(self._cooldown):
            self._cooldown[port] -= 1
            if self._cooldown[port] <= 0:
                del self._cooldown[port]

        known = set(self._known_ports)
        connected_port = self.device.info.port if self.device.connected and self.device.info else None
        self._rescan_in -= 1
        rescan_due = connected_port is None and self._rescan_in <= 0

        def work():
            self.device.ensure_keepalive()
            try:
                ports = self.device.list_ports()
            except Exception:
                ports = set()
            lost = connected_port is not None and connected_port not in ports
            devices = None
            if lost or ports != known or (rescan_due and ports):
                skip = set() if lost or not connected_port else {connected_port}
                try:
                    devices = self.device.discover(skip)
                except Exception:
                    devices = []
            self.after(0, lambda: self._on_scan_result(ports, lost, devices))

        threading.Thread(target=work, daemon=True).start()

    def _on_scan_result(self, ports: set[str], lost: bool, devices):
        self._scanning = False
        self._known_ports = ports

        if lost:
            self.device.disconnect()
            self.notify_disconnected()
            self.show_status("Device disconnected", "error")

        if devices is not None:  # a full probe ran
            self._rescan_in = RESCAN_TICKS
            self._found = [d for d in devices if d.status in CONNECTABLE]

        # Auto-connect when idle — a user with a single box never has to touch
        # the dropdown. discover() puts responding devices first, and ports
        # that just failed to connect sit out a few ticks.
        if not self.device.connected and not self._connecting:
            candidates = [d for d in self._found if d.port not in self._cooldown]
            if candidates:
                self._connect_to(candidates[0].port)

        self._refresh_connection_view()
        self.after(SCAN_INTERVAL_MS, self._scan_tick)

    # ----------------------------------------------------------- connecting

    def _connect_to(self, port: str):
        if self._connecting:
            return
        self._connecting = True
        if self.device.connected:
            self.notify_disconnected()  # stop the monitor on the old device

        def work():
            try:
                info = self.device.connect(port)
                full = self.device.get_info()
                self.after(0, lambda: self._connect_done(info, full))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self._connect_failed(port, msg))

        threading.Thread(target=work, daemon=True).start()

    def _connect_done(self, info, full):
        self._connecting = False
        self._cooldown.pop(info.port, None)
        self.notify_connected(info, full)
        self.show_status(f"Connected to {info.name}", "success")

    def _connect_failed(self, port: str, msg: str):
        self._connecting = False
        self._cooldown[port] = CONNECT_COOLDOWN_TICKS
        self.show_status(f"Connection failed: {msg}", "error", 6000)
        self._refresh_connection_view()

    def _on_select_device(self, label: str):
        port = self._discovered.get(label)
        if not port:
            # placeholder ("Searching…") — keep the dropdown showing reality
            self._refresh_connection_view()
            return
        if self.device.connected and self.device.info and self.device.info.port == port:
            return
        self._cooldown.pop(port, None)  # an explicit click always tries now
        self._connect_to(port)

    # ---------------------------------------------------------- dropdown view

    def _refresh_connection_view(self):
        info = self.device.info if self.device.connected else None

        devs = []
        if info:
            devs.append(info)
        for d in self._found:
            if info and d.port == info.port:
                continue
            devs.append(d)

        if devs:
            names = [d.name for d in devs]
            order: list[str] = []
            mapping: dict[str, str] = {}
            for d in devs:
                label = d.name if names.count(d.name) == 1 else f"{d.name} · {d.port.split('/')[-1]}"
                order.append(label)
                mapping[label] = d.port
        else:
            order, mapping = [LABEL_SEARCHING], {}

        self._discovered = mapping
        if list(self._conn_menu.cget("values")) != order:
            self._conn_menu.configure(values=order)
        self._conn_menu.set(order[0])

        if info:
            self._pill_dot.configure(text_color=t.SUCCESS)
            self._side_device.configure(
                text=f"{info.name}\n{info.port}  ·  v{info.version}",
                text_color=t.TEXT_DIM,
            )
        else:
            self._pill_dot.configure(text_color=t.TEXT_MUTED)
            self._side_device.configure(text="No device", text_color=t.TEXT_MUTED)

    # ---------------------------------------------------------- operations

    def run_operation(
        self,
        title: str,
        work: Callable[[OperationContext], object],
        *,
        on_success: Callable[[object], None] | None = None,
        success_message: str = "Done",
        indeterminate: bool = True,
    ):
        """Show the blocking overlay and run `work` on a worker thread.

        `work` receives an OperationContext for status/log/progress/cancel.
        Raising OperationCancelled or aborting via the overlay ends as cancelled.
        """
        cancel = threading.Event()
        ctx = OperationContext(self, cancel)
        self.overlay.show(title, on_cancel=cancel.set, indeterminate=indeterminate)

        def runner():
            try:
                result = work(ctx)
            except OperationCancelled:
                self.after(0, self.overlay.finish_cancelled)
                return
            except Exception as e:  # surface any device/IO error in the overlay
                msg = str(e) or e.__class__.__name__
                self.after(0, lambda: self.overlay.finish_error(msg))
                return
            if cancel.is_set():
                self.after(0, self.overlay.finish_cancelled)
                return

            def done():
                if on_success:
                    on_success(result)
                self.overlay.finish_success(success_message)
            self.after(0, done)

        threading.Thread(target=runner, daemon=True).start()

    # ---------------------------------------------------------------- toast

    def show_status(self, message: str, kind: str = "info", duration_ms: int = 4000):
        if self._status_after_id is not None:
            self.after_cancel(self._status_after_id)
            self._status_after_id = None
        if self._toast is not None:
            self._toast.destroy()
            self._toast = None

        palette = {
            "success": (t.SUCCESS_SOFT, t.SUCCESS, t.SUCCESS),
            "error": (t.ERROR_SOFT, t.ERROR, t.ERROR),
            "info": (t.SURFACE_2, t.BORDER_STRONG, t.TEXT),
        }
        bg, border, text_color = palette.get(kind, palette["info"])

        toast = ctk.CTkFrame(self, fg_color=bg, corner_radius=t.RADIUS, border_width=1, border_color=border)
        ctk.CTkLabel(toast, text="●", font=t.font(11), text_color=border).grid(row=0, column=0, padx=(14, 8), pady=10)
        ctk.CTkLabel(toast, text=message, font=t.font(13), text_color=text_color, anchor="w").grid(
            row=0, column=1, padx=(0, 16), pady=10, sticky="w")
        toast.place(relx=0.99, rely=0.985, anchor="se", x=-4, y=-4)
        self._toast = toast

        self._status_after_id = self.after(duration_ms, self._hide_toast)

    def _hide_toast(self):
        self._status_after_id = None
        if self._toast is not None:
            self._toast.destroy()
            self._toast = None
