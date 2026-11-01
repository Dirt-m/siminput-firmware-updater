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

if TYPE_CHECKING:
    from ..app import App


class UpdatePage(ctk.CTkFrame):
    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.package = None
        self._latest: releases.Release | None = None
        self._latest_checked = False

        self.grid_columnconfigure(0, weight=1)

        # -- 1. Select package --
        select = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        select.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        select.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(select, text="Firmware package", font=t.font(15, "bold"), text_color=t.TEXT).grid(
            row=0, column=0, columnspan=3, padx=18, pady=(16, 2), sticky="w")
        ctk.CTkLabel(
            select, text="Download the latest release, or choose a firmware .zip built for this controller.",
            font=t.font(12), text_color=t.TEXT_MUTED,
        ).grid(row=1, column=0, columnspan=3, padx=18, pady=(0, 14), sticky="w")

        self.download_btn = t.ghost_button(select, "Download latest", self._download_latest, width=150)
        self.download_btn.grid(row=2, column=0, padx=(18, 8), pady=(0, 6), sticky="w")
        self.select_btn = t.ghost_button(select, "Choose .zip…", self._select_file, width=130)
        self.select_btn.grid(row=2, column=1, padx=0, pady=(0, 6), sticky="w")
        self.file_label = ctk.CTkLabel(select, text="No file selected", font=t.font(13), text_color=t.TEXT_MUTED, anchor="w")
        self.file_label.grid(row=2, column=2, padx=(12, 18), pady=(0, 6), sticky="w")
        self.latest_label = ctk.CTkLabel(select, text="", font=t.font(12), text_color=t.TEXT_MUTED, anchor="w")
        self.latest_label.grid(row=3, column=0, columnspan=3, padx=18, pady=(0, 14), sticky="w")

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
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.grid(row=2, column=0, sticky="ew")
        action.grid_columnconfigure(1, weight=1)
        self.upload_btn = t.primary_button(action, "Flash firmware", self._start_update, width=170)
        self.upload_btn.grid(row=0, column=0, sticky="w")
        self.hint = ctk.CTkLabel(action, text="", font=t.font(12), text_color=t.WARN, anchor="w")
        self.hint.grid(row=0, column=1, padx=14, sticky="w")

        self.app.register_connection_listener(lambda c: self._refresh_action())
        self._refresh_action()

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
        if not self._latest_checked:
            self._latest_checked = True
            self._check_latest()

    # ----------------------------------------------------- GitHub release

    def _check_latest(self):
        """Look up the latest release in the background (once per session)
        and say how it compares with the box."""
        self.latest_label.configure(text="Checking GitHub for the latest firmware…", text_color=t.TEXT_MUTED)

        def work():
            try:
                rel, err = releases.fetch_latest(), None
            except releases.ReleaseError as e:
                rel, err = None, str(e)
            self.app.post(lambda: self._show_latest(rel, err))

        threading.Thread(target=work, daemon=True).start()

    def _show_latest(self, rel: releases.Release | None, err: str | None):
        self._latest = rel
        if rel is None:
            self.latest_label.configure(text=f"{err}. You can still choose a .zip.", text_color=t.TEXT_MUTED)
            return
        text, color = f"Latest release: v{rel.version}", t.TEXT_MUTED
        info = self.app.device.info if self.app.device.connected else None
        if info is not None:
            if _version_less(info.version, rel.version):
                text, color = f"v{rel.version} is available. Your box runs v{info.version}.", t.ACCENT
            else:
                text = f"Your box runs v{info.version}, the latest release is v{rel.version}."
        self.latest_label.configure(text=text, text_color=color)

    def _download_latest(self):
        known = self._latest

        def work(ctx):
            ctx.status("Looking up the latest release…")
            rel = known or releases.fetch_latest()
            ctx.status(f"Downloading {rel.asset_name}…")
            path = releases.download(
                rel, progress=lambda got, total: ctx.progress(got / total if total else 0),
                should_cancel=lambda: ctx.cancelled)
            if ctx.cancelled:
                raise OperationCancelled()
            ctx.status("Checking the package…")
            pkg = load_firmware_zip(str(path))
            if str(pkg.firmware_version).lstrip("vV") != rel.version:
                raise FirmwareError(f"The package says v{pkg.firmware_version} but the release is {rel.tag}")
            return rel, path, pkg

        def on_success(result):
            rel, path, pkg = result
            self._latest = rel
            self._set_package(pkg, f"{path.name} (from GitHub)")

        self.app.run_operation("Downloading firmware", work, on_success=on_success,
                               success_message="Firmware downloaded and checked", indeterminate=False)

    def _refresh_action(self):
        # Keep the "on device" chip current: it used to be written only when a
        # package was chosen, so it showed the pre-flash version after an
        # update and the previous device's version after switching boxes.
        if self.app.device.connected and self.app.device.info:
            self._cur_ver.configure(text=f"v{self.app.device.info.version}")
        else:
            self._cur_ver.configure(text="—")
        if self._latest is not None:
            self._show_latest(self._latest, None)   # the box may have changed

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
            self.file_label.configure(text=f"Error: {e}", text_color=t.ERROR)
            self.info_frame.grid_remove()
            self.package = None
            self._refresh_action()
            return
        self._set_package(pkg, Path(path).name)

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

        self.info_frame.grid()
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
