"""Find the box's USB drives (RPI-RP2, CIRCUITPY) by volume label and write
files onto them so they really land on the flash.

- Windows: the drive letter whose volume label matches.
- macOS: /Volumes/<label> (or "<label> 1" when macOS had to rename it).
- Linux: udisks style mount points (/run/media/$USER/<label>, /media/$USER/<label>,
  /media/<label>), plus any mount point in /proc/mounts with that name.

A label alone is not proof: an empty leftover mount point does not count,
and RPI-RP2 must carry an INFO_UF2.TXT naming the RP2040 bootloader.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

BOOTSEL = "RPI-RP2"
CIRCUITPY = "CIRCUITPY"


def _windows_drives(label: str) -> list[Path]:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    out = []
    mask = kernel32.GetLogicalDrives()
    # A drive letter with no medium must not pop up a "no disk" dialog.
    old_mode = kernel32.SetErrorMode(0x0001)
    try:
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if kernel32.GetDriveTypeW(root) != 2:   # DRIVE_REMOVABLE
                continue
            name = ctypes.create_unicode_buffer(261)
            ok = kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), name, len(name), None, None, None, None, 0)
            if ok and name.value.upper() == label.upper():
                out.append(Path(root))
    finally:
        kernel32.SetErrorMode(old_mode)
    return out


def _mac_drives(label: str, volumes: Path = Path("/Volumes")) -> list[Path]:
    return [p for p in (volumes / label, volumes / f"{label} 1") if p.is_dir()]


def _linux_drives(label: str, mounts_file: Path = Path("/proc/mounts"),
                  bases: list[Path] | None = None) -> list[Path]:
    if bases is None:
        try:
            user = getpass.getuser()
        except Exception:
            user = os.environ.get("USER", "")
        bases = [Path("/run/media") / user, Path("/media") / user, Path("/media")]
    found = [b / label for b in bases]
    try:
        for line in mounts_file.read_text().splitlines():
            parts = line.split()
            if len(parts) > 1:
                # /proc/mounts escapes spaces and the like as octal.
                mount = parts[1].encode().decode("unicode_escape")
                if Path(mount).name == label:
                    found.append(Path(mount))
    except OSError:
        pass
    return [p for p in dict.fromkeys(found) if p.is_dir()]


def candidates(label: str) -> list[Path]:
    if sys.platform.startswith("win"):
        return _windows_drives(label)
    if sys.platform == "darwin":
        return _mac_drives(label)
    return _linux_drives(label)


def looks_mounted(path: Path, label: str) -> bool:
    """A real, mounted drive with that label, not an empty leftover folder."""
    try:
        names = {p.name.upper() for p in path.iterdir()}
    except OSError:
        return False
    if label == BOOTSEL:
        info = path / "INFO_UF2.TXT"
        if "INFO_UF2.TXT" not in names:
            return False
        try:
            text = info.read_text(errors="replace")
        except OSError:
            return False
        # RP2040: "Board-ID: RPI-RP2". An RP2350 (Pico 2) says RP2350 and
        # mounts under its own label anyway.
        return "RPI-RP2" in text
    return bool(names)


def find(label: str) -> Path | None:
    for path in candidates(label):
        if looks_mounted(path, label):
            return path
    return None


def write_file(path: Path, data: bytes) -> None:
    """Write `data` to `path` and push it to the device before returning.

    Unbuffered, so an error says the device did not take the bytes. On
    RPI-RP2 the box reboots the moment it has the last block of a UF2, so
    the final flush can fail even though the copy worked: the caller decides
    by whether the drive then goes away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb", buffering=0) as f:
        view = memoryview(data)
        while view:
            n = f.write(view)
            view = view[n:]
        _fsync(f.fileno())


def _fsync(fd: int) -> None:
    if sys.platform == "darwin":
        # macOS fsync() stops at the drive cache; F_FULLFSYNC goes to the medium.
        try:
            import fcntl
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (ImportError, OSError, AttributeError):
            pass
    os.fsync(fd)


def sync_all() -> None:
    if hasattr(os, "sync"):
        try:
            os.sync()
        except OSError:
            pass
