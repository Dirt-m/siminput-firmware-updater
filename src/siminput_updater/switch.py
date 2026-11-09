"""Switching a box between the stock SIMINPUT firmware and SIMINPUT firmware
for MobiFlight, and setting up a fresh box with either.

To MobiFlight: back up config.json over serial (stop if that fails), reboot
into the bootloader, copy the MobiFlight UF2 onto RPI-RP2, wait for the box
to answer as "SIMINPUT rev2".

To SIMINPUT: 1200 baud touch (MobiFlight) or start from RPI-RP2/CIRCUITPY,
copy flash_nuke.uf2, then CircuitPython 9.2.8, then the stock firmware files
and a config.json onto CIRCUITPY. boot.py only runs on a hard reset, so the
user replugs the box; then it is checked over serial.

Nothing here deletes a backup. Every failure leaves the box somewhere the
flow can start again from: on its old firmware, in RPI-RP2, or on CIRCUITPY.

`Hardware` is the outside world (drives, MobiFlight ports); the mock app
swaps in `mock_device.MockHardware` and runs these same flows.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from . import drives, mobiflight
from .applog import LOG_DIR, log
from .config_history import Backup, list_backups
from .firmware import FirmwarePackage, _write_backup, upload_order
from .mobiflight import MobiFlightBox
from .operations import OperationCancelled

ASSETS = Path(__file__).resolve().parent / "assets"
NUKE_UF2 = "flash_nuke.uf2"
CIRCUITPYTHON_UF2 = "adafruit-circuitpython-raspberry_pi_pico-en_US-9.2.8.uf2"
CIRCUITPYTHON_VERSION = "9.2.8"
ASSET_SHA256 = {
    NUKE_UF2: "08aa7d561e8b8bf2f9b061b3506fb4d8f135e832efe0f3ae978241db2da0c853",
    CIRCUITPYTHON_UF2: "d4962d583603631faec871071d4e01ddc3934069a3bfc8dd4a7c70fd1177f920",
}
RECORDS_FILE = "firmware-switches.json"

UF2_MAGIC = (0x0A324655, 0x9E5D5157, 0x0AB16F30)
UF2_FAMILY_FLAG = 0x00002000
RP2040_FAMILY = 0xE48BFF56
MOBIFLIGHT_UF2 = re.compile(r"^siminput_rev2_([0-9A-Za-z_.\-]+)\.uf2$")

# The stock firmware's blank starting config (siminput-firmware-v2 configs/config.json).
DEFAULT_CONFIG = {
    "device": {"name": "SimInput Box", "pid": 61440, "inactivity_refresh": 1.0, "debounce_ms": 10},
    "bools": [],
    "axes": [],
    "rules": [],
}

DRIVE_TIMEOUT = 45.0        # an OS can take a while to mount a new drive
GONE_TIMEOUT = 30.0
CIRCUITPY_TIMEOUT = 120.0   # CircuitPython formats its filesystem on first boot
MOBIFLIGHT_TIMEOUT = 30.0
REPLUG_TIMEOUT = 15 * 60.0
RECONNECT_TIMEOUT = 60.0
POLL_S = 0.25


class SwitchError(Exception):
    pass


class Context(Protocol):
    """What the flows need from operations.OperationContext."""
    @property
    def cancelled(self) -> bool: ...
    def check_cancel(self) -> None: ...
    def status(self, message: str) -> None: ...
    def log(self, message: str) -> None: ...
    def progress(self, fraction: float) -> None: ...


class Hardware(Protocol):
    def find_drive(self, label: str) -> Path | None: ...
    def mobiflight_ports(self) -> list[tuple[str, str]]: ...
    def probe_mobiflight(self, port: str, usb_serial: str = "") -> MobiFlightBox: ...
    def touch_1200(self, port: str) -> None: ...
    def write_file(self, path: Path, data: bytes) -> None: ...
    def community_dir(self) -> Path | None: ...


class RealHardware:
    def find_drive(self, label: str) -> Path | None:
        return drives.find(label)

    def mobiflight_ports(self) -> list[tuple[str, str]]:
        return mobiflight.ports()

    def probe_mobiflight(self, port: str, usb_serial: str = "") -> MobiFlightBox:
        return mobiflight.probe(port, usb_serial)

    def touch_1200(self, port: str) -> None:
        mobiflight.touch_1200(port)

    def write_file(self, path: Path, data: bytes) -> None:
        drives.write_file(path, data)

    def community_dir(self) -> Path | None:
        return mobiflight.community_dir()


# ------------------------------------------------------------------ files

def check_uf2(data: bytes, what: str) -> None:
    """A UF2 for the RP2040, or SwitchError. Every block is checked: a
    truncated download or the wrong chip's file never reaches the box."""
    if not data or len(data) % 512:
        raise SwitchError(f"{what} is not a UF2 file")
    for off in range(0, len(data), 512):
        m0, m1, flags = struct.unpack_from("<III", data, off)
        (end,) = struct.unpack_from("<I", data, off + 508)
        if (m0, m1, end) != UF2_MAGIC:
            raise SwitchError(f"{what} is not a UF2 file")
        family = struct.unpack_from("<I", data, off + 28)[0]
        if flags & UF2_FAMILY_FLAG and family != RP2040_FAMILY:
            raise SwitchError(f"{what} is not built for the RP2040")


