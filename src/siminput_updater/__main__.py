from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk

import customtkinter as ctk


def _detect_linux_scale() -> float:
    try:
        root = tk.Tk()
        root.withdraw()
        dpi = root.winfo_fpixels("1i")
        root.destroy()
        factor = dpi / 96.0
        return max(factor, 1.0)
    except Exception:
        return 1.0


def _apply_scaling(override: float | None):
    if sys.platform != "linux":
        return

    if override is not None:
        scale = override
    else:
        scale = _detect_linux_scale()

    if abs(scale - 1.0) > 0.05:
        ctk.deactivate_automatic_dpi_awareness()
        ctk.set_widget_scaling(scale)
        ctk.set_window_scaling(scale)


def _fix_linux_scroll():
    if sys.platform != "linux":
        return

    from customtkinter.windows.widgets.ctk_scrollable_frame import CTkScrollableFrame

    _orig_set_increments = CTkScrollableFrame._set_scroll_increments

    def _patched_set_increments(self):
        _orig_set_increments(self)
        if sys.platform == "linux":
            self._parent_canvas.configure(xscrollincrement=4, yscrollincrement=8)

    CTkScrollableFrame._set_scroll_increments = _patched_set_increments

    _orig_mouse_wheel = CTkScrollableFrame._mouse_wheel_all

    def _patched_mouse_wheel(self, event):
        if not self.check_if_master_is_canvas(event.widget):
            return
        if sys.platform == "linux":
            delta = event.delta
            if abs(delta) >= 120:
                delta = -3 if delta > 0 else 3
            else:
                delta = -delta
            if self._shift_pressed:
                if self._parent_canvas.xview() != (0.0, 1.0):
                    self._parent_canvas.xview("scroll", delta, "units")
            else:
                if self._parent_canvas.yview() != (0.0, 1.0):
                    self._parent_canvas.yview("scroll", delta, "units")
        else:
            _orig_mouse_wheel(self, event)

    CTkScrollableFrame._mouse_wheel_all = _patched_mouse_wheel


def main():
    parser = argparse.ArgumentParser(description="SIMINPUT Firmware Updater & Configurator")
    parser.add_argument("--mock", action="store_true", help="Use mock device for testing")
    parser.add_argument("--scale", type=float, default=None,
                        help="UI scale factor (e.g. 1.5). Overrides auto-detection. "
                             "Also settable via SIMINPUT_SCALE env var.")
    args = parser.parse_args()

    use_mock = args.mock or os.environ.get("SIMINPUT_MOCK", "") == "1"

    scale_override = args.scale
    if scale_override is None:
        env_scale = os.environ.get("SIMINPUT_SCALE")
        if env_scale:
            try:
                scale_override = float(env_scale)
            except ValueError:
                pass

    _apply_scaling(scale_override)
    _fix_linux_scroll()

    if use_mock:
        from .mock_device import MockDevice
        device = MockDevice()
    else:
        from .device import Device
        device = Device()

    from .app import App
    app = App(device)

    if use_mock:
        info = device.connect("MOCK")
        full_info = device.get_info()
        app.notify_connected(info, full_info)

    app.mainloop()


if __name__ == "__main__":
    main()
