"""The Update page's "switch firmware" card: stock SIMINPUT firmware to
SIMINPUT firmware for MobiFlight and back, and setting up a fresh box.

What it offers follows what is plugged in (polled while the page shows):

- a stock rev2 box on serial: switch it to MobiFlight (rev1: the card hides)
- a box answering MobiFlight's GetInfo as "SIMINPUT rev2": switch it back
- an RPI-RP2 drive (a fresh box, or BOOTSEL held): install either firmware
- a CIRCUITPY drive with CircuitPython 9: install the stock firmware
- nothing: a line on how to get a new box to show up

The work itself is in `switch.py`, run through `App.run_operation`.
"""

from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from .. import releases, switch
from .. import ui_theme as t
from ..config_history import Backup
from ..firmware import FirmwarePackage
from ..mobiflight import CONNECTOR_HINT, MobiFlightBox
from .history_dialog import HistoryDialog

if TYPE_CHECKING:
    from ..app import App

POLL_MS = 1500
WRAP = 760
COMMUNITY_HINT = r"%LOCALAPPDATA%\MobiFlight\MobiFlight Connector\Community\ "
MF_RELEASES = f"github.com/{releases.MOBIFLIGHT.repo}/releases"


class SwitchPanel(ctk.CTkFrame):
    def __init__(self, parent, app: App, stock_package: Callable[[], FirmwarePackage | None]):
        super().__init__(parent, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        self.app = app
        self.hw = app.hardware
        self._stock_package = stock_package
        self._scanner = switch.Scanner(self.hw)
        self._scan = switch.Scan()
        self._scanning = False
        self._visible = False
        self._poll_id: str | None = None
        self._mode = ""
        self._box: MobiFlightBox | None = None
        self._restore: switch.RestoreChoice | None = None
        self._restore_key: tuple | None = None   # what the restore choice was made for

        self.mf_pkg: switch.MobiFlightPackage | None = None
        self._mf_note = "Getting the latest SIMINPUT firmware for MobiFlight from GitHub…"
        self._mf_failed = False

        self.grid_columnconfigure(0, weight=1)
        self.title = ctk.CTkLabel(self, text="", font=t.font(15, "bold"), text_color=t.TEXT, anchor="w")
        self.title.grid(row=0, column=0, padx=18, pady=(16, 2), sticky="w")
        self.body = ctk.CTkLabel(self, text="", font=t.font(12), text_color=t.TEXT_DIM, anchor="w",
                                 justify="left", wraplength=WRAP)
        self.body.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="w")

        self.fw_row = ctk.CTkFrame(self, fg_color="transparent")
        self.fw_row.grid(row=2, column=0, padx=18, pady=(0, 6), sticky="ew")
        self.fw_row.grid_columnconfigure(0, weight=1)
        self.fw_lbl = ctk.CTkLabel(self.fw_row, text="", font=t.font(12), text_color=t.TEXT,
                                   anchor="w", justify="left", wraplength=WRAP - 160)
        self.fw_lbl.grid(row=0, column=0, sticky="w")
        self.fw_btn = t.ghost_button(self.fw_row, "Choose file…", self._choose_mf_file, width=120, height=28)
        self.fw_btn.grid(row=0, column=1, padx=(12, 0), sticky="e")

        self.restore_row = ctk.CTkFrame(self, fg_color="transparent")
        self.restore_row.grid(row=3, column=0, padx=18, pady=(0, 6), sticky="ew")
        self.restore_row.grid_columnconfigure(0, weight=1)
        self.restore_lbl = ctk.CTkLabel(self.restore_row, text="", font=t.font(12), text_color=t.TEXT,
                                        anchor="w", justify="left", wraplength=WRAP - 160)
        self.restore_lbl.grid(row=0, column=0, sticky="w")
        t.ghost_button(self.restore_row, "Change…", self._change_restore, width=120, height=28).grid(
            row=0, column=1, padx=(12, 0), sticky="e")

        self.community_cb = ctk.CTkCheckBox(
            self, text="Also put the MobiFlight Connector files in its Community folder",
            font=t.font(12), fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER, text_color=t.TEXT_DIM)
        self.community_cb.select()
        self.community_cb.grid(row=4, column=0, padx=18, pady=(2, 6), sticky="w")

        self.actions = ctk.CTkFrame(self, fg_color="transparent")
        self.actions.grid(row=5, column=0, padx=18, pady=(8, 16), sticky="w")
        self.primary = t.primary_button(self.actions, "", None, width=250)
        self.primary.grid(row=0, column=0, sticky="w")
        self.secondary = t.ghost_button(self.actions, "", None, width=290)
        self.secondary.grid(row=0, column=1, padx=(10, 0), sticky="w")

        self.note = ctk.CTkLabel(self, text="", font=t.font(11), text_color=t.TEXT_MUTED, anchor="w",
                                 justify="left", wraplength=WRAP)
        self.note.grid(row=6, column=0, padx=18, pady=(0, 16), sticky="w")

        app.register_connection_listener(lambda _c: self.refresh())
        self._fetch_mobiflight()
        self.refresh()

    # ----------------------------------------------------------- lifecycle

    def on_show(self):
        self._visible = True
        if self._poll_id is None:
            self._poll()

    def on_hide(self):
        self._visible = False

    def _poll(self):
        self._poll_id = None
        if not self._visible:
            return
        if not self._scanning and not self.app.busy and not self.app.device.connected:
            self._scanning = True

            def work():
                try:
                    result = self._scanner.scan()
                except Exception:
                    result = switch.Scan()
                self.app.post(lambda: self._scanned(result))

            threading.Thread(target=work, daemon=True).start()
        self._poll_id = self.after(POLL_MS, self._poll)

    def _scanned(self, result: switch.Scan):
        self._scanning = False
        self._scan = result
        self.refresh()

    def rescan_soon(self):
        self._scanner.forget()
        self._scan = switch.Scan()
        self._restore_key = None
        self.refresh()

    def _run(self, title: str, work, done, success: str):
        """run_operation, plus: whatever happened, the pages hear about a box
        that is no longer connected (the flows restart it)."""
        device = self.app.device

        def guarded(ctx):
            try:
                return work(ctx)
            except BaseException:
                if not device.connected:
                    self.app.post(self._after_failure)
                else:
                    # Stopped after the box came back: let the pages know.
                    try:
                        info, full = device.info, device.get_info()
                        self.app.post(lambda: self.app.notify_connected(info, full))
                    except Exception:
                        pass
                raise

        self.app.run_operation(title, guarded, on_success=done, success_message=success,
                               indeterminate=False)

    def _after_failure(self):
        if self.app.full_info is not None:
            self.app.notify_disconnected()
        self.rescan_soon()

    # ------------------------------------------------------------ packages

    def _fetch_mobiflight(self):
        def work():
            try:
                rel = releases.fetch_latest(source=releases.MOBIFLIGHT)
                path = releases.download(rel)
                pkg = switch.load_mobiflight_package(path)
                if pkg.version != rel.version:
                    raise switch.SwitchError(f"the package says v{pkg.version} but the release is {rel.tag}")
                pkg.source = rel.asset_name
                self.app.post(lambda: self._set_mf(pkg, f"v{pkg.version}, the latest from GitHub"))
            except (releases.ReleaseError, switch.SwitchError) as e:
                msg = str(e)
                self.app.post(lambda: self._mf_fetch_failed(msg))

        threading.Thread(target=work, daemon=True).start()

    def _set_mf(self, pkg: switch.MobiFlightPackage, note: str):
        self.mf_pkg = pkg
        self._mf_note = note
        self._mf_failed = False
        self.refresh()

    def _mf_fetch_failed(self, msg: str):
        if self.mf_pkg is None:
            self._mf_note = f"Couldn't get it from GitHub ({msg}). Choose the release .zip or .uf2."
            self._mf_failed = True
            self.refresh()

    def _choose_mf_file(self):
        path = filedialog.askopenfilename(
            title="SIMINPUT firmware for MobiFlight",
            filetypes=[("Release zip or UF2", "*.zip *.uf2"), ("All files", "*.*")])
        if not path:
            return
        try:
            pkg = switch.load_mobiflight_package(path)
        except switch.SwitchError as e:
            self.app.show_status(str(e), "error", 6000)
            return
        self._set_mf(pkg, f"v{pkg.version}, from {Path(path).name}")

    # ------------------------------------------------------------- restore

    def _restore_for(self, key: tuple, box_ids: list[str] | None):
        if self._restore_key != key:
            self._restore_key = key
            self._restore = switch.choose_restore(box_ids)

    def _change_restore(self):
        def picked(b: Backup):
            self._restore = switch.RestoreChoice(b, self._restore.record_id if self._restore else None)
            self.refresh()

        def blank():
            self._restore = switch.RestoreChoice(None, self._restore.record_id if self._restore else None)
            self.refresh()

        HistoryDialog(self, picked, title="Configuration to restore", action="Use",
                      intro="Pick the configuration the box gets on the SIMINPUT firmware. These are "
                            "the backups made before every save, update and switch.",
                      extra=("Blank configuration", blank))

    # --------------------------------------------------------------- view

    def _mode_now(self) -> str:
        dev = self.app.device
        if dev.connected:
            full = self.app.full_info
            fault = getattr(full, "fault", "") if full else ""
            return "to_mf" if self.app.board_map == "rev2" and fault != "no_expander" else "hidden"
        s = self._scan
        if s.siminput_boxes:
            return "mf_box"
        if s.bootsel:
            return "bootsel"
        if s.circuitpy and s.circuitpy_version.startswith("9."):
            return "circuitpy"
        if s.busy_boxes:
            return "mf_busy"
        return "idle"

    def refresh(self):
        mode = self._mode_now()
        self._mode = mode
        if mode == "hidden":
            self.grid_remove()
            return
        self.grid()
        stock = self._stock_package()
        stock_v = f"v{stock.firmware_version}" if stock else None
        mf = self.mf_pkg
        show_fw = show_restore = show_community = show_secondary = False
        primary: tuple[str, Callable[[], None], bool] | None = None
        note = ""

        if mode == "to_mf":
            title = "SIMINPUT firmware for MobiFlight"
            body = ("Makes this box a native MobiFlight module, so the sim can drive outputs such as "
                    "the backlight. The box stops being a joystick and works with MobiFlight Connector "
                    "only. Your configuration is backed up first, and you can switch back here.")
            show_fw = True
            self.fw_lbl.configure(text=f"Firmware: {self._mf_note}",
                                  text_color=t.WARN if self._mf_failed else t.TEXT)
            show_community = self._community_target() is not None
            primary = ("Switch to MobiFlight firmware", self._start_to_mf, mf is not None)
            if getattr(self.app.pages.get("configure"), "_dirty", False):
                note = ("The Configure page has unsaved edits. The backup holds what is on the box, "
                        "so save them first if you want to keep them.")
        elif mode == "mf_box":
            self._box = self._scan.siminput_boxes[0]
            self._restore_for(("mf", self._box.usb_serial, self._box.serial), self._box.ids)
            title = "Box on SIMINPUT firmware for MobiFlight"
            body = ("Switch it back to the SIMINPUT firmware. The box is erased, then gets CircuitPython, "
                    "the SIMINPUT firmware and your configuration.")
            show_restore = True
            primary = ("Switch back to SIMINPUT firmware", self._start_to_siminput, stock is not None)
            note = ("Using it in MobiFlight instead? In MobiFlight Connector, select SIMINPUT rev2 and "
                    "click “upload default config”.")
        elif mode == "bootsel":
            self._restore_for(("anon",), None)
            title = "Box in bootloader mode (RPI-RP2)"
            body = ("A new box, or one plugged in with BOOTSEL held. Choose the firmware to install. "
                    "SIMINPUT firmware for MobiFlight is for rev2 boxes only.")
            show_restore = show_fw = True
            self.fw_lbl.configure(text=f"MobiFlight firmware: {self._mf_note}",
                                  text_color=t.WARN if self._mf_failed else t.TEXT)
            show_community = self._community_target() is not None
            primary = ("Install SIMINPUT firmware", self._start_to_siminput, stock is not None)
            show_secondary = True
            self.secondary.configure(text="Install MobiFlight firmware", command=self._start_fresh_mf,
                                     state="normal" if mf else "disabled")
        elif mode == "circuitpy":
            self._restore_for(("anon",), None)
            title = "Box on CircuitPython"
            body = (f"It has CircuitPython {self._scan.circuitpy_version} but not the SIMINPUT firmware, "
                    f"for example after a switch that stopped half way. Install it to finish.")
            show_restore = True
            primary = ("Install SIMINPUT firmware", self._start_to_siminput, stock is not None)
        elif mode == "mf_busy":
            title = "Box on MobiFlight firmware"
            body = f"Its port is in use. {CONNECTOR_HINT} Then it shows up here."
        else:
            title = "New box, or one on MobiFlight firmware?"
            body = ("Plug it in and it shows up here. A new box appears as an RPI-RP2 drive. Any other "
                    "box: hold the BOOTSEL button while plugging it in. On MobiFlight firmware, close "
                    "MobiFlight Connector first.")

        if mode in ("mf_box", "bootsel", "circuitpy") and stock is None:
            note = "Waiting for the SIMINPUT firmware: it downloads above, or choose a .zip there."
        elif mode in ("mf_box", "bootsel", "circuitpy"):
            note = note or f"Installs SIMINPUT firmware {stock_v} and CircuitPython {switch.CIRCUITPYTHON_VERSION}."

        self.title.configure(text=title)
        self.body.configure(text=body)
        self.fw_row.grid() if show_fw else self.fw_row.grid_remove()
        if show_restore and self._restore is not None:
            self.restore_lbl.configure(text=f"Restores: {self._restore.label}")
            self.restore_row.grid()
        else:
            self.restore_row.grid_remove()
        self.community_cb.grid() if show_community else self.community_cb.grid_remove()
        if primary:
            text, cmd, enabled = primary
            self.primary.configure(text=text, command=cmd, state="normal" if enabled else "disabled",
                                   fg_color=t.ACCENT if enabled else t.SURFACE_3,
                                   text_color="#FFFFFF" if enabled else t.TEXT_MUTED)
            self.actions.grid()
            self.secondary.grid() if show_secondary else self.secondary.grid_remove()
        else:
            self.actions.grid_remove()
        self.note.configure(text=note)
        self.note.grid() if note else self.note.grid_remove()

    def _community_target(self) -> Path | None:
        if self.mf_pkg is None or not self.mf_pkg.has_community:
            return None
        return self.hw.community_dir()

    # ------------------------------------------------------------ actions

    def _start_to_mf(self):
        pkg, device = self.mf_pkg, self.app.device
        if pkg is None or not device.connected:
            return
        if getattr(self.app.pages.get("configure"), "_dirty", False):
            if not messagebox.askyesno("Unsaved changes",
                                       "The Configure page has edits that aren't saved to the box. "
                                       "The backup holds what is on the box now.\n\nSwitch anyway?"):
                return
        if not messagebox.askyesno(
                "Switch to MobiFlight firmware?",
                "1. Your configuration is backed up on this computer.\n"
                f"2. The box restarts and gets SIMINPUT firmware for MobiFlight v{pkg.version}.\n"
                "3. It shows up in MobiFlight Connector as SIMINPUT rev2, and is no longer a joystick.\n\n"
                "Switching back restores your configuration. This takes about a minute."):
            return
        community = self._community_target() if self.community_cb.get() else None

        def work(ctx):
            return switch.to_mobiflight(device, self.hw, pkg, ctx, community)

        def done(_box):
            self.app.notify_disconnected()
            self.rescan_soon()
            self.after(800, lambda: self._next_steps(pkg, community))

        self._run("Switching to MobiFlight firmware", work, done,
                  "The box is on SIMINPUT firmware for MobiFlight")

    def _start_fresh_mf(self):
        pkg = self.mf_pkg
        if pkg is None:
            return
        if not messagebox.askyesno(
                "Is this a rev2 box?",
                "SIMINPUT firmware for MobiFlight is for rev2 boxes only; rev1 is wired differently.\n\n"
                "Not sure? Plug the box in without BOOTSEL first: on the SIMINPUT firmware the "
                f"Configurator checks it for you.\n\nInstall v{pkg.version} on this rev2 box?",
                icon="warning"):
            return
        community = self._community_target() if self.community_cb.get() else None

        def work(ctx):
            return switch.install_mobiflight(self.hw, pkg, ctx, community)

        def done(_box):
            self.rescan_soon()
            self.after(800, lambda: self._next_steps(pkg, community))

        self._run("Installing MobiFlight firmware", work, done,
                  "The box is on SIMINPUT firmware for MobiFlight")

    def _start_to_siminput(self):
        stock, choice, mode = self._stock_package(), self._restore, self._mode
        if stock is None or choice is None or mode not in ("mf_box", "bootsel", "circuitpy"):
            return
        try:
            config = choice.config()
        except switch.SwitchError as e:
            messagebox.showerror("Can't use that backup", str(e))
            return
        steps = {
            "mf_box": "The box is erased, then gets CircuitPython "
                      f"{switch.CIRCUITPYTHON_VERSION} and SIMINPUT firmware v{stock.firmware_version}.",
            "bootsel": "The box is erased, then gets CircuitPython "
                       f"{switch.CIRCUITPYTHON_VERSION} and SIMINPUT firmware v{stock.firmware_version}.",
            "circuitpy": f"The box gets SIMINPUT firmware v{stock.firmware_version}.",
        }[mode]
        if not messagebox.askyesno(
                "Install SIMINPUT firmware?",
                f"{steps}\n\nConfiguration: {choice.label}\n\n"
                "At the end you unplug the box and plug it back in. This takes about two minutes."):
            return
        start = {"mf_box": "mobiflight", "bootsel": "bootsel", "circuitpy": "circuitpy"}[mode]
        box, device, hw = self._box, self.app.device, self.hw

        def work(ctx):
            switch.to_siminput(hw, stock, config, ctx, start=start, box=box,
                               record_id=choice.record_id, device=device)
            return device.info, device.get_info()

        def done(result):
            info, full = result
            self.rescan_soon()
            self.app.notify_connected(info, full)

        self._run("Installing SIMINPUT firmware", work, done,
                  "The box is on the SIMINPUT firmware, configuration restored")

    def _next_steps(self, pkg: switch.MobiFlightPackage, community: Path | None):
        if community is not None:
            first = "1. The Connector files are in its Community folder. Restart MobiFlight Connector."
        else:
            name = pkg.source if pkg.has_community else f"siminput_rev2_{pkg.version}.zip"
            first = (f"1. Unzip {name} (from {MF_RELEASES}) into MobiFlight Connector's Community "
                     f"folder, {COMMUNITY_HINT.strip()}, then restart the Connector.")
        messagebox.showinfo(
            "Next: MobiFlight Connector",
            f"The box runs SIMINPUT firmware for MobiFlight v{pkg.version}.\n\n{first}\n"
            "2. Select the SIMINPUT rev2 module and click “upload default config”.\n\n"
            "To switch back, close the Connector and open this Update page.")