def asset(name: str) -> bytes:
    """A bundled UF2, checked against its known hash."""
    try:
        data = (ASSETS / name).read_bytes()
    except OSError as e:
        raise SwitchError(f"The app is missing {name} ({e})") from e
    if hashlib.sha256(data).hexdigest() != ASSET_SHA256[name]:
        raise SwitchError(f"The app's copy of {name} is damaged. Reinstall the Configurator.")
    return data


@dataclass
class MobiFlightPackage:
    version: str
    uf2_name: str
    uf2: bytes
    zip_bytes: bytes | None = None    # the Connector package, when loaded from the release zip
    source: str = ""

    @property
    def has_community(self) -> bool:
        return self.zip_bytes is not None


def load_mobiflight_package(path: str | Path) -> MobiFlightPackage:
    """A siminput-mobiflight release zip, or the bare UF2 from one."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SwitchError(f"Couldn't read {path.name}: {e}") from e
    if path.suffix.lower() == ".uf2":
        m = MOBIFLIGHT_UF2.match(path.name)
        version = m.group(1).replace("_", ".") if m else "?"
        check_uf2(raw, path.name)
        return MobiFlightPackage(version, path.name, raw, None, path.name)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            uf2s = [n for n in z.namelist()
                    if PurePosixPath(n).parent == PurePosixPath("SIMINPUT/firmware")
                    and MOBIFLIGHT_UF2.match(PurePosixPath(n).name)]
            if len(uf2s) != 1:
                raise SwitchError("This zip isn't a SIMINPUT firmware for MobiFlight release "
                                  "(no SIMINPUT/firmware/siminput_rev2_*.uf2)")
            name = PurePosixPath(uf2s[0]).name
            uf2 = z.read(uf2s[0])
    except zipfile.BadZipFile as e:
        raise SwitchError(f"{path.name} is not a valid zip file") from e
    check_uf2(uf2, name)
    version = MOBIFLIGHT_UF2.match(name).group(1).replace("_", ".")
    return MobiFlightPackage(version, name, uf2, raw, path.name)


# ---------------------------------------------------------------- records
#
# One record per switch to MobiFlight: which backup holds the box's config
# and every id the box was seen with (USB serial on the stock firmware, USB
# serial and MobiFlight serial on the new one). That is how "the newest
# backup for that box" is found on the way back.

def _records_path() -> Path:
    return LOG_DIR / RECORDS_FILE


def load_records(path: Path | None = None) -> list[dict]:
    path = path or _records_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _save_records(records: list[dict], path: Path | None = None) -> None:
    path = path or _records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_record(backup: Path, name: str, ids: list[str], path: Path | None = None) -> str:
    records = load_records(path)
    rid = time.strftime("%Y%m%d-%H%M%S") + f"-{len(records)}"
    records.append({"id": rid, "backup": str(backup), "name": name,
                    "ids": [i for i in ids if i], "state": "switching"})
    _save_records(records, path)
    return rid


def update_record(rid: str | None, *, state: str | None = None, ids: list[str] = (),
                  path: Path | None = None) -> None:
    if not rid:
        return
    records = load_records(path)
    for r in records:
        if r.get("id") == rid:
            if state:
                r["state"] = state
            r["ids"] = list(dict.fromkeys([*r.get("ids", []), *[i for i in ids if i]]))
    try:
        _save_records(records, path)
    except OSError as e:
        log.warning("couldn't update firmware switch record: %s", e)


@dataclass
class RestoreChoice:
    """Which config a switch back to SIMINPUT writes."""
    backup: Backup | None          # None: the default config
    record_id: str | None = None
    matched: bool = False          # the backup is known to belong to this box

    @property
    def label(self) -> str:
        if self.backup is None:
            return "A blank configuration (no backup found)"
        b = self.backup
        when = b.when.strftime("%d %b %Y, %H:%M")
        name = b.name or "Unnamed box"
        return f"{name}, backed up {when} ({b.rules} rule{'' if b.rules == 1 else 's'})"

    def config(self) -> dict:
        if self.backup is None:
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            data = json.loads(self.backup.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SwitchError(f"Couldn't read the backup {self.backup.path.name}: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("device"), dict):
            raise SwitchError(f"The backup {self.backup.path.name} isn't a SIMINPUT config")
        return data


def _backup_for(path_str: str, backups: list[Backup]) -> Backup | None:
    target = Path(path_str)
    for b in backups:
        try:
            if b.path.resolve() == target.resolve():
                return b if b.readable else None
        except OSError:
            continue
    return None


def choose_restore(box_ids: list[str] | None, records: list[dict] | None = None,
                   backups: list[Backup] | None = None) -> RestoreChoice:
    """The config to restore.

    `box_ids`: ids of the box being switched back (MobiFlight USB serial and
    serial); None for a box in RPI-RP2 or on CIRCUITPY, which can't say who
    it is. Order: the newest switch record for this box; for an anonymous box
    the newest switch that hasn't been switched back yet (most likely this
    box, half way); else the newest readable backup for a known box; else
    the default config.
    """
    records = load_records() if records is None else records
    backups = list_backups() if backups is None else backups
    for r in reversed(records):
        if r.get("state") == "restored":
            continue
        if box_ids is not None and not set(box_ids) & set(r.get("ids", [])):
            continue
        b = _backup_for(str(r.get("backup", "")), backups)
        if b is not None:
            return RestoreChoice(b, r.get("id"), matched=box_ids is not None)
    if box_ids is not None:
        for r in reversed(records):
            if set(box_ids) & set(r.get("ids", [])):
                b = _backup_for(str(r.get("backup", "")), backups)
                if b is not None:
                    return RestoreChoice(b, r.get("id"), matched=True)
        newest = next((b for b in backups if b.readable), None)
        if newest is not None:
            return RestoreChoice(newest)
    return RestoreChoice(None)


# ------------------------------------------------------------------ state

@dataclass
class Scan:
    """What is plugged in, beyond a stock box on serial."""
    bootsel: Path | None = None
    circuitpy: Path | None = None
    circuitpy_version: str = ""      # from boot_out.txt
    boxes: list[MobiFlightBox] = field(default_factory=list)

    @property
    def siminput_boxes(self) -> list[MobiFlightBox]:
        return [b for b in self.boxes if b.is_siminput]

    @property
    def busy_boxes(self) -> list[MobiFlightBox]:
        return [b for b in self.boxes if b.status == "busy"]


def circuitpy_version(drive: Path) -> str:
    """'9.2.8' from boot_out.txt on an RP2040 Pico build, else ''."""
    try:
        text = (drive / "boot_out.txt").read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"CircuitPython (\d+\.\d+\.\d+\S*)", text)
    if not m or "rp2040" not in text.lower():
        return ""
    return m.group(1)


class Scanner:
    """Remembers probe results per (port, usb serial), so polling opens a
    board's port once per plug-in instead of every tick: 2E8A:000A is every
    arduino-pico board, including the user's own projects. Only a busy port
    is probed again, so closing MobiFlight Connector is noticed."""

    def __init__(self, hw: Hardware):
        self.hw = hw
        self._cache: dict[tuple[str, str], MobiFlightBox] = {}

    def forget(self) -> None:
        self._cache.clear()

    def scan(self) -> Scan:
        s = Scan()
        s.bootsel = self.hw.find_drive(drives.BOOTSEL)
        s.circuitpy = self.hw.find_drive(drives.CIRCUITPY)
        if s.circuitpy:
            s.circuitpy_version = circuitpy_version(s.circuitpy)
        present = self.hw.mobiflight_ports()
        self._cache = {k: v for k, v in self._cache.items() if k in present}
        for key in present:
            box = self._cache.get(key)
            if box is None or box.status == "busy":
                box = self.hw.probe_mobiflight(*key)
                self._cache[key] = box
            s.boxes.append(box)
        return s


# ------------------------------------------------------------------ flows

class _Flow:
    """Shared plumbing: step progress, waits that honour cancel, and a
    running note of where the box is, logged whenever the flow stops."""

    def __init__(self, hw: Hardware, ctx: Context, steps: int):
        self.hw = hw
        self.ctx = ctx
        self.steps = steps
        self.step = 0
        self.where = "Nothing has changed on the box."

    def next(self, status: str) -> None:
        self.ctx.progress(self.step / self.steps)
        self.step += 1
        self.ctx.status(status)
        self.ctx.log(status)

    def wait(self, what: str, fn: Callable[[], object], timeout: float, hint: str = ""):
        end = time.monotonic() + timeout
        while True:
            self.ctx.check_cancel()
            value = fn()
            if value:
                return value
            if time.monotonic() > end:
                raise SwitchError(f"{what} didn't happen within {timeout:.0f} s. {hint}".strip())
            time.sleep(POLL_S)

    def drive(self, label: str, timeout: float | None = None) -> Path:
        timeout = DRIVE_TIMEOUT if timeout is None else timeout
        hint = (f"If your computer doesn't mount USB drives on its own, open the "
                f"{label} drive in your file manager. Then try again.")
        return self.wait(f"The {label} drive appearing", lambda: self.hw.find_drive(label), timeout, hint)

    def gone(self, label: str, timeout: float | None = None) -> None:
        timeout = GONE_TIMEOUT if timeout is None else timeout
        self.wait(f"The {label} drive closing", lambda: self.hw.find_drive(label) is None, timeout,
                  "Unplug the box, plug it back in, and try again.")

    def copy_uf2(self, data: bytes, name: str) -> None:
        drive = self.drive(drives.BOOTSEL)
        self.ctx.log(f"  copying {name}")
        try:
            self.hw.write_file(drive / name, data)
        except OSError as e:
            # The box reboots on the last block; the closing flush can fail.
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if self.hw.find_drive(drives.BOOTSEL) is None:
                    return
                time.sleep(POLL_S)
            raise SwitchError(f"Copying {name} failed: {e}") from e
        self.gone(drives.BOOTSEL)

    def stopped(self, e: BaseException) -> None:
        if isinstance(e, OperationCancelled):
            self.ctx.log(f"Stopped. {self.where}")
        else:
            self.ctx.log(f"Stopped: {e}")
            self.ctx.log(self.where)


def to_mobiflight(device, hw: Hardware, pkg: MobiFlightPackage, ctx: Context,
                  community: Path | None = None) -> MobiFlightBox | None:
    """Stock box connected over serial -> SIMINPUT firmware for MobiFlight."""
    flow = _Flow(hw, ctx, steps=5)
    try:
        flow.next("Checking the box…")
        full = device.get_info()
        rev = device.info.board_map if device.info else ""
        if rev != "rev2" or full.fault == "no_expander":
            raise SwitchError("SIMINPUT firmware for MobiFlight is for rev2 boxes only, and this "
                              "box doesn't report as rev2.")
        name = device.info.name

        flow.next("Backing up the configuration…")
        config = device.get_config()
        if not isinstance(config, dict) or not isinstance(config.get("device"), dict):
            raise SwitchError("The box sent back an empty configuration, so there is nothing safe "
                              "to back up. Nothing was changed.")
        try:
            backup = _write_backup(config)
            if json.loads(backup.read_text(encoding="utf-8")) != config:
                raise SwitchError("the saved copy doesn't match")
        except (OSError, ValueError, SwitchError) as e:
            raise SwitchError(f"Couldn't back up the configuration ({e}). Nothing was changed.") from e
        ctx.log(f"  saved to {backup}")
        rid = add_record(backup, name, [device.usb_serial or ""])
        flow.where = f"The box is unchanged. Your configuration is backed up in {backup}."
        ctx.check_cancel()

        flow.next("Restarting the box into its bootloader…")
        device.enter_bootloader()
        device.disconnect()
        flow.where = (f"The box may be in bootloader mode (an RPI-RP2 drive). Start the switch "
                      f"again from the Update page. Your configuration is backed up in {backup}.")
        flow.drive(drives.BOOTSEL)

        flow.next(f"Installing SIMINPUT firmware for MobiFlight v{pkg.version}…")
        flow.copy_uf2(pkg.uf2, pkg.uf2_name)

        box = _await_mobiflight(flow)
        if box is not None:
            update_record(rid, state="mobiflight", ids=box.ids)
        flow.where = f"Your configuration is backed up in {backup}."
        _install_community(flow, pkg, community)
        ctx.progress(1.0)
        return box
    except BaseException as e:
        flow.stopped(e)
        raise


def install_mobiflight(hw: Hardware, pkg: MobiFlightPackage, ctx: Context,
                       community: Path | None = None) -> MobiFlightBox | None:
    """A box in RPI-RP2 (a fresh board, or one held on BOOTSEL) -> MobiFlight."""
    flow = _Flow(hw, ctx, steps=2)
    flow.where = "The box is still in bootloader mode (RPI-RP2). You can try again."
    try:
        flow.next(f"Installing SIMINPUT firmware for MobiFlight v{pkg.version}…")
        flow.copy_uf2(pkg.uf2, pkg.uf2_name)
        box = _await_mobiflight(flow)
        flow.where = ""
        _install_community(flow, pkg, community)
        ctx.progress(1.0)
        return box
    except BaseException as e:
        flow.stopped(e)
        raise


def _await_mobiflight(flow: _Flow) -> MobiFlightBox | None:
    """Wait for the box to come up on the new firmware. Returns it, or None
    when its port showed up but is held (MobiFlight Connector got there
    first), which is a success too."""
    flow.next("Waiting for the box to start…")
    flow.where = ("The firmware is copied. If the box doesn't start, unplug it and plug it back "
                  "in; hold BOOTSEL while plugging in to get the RPI-RP2 drive and try again.")
    found: list = []

    def check():
        for port, sn in flow.hw.mobiflight_ports():
            box = flow.hw.probe_mobiflight(port, sn)
            if box.is_siminput or box.status == "busy":
                found.append(box)
                return True
        return False

    flow.wait("The box answering as SIMINPUT rev2", check, MOBIFLIGHT_TIMEOUT,
              "Unplug the box and plug it back in. If it still doesn't show up, hold BOOTSEL "
              "while plugging it in and try again.")
    box = found[0]
    if box.status == "busy":
        flow.ctx.log("  the box is up; MobiFlight Connector already has its port")
        return None
    flow.ctx.log(f"  {box.type}, firmware {box.version}, serial {box.serial}")
    return box


def _install_community(flow: _Flow, pkg: MobiFlightPackage, community: Path | None) -> None:
    if community is None or not pkg.has_community:
        return
    try:
        where = mobiflight.install_community(pkg.zip_bytes, community)
        flow.ctx.log(f"  installed the Connector files in {where}")
    except (OSError, mobiflight.MobiFlightError, zipfile.BadZipFile) as e:
        # The box is done; this is the Connector side, which the user can do by hand.
        flow.ctx.log(f"  couldn't install the Connector files ({e}). Unzip "
                     f"{pkg.source} into {community} yourself.")


def to_siminput(hw: Hardware, stock: FirmwarePackage, config: dict, ctx: Context, *,
                start: str, box: MobiFlightBox | None = None, record_id: str | None = None,
                device=None) -> None:
    """Back to the stock firmware (or a fresh install of it).

    start: "mobiflight" (box on MobiFlight firmware, `box` set), "bootsel"
    (RPI-RP2 drive) or "circuitpy" (CircuitPython 9 already on it, for
    picking up a flow that stopped after that step). With `device`, waits
    for the replug and checks the config over serial.
    """
    flow = _Flow(hw, ctx, steps={"mobiflight": 7, "bootsel": 6, "circuitpy": 3}[start])
    try:
        if start == "mobiflight":
            flow.next("Restarting the box into its bootloader…")
            fresh = hw.probe_mobiflight(box.port, box.usb_serial)
            if fresh.status == "busy":
                raise SwitchError(mobiflight.CONNECTOR_HINT)
            if not fresh.is_siminput:
                raise SwitchError("That board doesn't answer as SIMINPUT rev2, so it was left alone.")
            update_record(record_id, state="restoring")
            _touch(flow, box.port)
            flow.where = "The box may be in bootloader mode (an RPI-RP2 drive). You can try again."
            flow.drive(drives.BOOTSEL)
        elif start == "bootsel":
            if hw.find_drive(drives.BOOTSEL) is None:
                raise SwitchError("No RPI-RP2 drive found. Hold BOOTSEL while plugging in the box.")
            update_record(record_id, state="restoring")
            flow.where = "The box is still in bootloader mode (RPI-RP2). You can try again."

        if start in ("mobiflight", "bootsel"):
            nuke, cp = asset(NUKE_UF2), asset(CIRCUITPYTHON_UF2)
            flow.next("Erasing the box…")
            flow.copy_uf2(nuke, NUKE_UF2)
            flow.where = ("The box is erased and in bootloader mode (RPI-RP2). You can try again; "
                          "your configuration backups are untouched.")
            flow.next(f"Installing CircuitPython {CIRCUITPYTHON_VERSION}…")
            flow.copy_uf2(cp, CIRCUITPYTHON_UF2)
            flow.where = ("CircuitPython is installed (a CIRCUITPY drive). You can try again from "
                          "the Update page; your configuration backups are untouched.")
            flow.next("Waiting for the CIRCUITPY drive…")
            drive = flow.drive(drives.CIRCUITPY, CIRCUITPY_TIMEOUT)
        else:
            drive = hw.find_drive(drives.CIRCUITPY)
            if drive is None:
                raise SwitchError("No CIRCUITPY drive found.")
            update_record(record_id, state="restoring")
            flow.where = "The box is on CircuitPython (a CIRCUITPY drive). You can try again."

        version = circuitpy_version(drive)
        if not version.startswith("9."):
            raise SwitchError(f"The box runs CircuitPython {version or '(unknown)'}; the SIMINPUT "
                              f"firmware needs 9.x. Hold BOOTSEL while plugging it in and install "
                              f"from the RPI-RP2 drive instead.")

        flow.next(f"Copying SIMINPUT firmware v{stock.firmware_version}…")
        _copy_stock(flow, drive, stock, config)
        flow.where = ("The SIMINPUT firmware is installed. Unplug the box and plug it back in "
                      "to start it.")

        if device is not None:
            flow.next("Unplug the box, then plug it back in.")
            ctx.log("  boot.py, which sets up the box as a SIMINPUT controller, only runs at power-up")
            flow.wait("Unplugging the box", lambda: hw.find_drive(drives.CIRCUITPY) is None,
                      REPLUG_TIMEOUT)
            flow.next("Waiting for the box…")
            _check_replugged(flow, device, config)
        update_record(record_id, state="restored")
        ctx.progress(1.0)
    except BaseException as e:
        flow.stopped(e)
        raise


def _touch(flow: _Flow, port: str) -> None:
    """The 1200 baud touch, retried briefly: the Update page's own scan may
    have the port open for a moment."""
    for attempt in range(4):
        try:
            flow.hw.touch_1200(port)
            return
        except mobiflight.MobiFlightError as e:
            if attempt == 3:
                raise SwitchError(str(e)) from e
            flow.ctx.check_cancel()
            time.sleep(0.7)


def _copy_stock(flow: _Flow, drive: Path, stock: FirmwarePackage, config: dict) -> None:
    """lib/ and boot.py first, then config.json, code.py last: code.py is what
    runs, and CircuitPython restarts it after every write. Each file is read
    back and compared."""
    files = upload_order(stock.files)
    cfg = ("config.json", json.dumps(config, indent=2).encode("utf-8"))
    entries = [(f.path, f.data) for f in files if f.path not in ("code.py", "config.json")]
    entries.append(cfg)
    entries += [(f.path, f.data) for f in files if f.path == "code.py"]
    for i, (rel, data) in enumerate(entries):
        flow.ctx.check_cancel()
        flow.ctx.status(f"Copying {rel} ({i + 1} of {len(entries)})…")
        target = drive.joinpath(*PurePosixPath(rel).parts)
        try:
            flow.hw.write_file(target, data)
            if target.read_bytes() != data:
                raise SwitchError(f"{rel} didn't read back the same")
        except OSError as e:
            raise SwitchError(f"Copying {rel} to {drive} failed: {e}") from e
    drives.sync_all()
    flow.ctx.log(f"  {len(entries)} files copied, including config.json")


def _check_replugged(flow: _Flow, device, config: dict) -> None:
    def connect():
        for info in device.discover():
            if info.status == "ok":
                return info
        return None

    info = flow.wait("The box connecting over USB", connect, RECONNECT_TIMEOUT,
                     "Unplug it and plug it back in once more; the Configurator connects on its own.")
    device.connect(info.port)
    got = device.get_config()
    if got.get("device", {}).get("name") != config["device"].get("name") or \
            len(got.get("rules", [])) != len(config.get("rules", [])):
        raise SwitchError("The box is back, but its configuration isn't the one that was restored. "
                          "Open History on the Configure page to load your backup.")
    flow.ctx.log(f"  connected to {info.name} v{info.version}, configuration checked")
