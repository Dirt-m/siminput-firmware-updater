from __future__ import annotations

import hashlib
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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


def _own_version() -> str:
    try:
        from importlib.metadata import version
        return version("siminput-updater")
    except Exception:
        return "0"


def _version_less(a: str, b: str) -> bool:
    """True when version string a < b (dotted-integer comparison; unparsable
    parts compare as 0, so malformed manifest values never block a load)."""
    def parts(v: str) -> list[int]:
        out = []
        for piece in v.split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    pa, pb = parts(a), parts(b)
    length = max(len(pa), len(pb))
    pa += [0] * (length - len(pa))
    pb += [0] * (length - len(pb))
    return pa < pb


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
            if not isinstance(manifest, dict):
                raise FirmwareError("manifest.json must contain a JSON object")

            fw_version = manifest.get("firmware_version", "unknown")

            min_updater = manifest.get("min_updater_version")
            if min_updater and _version_less(_own_version(), str(min_updater)):
                raise FirmwareError(
                    f"This package needs configurator v{min_updater} or newer "
                    f"(you have v{_own_version()}). Update the app first."
                )

            entries = manifest.get("files", [])
            if not isinstance(entries, list) or not entries:
                raise FirmwareError("Manifest lists no files — not a usable package")

            files: list[FirmwareFile] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise FirmwareError("Malformed manifest: file entries must be objects")
                path = entry["path"]
                expected_sha = entry["sha256"]
                expected_size = entry["size"]

                if not _is_path_allowed(path):
                    raise FirmwareError(f"Disallowed file path in manifest: {path}")

                if path not in zf.namelist():
                    raise FirmwareError(f"File listed in manifest but missing from zip: {path}")

                # Check the declared size against the zip directory before
                # decompressing, so a bogus entry can't balloon in memory.
                stored_size = zf.getinfo(path).file_size
                if stored_size != expected_size:
                    raise FirmwareError(
                        f"{path}: size mismatch (manifest says {expected_size}, "
                        f"actual {stored_size})"
                    )

                data = zf.read(path)

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

    except FirmwareError:
        raise
    except zipfile.BadZipFile:
        raise FirmwareError("Not a valid zip file")
    except KeyError as e:
        raise FirmwareError(f"Missing required field in manifest: {e}")
    except Exception as e:
        # Corrupt manifests, unreadable files, encrypted zips, bad deflate
        # streams: everything must surface as FirmwareError — in the windowed
        # build an uncaught exception here is completely invisible.
        raise FirmwareError(f"Could not read firmware package: {e}")


def upload_order(files: list[FirmwareFile]) -> list[FirmwareFile]:
    def rank(f: FirmwareFile) -> int:
        if f.path.startswith("lib/"):
            return 0
        if f.path == "boot.py":
            return 2
        if f.path == "code.py":
            return 3
        return 1
    return sorted(files, key=rank)


def _write_backup(config: dict) -> Path:
    backup_dir = Path.home() / "SIMINPUT Backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"config-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


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

    backup_path = None
    if backup_config:
        status("Backing up current configuration...")
        try:
            backup_path = _write_backup(device.get_config())
            status(f"  Config backed up to {backup_path}")
        except Exception as e:
            status(f"Warning: could not back up config ({e})")

    ordered = upload_order(package.files)
    total_bytes = sum(f.size for f in ordered)
    uploaded_bytes = 0

    # Older firmware has no update_begin — probe for it, but only fall back
    # to direct (non-staged) writes when the device explicitly says the
    # command is unknown. Any other failure — a timeout, "already in
    # progress", a serial error — aborts the update: falling back on those
    # used to end with every file silently diverted into the device's staging
    # directory and a success report for a flash that changed nothing.
    transactional = False
    try:
        device.update_begin()
        transactional = True
        status("Starting transactional update (staged writes)...")
    except Exception as e:
        if "unknown command" in str(e).lower():
            status("Firmware has no staged updates — writing files directly")
        else:
            raise FirmwareError(f"Could not start staged update: {e}")

    try:
        for f in ordered:
            check_cancel()
            status(f"Uploading {f.path} ({f.size} bytes)...")

            def file_progress(sent: int, total: int):
                nonlocal uploaded_bytes
                if on_progress:
                    on_progress(uploaded_bytes + sent, total_bytes, f.path)

            device.file_write(
                f.path, f.data,
                progress=file_progress,
                should_cancel=should_cancel,
            )
            uploaded_bytes += f.size
            status(f"  {f.path} written successfully")

        if transactional:
            status("All files staged — committing update...")
            committed = device.update_commit()
            status(f"  Committed {len(committed)} files")
    except Exception:
        if transactional:
            status("Upload failed — aborting staged update...")
            if device.update_abort():
                status("  Staged files discarded, device unchanged")
            else:
                status("  Warning: could not confirm the abort — the device may "
                       "still hold staged files (they are discarded on its next update)")
        else:
            status("  Warning: the device may be partially updated — re-run the "
                   "update before unplugging")
        raise

    status("Rebooting device...")
    port_name = device.info.port if device.info else ""
    # Hard reset where supported: a soft reload never re-runs boot.py, so a
    # freshly flashed boot.py (USB name/PID) would not take effect until the
    # user physically unplugs the box.
    try:
        device.reboot(hard=True)
    except TypeError:
        device.reboot()

    status("Waiting for device to restart...")
    try:
        info = device.wait_for_reconnect(port_name)
        status(f"Device reconnected: {info.name} v{info.version}")
    except Exception as e:
        status(f"Could not reconnect: {e}")
        if backup_path:
            status(f"Your config backup is at {backup_path}")
        raise

    if info.version != package.firmware_version:
        # A version mismatch after a "successful" flash means the update did
        # not actually land (e.g. files staged but never committed). Fail
        # loudly instead of logging a warning nobody reads.
        raise FirmwareError(
            f"Device reports v{info.version} after the update, expected "
            f"v{package.firmware_version} — the update did not take effect"
        )
    status(f"Update successful: now running v{info.version}")
    return info.version
