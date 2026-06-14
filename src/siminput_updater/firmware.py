from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

ALLOWED_PATHS = {"config.json", "code.py", "boot.py"}
ALLOWED_PREFIXES = ("lib/",)


@dataclass
class FirmwareFile:
    path: str
    sha256: str
    size: int
    data: bytes | None = None


@dataclass
class FirmwarePackage:
    firmware_version: str
    files: list[FirmwareFile]

    @property
    def description(self) -> str:
        return f"Firmware v{self.firmware_version} ({len(self.files)} files)"


class FirmwareError(Exception):
    pass


class UpdateCancelled(FirmwareError):
    pass


def _is_path_allowed(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    if ".." in normalized or normalized.startswith("/"):
        return False
    if normalized in ALLOWED_PATHS:
        return True
    return any(normalized.startswith(p) for p in ALLOWED_PREFIXES)


def load_firmware_zip(zip_path: str) -> FirmwarePackage:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "manifest.json" not in zf.namelist():
                raise FirmwareError("Missing manifest.json in firmware package")

            manifest = json.loads(zf.read("manifest.json"))

            fw_version = manifest.get("firmware_version", "unknown")

            files: list[FirmwareFile] = []
            for entry in manifest.get("files", []):
                path = entry["path"]
                expected_sha = entry["sha256"]
                expected_size = entry["size"]

                if not _is_path_allowed(path):
                    raise FirmwareError(f"Disallowed file path in manifest: {path}")

                if path not in zf.namelist():
                    raise FirmwareError(f"File listed in manifest but missing from zip: {path}")

                data = zf.read(path)

                if len(data) != expected_size:
                    raise FirmwareError(
                        f"{path}: size mismatch (manifest says {expected_size}, "
                        f"actual {len(data)})"
                    )

                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != expected_sha:
                    raise FirmwareError(
                        f"{path}: checksum mismatch (manifest says {expected_sha[:12]}..., "
                        f"actual {actual_sha[:12]}...)"
                    )

                files.append(FirmwareFile(
                    path=path,
                    sha256=expected_sha,
                    size=expected_size,
                    data=data,
                ))

            return FirmwarePackage(
                firmware_version=fw_version,
                files=files,
            )

    except zipfile.BadZipFile:
        raise FirmwareError("Not a valid zip file")
    except KeyError as e:
        raise FirmwareError(f"Missing required field in manifest: {e}")


def upload_order(files: list[FirmwareFile]) -> list[FirmwareFile]:
    libs = [f for f in files if f.path.startswith("lib/")]
    boot = [f for f in files if f.path == "boot.py"]
    code = [f for f in files if f.path == "code.py"]
    other = [f for f in files if f not in libs and f not in boot and f not in code]
    return libs + other + boot + code


def perform_update(
    device,
    package: FirmwarePackage,
    backup_config: bool = True,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    def status(msg: str):
        if on_status:
            on_status(msg)

    # Cooperative cancellation checkpoint. Checked between files (and before the
    # backup) so an abort stops cleanly at a file boundary; the transactional
    # path then discards staged writes via update_abort().
    def check_cancel():
        if should_cancel and should_cancel():
            raise UpdateCancelled("Update aborted by user")

    check_cancel()

    config_backup = None
    if backup_config:
        status("Backing up current configuration...")
        try:
            config_backup = device.get_config()
        except Exception:
            status("Warning: could not back up config")

    ordered = upload_order(package.files)
    total_bytes = sum(f.size for f in ordered)
    uploaded_bytes = 0

    transactional = hasattr(device, "update_begin")

    if transactional:
        status("Starting transactional update (staged writes)...")
        device.update_begin()

    try:
        for f in ordered:
            check_cancel()
            status(f"Uploading {f.path} ({f.size} bytes)...")

            def file_progress(sent: int, total: int):
                nonlocal uploaded_bytes
                if on_progress:
                    on_progress(uploaded_bytes + sent, total_bytes, f.path)

            device.file_write(f.path, f.data, progress=file_progress)
            uploaded_bytes += f.size
            status(f"  {f.path} written successfully")

        if transactional:
            status("All files staged — committing update...")
            committed = device.update_commit()
            status(f"  Committed {len(committed)} files")
    except Exception:
        if transactional:
            status("Upload failed — aborting staged update...")
            device.update_abort()
            status("  Staged files discarded, device unchanged")
        raise

    status("Rebooting device...")
    port_name = device.info.port if device.info else ""
    device.reboot()

    status("Waiting for device to restart...")
    try:
        info = device.wait_for_reconnect(port_name)
        status(f"Device reconnected: {info.name} v{info.version}")
        if info.version == package.firmware_version:
            status(f"Update successful: now running v{info.version}")
        else:
            status(
                f"Warning: expected v{package.firmware_version}, "
                f"device reports v{info.version}"
            )
        return info.version
    except Exception as e:
        status(f"Could not reconnect: {e}")
        if config_backup:
            status("Config backup was saved before update")
        raise
