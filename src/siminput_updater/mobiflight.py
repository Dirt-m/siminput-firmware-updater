"""Talking to a box on SIMINPUT firmware for MobiFlight.

The box enumerates with the arduino-pico default USB ID `2E8A:000A`, which a
plain Raspberry Pi Pico running MobiFlight shares. It is only treated as a
SIMINPUT box when it answers MobiFlight's CmdMessenger GetInfo (`9;`) with
type "SIMINPUT rev2"; anything else is never sent anything but that query.
"""

from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import serial
import serial.tools.list_ports

PICO_VID = 0x2E8A
ARDUINO_PICO_PID = 0x000A
SIMINPUT_TYPE = "SIMINPUT rev2"
CMD_GET_INFO, CMD_INFO = 9, 10
BAUD = 115200
PROBE_TIMEOUT = 2.5   # MobiFlight Connector itself waits 1.25 s after opening
CONNECTOR_HINT = "Close MobiFlight Connector: while it runs it holds the box's port."


@dataclass
class MobiFlightBox:
    port: str
    usb_serial: str = ""
    status: str = "ok"          # ok / busy / no_response / error
    detail: str = ""
    type: str = ""
    name: str = ""
    serial: str = ""            # MobiFlight's own serial (GetInfo)
    version: str = ""

    @property
    def is_siminput(self) -> bool:
        return self.status == "ok" and self.type == SIMINPUT_TYPE

    @property
    def ids(self) -> list[str]:
        """Everything that identifies this box across firmware switches."""
        return [i for i in (self.usb_serial, self.serial) if i]


class MobiFlightError(Exception):
    pass


def parse_message(raw: str) -> tuple[int, list[str]] | None:
    """One CmdMessenger command (without its ';'): fields split by ',', '/'
    escapes the next character."""
    fields, cur, esc = [], "", False
    for c in raw.strip("\r\n"):
        if esc:
            cur += c
            esc = False
        elif c == "/":
            esc = True
        elif c == ",":
            fields.append(cur)
            cur = ""
        else:
            cur += c
    fields.append(cur)
    try:
        return int(fields[0]), fields[1:]
    except ValueError:
        return None


def split_messages(buf: bytes) -> tuple[list[str], bytes]:
    """Complete commands in `buf` and the unfinished rest."""
    out, start, i, esc = [], 0, 0, False
    while i < len(buf):
        c = buf[i:i + 1]
        if esc:
            esc = False
        elif c == b"/":
            esc = True
        elif c == b";":
            out.append(buf[start:i].decode(errors="replace"))
            start = i + 1
        i += 1
    return out, buf[start:]


def _busy(e: Exception) -> bool:
    text = str(e)
    return (isinstance(e, PermissionError) or "Access is denied" in text
            or "Permission denied" in text or "busy" in text.lower()
            or "exclusively lock" in text or "Errno 16" in text)


def _open(port: str, baud: int) -> serial.Serial:
    kwargs = {"timeout": 0.05, "write_timeout": 1.0}
    if os.name == "posix":
        # Don't share the port with a program that already has it open.
        kwargs["exclusive"] = True
    return serial.Serial(port, baud, **kwargs)


def probe(port: str, usb_serial: str = "", timeout: float = PROBE_TIMEOUT) -> MobiFlightBox:
    box = MobiFlightBox(port=port, usb_serial=usb_serial)
    try:
        s = _open(port, BAUD)
    except (serial.SerialException, OSError) as e:
        box.status = "busy" if _busy(e) else "error"
        box.detail = CONNECTOR_HINT if box.status == "busy" else str(e)
        return box
    try:
        s.dtr = True
        time.sleep(0.15)
        s.reset_input_buffer()
        buf = b""
        end = time.monotonic() + timeout
        next_send = 0.0
        while time.monotonic() < end:
            if time.monotonic() >= next_send:
                s.write(b"%d;" % CMD_GET_INFO)
                next_send = time.monotonic() + 0.8
            buf += s.read(256)
            msgs, buf = split_messages(buf)
            for raw in msgs:
                msg = parse_message(raw)
                if msg and msg[0] == CMD_INFO:
                    f = (msg[1] + [""] * 5)[:5]
                    box.type, box.name, box.serial, box.version = f[0], f[1], f[2], f[3]
                    return box
        box.status = "no_response"
        box.detail = "The board did not answer MobiFlight's GetInfo"
        return box
    except (serial.SerialException, OSError) as e:
        box.status = "error"
        box.detail = str(e)
        return box
    finally:
        try:
            s.close()
        except Exception:
            pass


def ports() -> list[tuple[str, str]]:
    """(port, usb serial) of every board with the arduino-pico USB ID."""
    return [(p.device, p.serial_number or "") for p in serial.tools.list_ports.comports()
            if p.vid == PICO_VID and p.pid == ARDUINO_PICO_PID]


def find_boxes() -> list[MobiFlightBox]:
    return [probe(port, sn) for port, sn in ports()]


def touch_1200(port: str) -> None:
    """Reboot an arduino-pico board into its bootloader: open the port at
    1200 baud, drop DTR, close."""
    try:
        s = serial.Serial(port, 1200)
    except (serial.SerialException, OSError) as e:
        if _busy(e):
            raise MobiFlightError(CONNECTOR_HINT) from e
        raise MobiFlightError(f"Couldn't open {port}: {e}") from e
    try:
        s.dtr = False
        time.sleep(0.1)
    finally:
        try:
            s.close()
        except Exception:
            pass


# ------------------------------------------------------- Connector package

def community_dir() -> Path | None:
    """MobiFlight Connector's Community folder, when the Connector is
    installed (Windows only; the Connector has no other platforms)."""
    if not sys.platform.startswith("win"):
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    connector = Path(base) / "MobiFlight" / "MobiFlight Connector"
    return connector / "Community" if connector.is_dir() else None


def install_community(zip_bytes: bytes, target: Path) -> Path:
    """Unpack the release zip's SIMINPUT/ folder into `target` (the
    Community folder). Only paths under SIMINPUT/ are written."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for name in names:
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or p.parts[0] != "SIMINPUT":
                raise MobiFlightError(f"Unexpected file in the package: {name}")
        for name in names:
            dest = target.joinpath(*PurePosixPath(name).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
    return target / "SIMINPUT"
