from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING

import customtkinter as ctk

from .. import ui_theme as t
from .. import releases
from ..firmware import (
    FirmwareError, FirmwarePackage, UpdateCancelled, _version_less, load_firmware_zip, perform_update,
)
from ..operations import OperationCancelled
from ..widgets.switch_panel import SwitchPanel

if TYPE_CHECKING:
    from ..app import App


class UpdatePage(ctk.CTkFrame):
    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.package = None
        self._picked_by_hand = False
        self._latest: releases.Release | None = None
        self._latest_error: str | None = None
        self._fetching = False

        self.grid_columnconfigure(0, weight=1)

        # -- 1. Package: the latest release, fetched on its own --
        select = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        select.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        select.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(select, text="SIMINPUT firmware", font=t.font(15, "bold"), text_color=t.TEXT).grid(
            row=0, column=0, columnspan=3, padx=18, pady=(16, 2), sticky="w")
        self.latest_label = ctk.CTkLabel(select, text="", font=t.font(12), text_color=t.TEXT_MUTED,
                                         anchor="w", justify="left", wraplength=760)
        self.latest_label.grid(row=1, column=0, columnspan=3, padx=18, pady=(0, 12), sticky="w")

        self.select_btn = t.ghost_button(select, "Choose .zip…", self._select_file, width=130)
        self.select_btn.grid(row=2, column=0, padx=(18, 8), pady=(0, 16), sticky="w")
        self.file_label = ctk.CTkLabel(select, text="", font=t.font(12), text_color=t.TEXT_MUTED, anchor="w")
        self.file_label.grid(row=2, column=1, padx=(4, 8), pady=(0, 16), sticky="w")
        self.retry_btn = t.ghost_button(select, "Try GitHub again", self._fetch_latest, width=150)
        self.retry_btn.grid(row=2, column=2, padx=(0, 18), pady=(0, 16), sticky="e")
        self.retry_btn.grid_remove()

        # -- 2. Package details --
        self.info_frame = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        self.info_frame.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.info_frame.grid_columnconfigure(0, weight=1)
        self.info_frame.grid_remove()

        self.version_row = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.version_row.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        self._pkg_ver = self._stat(self.version_row, "PACKAGE", 0)
        self._cur_ver = self._stat(self.version_row, "ON DEVICE", 1)
        self._file_count = self._stat(self.version_row, "FILES", 2)

        self.files_box = ctk.CTkTextbox(
            self.info_frame, height=120, fg_color=t.CANVAS_BG, text_color=t.TEXT_DIM,
            border_width=1, border_color=t.BORDER, corner_radius=t.RADIUS, font=t.mono(11),
            wrap="none", state="disabled",
        )
        self.files_box.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))

        self.backup_cb = ctk.CTkCheckBox(
            self.info_frame, text="Back up current config before updating",
            font=t.font(13), fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
            text_color=t.TEXT_DIM,
        )
        self.backup_cb.select()
        self.backup_cb.grid(row=2, column=0, sticky="w", padx=18, pady=(0, 14))

        # -- 3. Action --
        action = self.action_row = ctk.CTkFrame(self, fg_color="transparent")
        action.grid(row=2, column=0, sticky="ew")
        action.grid_columnconfigure(1, weight=1)
        self.upload_btn = t.primary_button(action, "Flash firmware", self._start_update, width=170)
        self.upload_btn.grid(row=0, column=0, sticky="w")
        self.hint = ctk.CTkLabel(action, text="", font=t.font(12), text_color=t.WARN, anchor="w")
        self.hint.grid(row=0, column=1, padx=14, sticky="w")

        # -- 4. Switch firmware / set up a new box --
        self.switch_panel = SwitchPanel(self, app, lambda: self.package)
        self.switch_panel.grid(row=3, column=0, sticky="ew", pady=(22, 0))

        self.app.register_connection_listener(lambda c: self._refresh_action())
        self._refresh_action()
        self._fetch_latest()

    def _stat(self, parent, label: str, col: int) -> ctk.CTkLabel:
        parent.grid_columnconfigure(col, weight=1)
        chip = ctk.CTkFrame(parent, fg_color=t.SURFACE_2, corner_radius=t.RADIUS)
        chip.grid(row=0, column=col, padx=(0 if col == 0 else 8, 0), sticky="ew")
        ctk.CTkLabel(chip, text=label, font=t.font(9, "bold"), text_color=t.TEXT_MUTED, anchor="w").grid(
            row=0, column=0, padx=12, pady=(10, 0), sticky="w")
        val = ctk.CTkLabel(chip, text="—", font=t.font(14, "bold"), text_color=t.TEXT, anchor="w")
        val.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")
        return val

    def on_show(self):
        self._refresh_action()
        self.switch_panel.on_show()

    def on_hide(self):
        self.switch_panel.on_hide()

    # ----------------------------------------------------- GitHub release

    def _fetch_latest(self):
        """Fetch, download and check the latest release in the background,
        so it is ready to flash without a click. A package the user picked
        by hand is never replaced."""
        if self._fetching:
            return
        self._fetching = True
        self._latest_error = None
        self.retry_btn.grid_remove()
        self.latest_label.configure(text="Getting the latest firmware from GitHub…", text_color=t.TEXT_MUTED)

        def work():
            try:
                rel = releases.fetch_latest()
                path = releases.download(rel)
                pkg = load_firmware_zip(str(path))
                if str(pkg.firmware_version).lstrip("vV") != rel.version:
                    raise FirmwareError(f"the package says v{pkg.firmware_version} but the release is {rel.tag}")
                result = (rel, pkg, None)
            except (releases.ReleaseError, FirmwareError) as e:
                result = (None, None, str(e))
            self.app.post(lambda: self._latest_done(*result))

        threading.Thread(target=work, daemon=True).start()

    def _latest_done(self, rel, pkg, err):
        self._fetching = False
        if rel is None:
            self._latest_error = err
            self.retry_btn.grid()
        else:
            self._latest = rel
            if not self._picked_by_hand:
                self._set_package(pkg, "")
        self._refresh_action()
        self.switch_panel.refresh()

    def _show_latest(self):
        if self._fetching:
            return
        if self._latest is None:
            self.latest_label.configure(
                text=f"Couldn't get the latest firmware from GitHub: {self._latest_error}. "
                     f"You can choose a .zip instead.", text_color=t.WARN)
            return
        rel = self._latest
        info = self.app.device.info if self.app.device.connected else None
        if info is None:
            text, color = f"v{rel.version}, the latest release, is downloaded and checked.", t.TEXT_MUTED
        elif _version_less(info.version, rel.version):
            text, color = f"v{rel.version} is ready to install. Your box runs v{info.version}.", t.ACCENT
        else:
            text, color = f"Your box is up to date: it runs v{info.version}, the latest release.", t.TEXT_MUTED
        self.latest_label.configure(text=text, text_color=color)

    def _refresh_action(self):
        # Keep the "on device" chip current: it used to be written only when a
        # package was chosen, so it showed the pre-flash version after an
        # update and the previous device's version after switching boxes.
        if self.app.device.connected and self.app.device.info:
            self._cur_ver.configure(text=f"v{self.app.device.info.version}")
        else:
            self._cur_ver.configure(text="—")
        self._show_latest()   # the box may have changed

        # Without a box on the SIMINPUT firmware there is nothing to flash:
        # keep the page to the latest release and the switch / new box card.
        connected = self.app.device.connected
        if connected and self.package:
            self.info_frame.grid()
        else:
            self.info_frame.grid_remove()
        if connected:
            self.action_row.grid()
        else:
            self.action_row.grid_remove()

        if not self.package:
            self.upload_btn.configure(state="disabled", fg_color=t.SURFACE_3, text_color=t.TEXT_MUTED)
            self.hint.configure(text="Select a firmware package to begin.", text_color=t.TEXT_MUTED)
        elif not self.app.device.connected:
            self.upload_btn.configure(state="disabled", fg_color=t.SURFACE_3, text_color=t.TEXT_MUTED)
            self.hint.configure(text="Connect a device on the Device tab first.", text_color=t.WARN)
        else:
            self.upload_btn.configure(state="normal", fg_color=t.ACCENT, text_color="#FFFFFF")
            self.hint.configure(text="")

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="Select Firmware Package",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            pkg = load_firmware_zip(path)
        except FirmwareError as e:
            # Keep whatever package was loaded; just say why this one isn't usable.
            self.file_label.configure(text=f"{Path(path).name}: {e}", text_color=t.ERROR)
            return
        self._picked_by_hand = True
        self._set_package(pkg, f"Using {Path(path).name}")
        self.switch_panel.refresh()

    def _set_package(self, pkg: FirmwarePackage, label: str):
        self.package = pkg
        self.file_label.configure(text=label, text_color=t.TEXT)

        self._pkg_ver.configure(text=f"v{self.package.firmware_version}")
        self._file_count.configure(text=str(len(self.package.files)))

        self.files_box.configure(state="normal")
        self.files_box.delete("1.0", "end")
        for f in self.package.files:
            self.files_box.insert("end", f"{f.path:<28} {f.size:>7} bytes\n")
        self.files_box.configure(state="disabled")

        self._refresh_action()

    def _start_update(self):
        if not self.package or not self.app.device.connected:
            return
        backup = bool(self.backup_cb.get())
        pkg = self.package
        device = self.app.device

        def work(ctx):
            def progress(sent: int, total: int, fname: str):
                frac = sent / total if total else 0
                ctx.progress(frac)
                ctx.status(f"{fname} — {sent}/{total} bytes ({frac:.0%})")

            try:
                version = perform_update(
                    device, pkg, backup_config=backup,
                    on_status=ctx.log, on_progress=progress,
                    should_cancel=lambda: ctx.cancelled,
                )
            except UpdateCancelled:
                raise OperationCancelled()

            full = None
            if device.connected:
                try:
                    full = device.get_info()
                except Exception:
                    pass
            return version, full

        def on_success(result):
            version, full = result
            if self.app.device.connected and self.app.device.info:
                self.app.notify_connected(self.app.device.info, full)
            self._refresh_action()
            self.app.show_status(f"Firmware updated to v{version}", "success", 6000)

        self.app.run_operation(
            "Flashing firmware", work,
            on_success=on_success, success_message="Firmware updated",
            indeterminate=False,
        )
