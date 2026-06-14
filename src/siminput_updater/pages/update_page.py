from __future__ import annotations

from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING

import customtkinter as ctk

from .. import ui_theme as t
from ..firmware import FirmwareError, UpdateCancelled, load_firmware_zip, perform_update
from ..operations import OperationCancelled

if TYPE_CHECKING:
    from ..app import App


class UpdatePage(ctk.CTkFrame):
    def __init__(self, parent, app: App):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.package = None

        self.grid_columnconfigure(0, weight=1)

        # -- 1. Select package --
        select = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS)
        select.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        select.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(select, text="Firmware package", font=t.font(15, "bold"), text_color=t.TEXT).grid(
            row=0, column=0, columnspan=3, padx=18, pady=(16, 2), sticky="w")
        ctk.CTkLabel(
            select, text="Choose a signed .zip built for this controller.",
            font=t.font(12), text_color=t.TEXT_MUTED,
        ).grid(row=1, column=0, columnspan=3, padx=18, pady=(0, 14), sticky="w")

        self.select_btn = t.ghost_button(select, "Choose .zip…", self._select_file, width=140)
        self.select_btn.grid(row=2, column=0, padx=18, pady=(0, 16))
        self.file_label = ctk.CTkLabel(select, text="No file selected", font=t.font(13), text_color=t.TEXT_MUTED, anchor="w")
        self.file_label.grid(row=2, column=1, padx=(0, 18), pady=(0, 16), sticky="w")

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

    def _refresh_action(self):
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
            self.package = load_firmware_zip(path)
        except FirmwareError as e:
            self.file_label.configure(text=f"Error: {e}", text_color=t.ERROR)
            self.info_frame.grid_remove()
            self.package = None
            self._refresh_action()
            return

        self.file_label.configure(text=Path(path).name, text_color=t.TEXT)

        current = "unknown"
        if self.app.device.connected and self.app.device.info:
            current = self.app.device.info.version
        self._pkg_ver.configure(text=f"v{self.package.firmware_version}")
        self._cur_ver.configure(text=f"v{current}" if current != "unknown" else "—")
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
            self.app.show_status(f"Firmware updated to v{version}", "success", 6000)

        self.app.run_operation(
            "Flashing firmware", work,
            on_success=on_success, success_message="Firmware updated",
            indeterminate=False,
        )
