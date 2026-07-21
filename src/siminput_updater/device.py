from __future__ import annotations

import base64
import getpass
import hashlib
import json
import select
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import serial
import serial.tools.list_ports

try:
    import evdev
    import evdev.ecodes as ec
    _HAS_EVDEV = True
except ImportError:
    _HAS_EVDEV = False


class DeviceError(Exception):
    pass


class TimeoutError(DeviceError):
    pass


class ChecksumError(DeviceError):
    pass


@dataclass
class DeviceInfo:
    product: str
    version: str
    name: str
    pid: int
    port: str = ""
    status: str = "ok"
    status_detail: str = ""
    board_map: str = ""


@dataclass
class FullDeviceInfo(DeviceInfo):
    circuitpython: str = ""
    board: str = ""
    nvm_size: int = 0
    pins: dict[str, list[str]] | None = None
    bools: list[str] | None = None
    axes: list[str] | None = None
    rules_count: int = 0


ADAFRUIT_VID = 0x239A
CHUNK_SIZE = 2048


class Device:
    def __init__(self):
        self._port: serial.Serial | None = None
        self._info: DeviceInfo | None = None
        self._streaming = False
        self._stream_thread: threading.Thread | None = None
        self._stream_callback: Callable[[dict], None] | None = None
        self._lock = threading.Lock()
        self._keepalive: Any | None = None
        self._evdev: Any | None = None
        self._btn_map: dict[int, int] = {}
        self._serial_number: str | None = None

    @property
    def connected(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def info(self) -> DeviceInfo | None:
        return self._info

    @staticmethod
    def _ping_port(port: serial.Serial, timeout: float = 2.5) -> dict | None:
        saved_timeout = port.timeout
        port.timeout = timeout
        try:
            port.reset_input_buffer()
            port.write(b'{"cmd":"ping"}\n')
            port.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = port.readline()
                if not line:
                    return None
                try:
                    resp = json.loads(line.decode("utf-8", errors="replace"))
                    if resp.get("ok") and resp.get("product") == "SIMINPUT":
                        return resp
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            return None
        finally:
            port.timeout = saved_timeout

    def ensure_keepalive(self) -> None:
        """Hold an Adafruit evdev input device open to keep xHCI polling active.

        Idempotent — safe to call every scan tick. No-op without evdev.
        """
        if not _HAS_EVDEV:
            return
        if self._keepalive is not None:
            try:
                self._keepalive.fd
                # Keep it unless it demonstrably belongs to a different box
                # than the live connection (uniq is often empty — then keep).
                held = self._keepalive.uniq or ""
                if not self._serial_number or held in ("", self._serial_number):
                    return
                self._keepalive.close()
            except OSError:
                pass
            self._keepalive = None
        # Prefer the evdev node whose USB serial matches the connected box, so
        # the live monitor never streams a different box's inputs.
        fallback = None
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except (PermissionError, OSError):
                continue
            if dev.info.vendor != ADAFRUIT_VID:
                dev.close()
                continue
            if self._serial_number and dev.uniq == self._serial_number:
                if fallback is not None:
                    fallback.close()
                self._keepalive = dev
                return
            if fallback is None:
                fallback = dev
            else:
                dev.close()
        self._keepalive = fallback

    @staticmethod
    def list_ports() -> set[str]:
        """Cheap presence check: Adafruit serial ports currently enumerated.

        Opens nothing — safe to call every second, including while a
        connection is live.
        """
        return {p.device for p in serial.tools.list_ports.comports() if p.vid == ADAFRUIT_VID}

    @staticmethod
    def _probe_port(port_info, skip_ports: set[str] | None) -> DeviceInfo | None:
        """Open one candidate port, ping it, and classify the result."""
        if skip_ports and port_info.device in skip_ports:
            return None

        desc = port_info.description or ""
        name_from_usb = desc.split(" - ")[0].strip() if " - " in desc else "SIMINPUT Device"
        fallback = dict(
            product="SIMINPUT", version="?",
            name=name_from_usb,
            pid=port_info.pid or 0,
            port=port_info.device,
        )

        try:
            port = serial.Serial(port_info.device, timeout=0.5)
        except (PermissionError, serial.SerialException, OSError) as e:
            err = str(e)
            denied = (
                isinstance(e, PermissionError)
                or "Permission denied" in err or "Errno 13" in err
                or "PermissionError" in err or "Access is denied" in err
            )
            if denied:
                if sys.platform == "linux":
                    try:
                        user = getpass.getuser()
                    except Exception:
                        user = "$USER"
                    detail = f"Permission denied — run: sudo usermod -aG dialout {user} (then log out and back in)"
                else:
                    detail = "Access denied — the port may be in use by another program"
                return DeviceInfo(**fallback, status="no_permission", status_detail=detail)
            return DeviceInfo(**fallback, status="open_failed", status_detail=err)

        try:
            resp = Device._ping_port(port, timeout=1.2)
            if resp:
                return DeviceInfo(
                    product=resp["product"],
                    version=resp.get("version", "?"),
                    name=resp.get("name", "Unknown"),
                    pid=resp.get("pid", 0),
                    port=port_info.device,
                    status="ok",
                    board_map=resp.get("board_map", ""),
                )
            return DeviceInfo(
                **fallback, status="no_response",
                status_detail="Device found but did not respond to ping",
            )
        except (serial.SerialException, OSError) as e:
            return DeviceInfo(**fallback, status="error", status_detail=str(e))
        finally:
            port.close()

    @staticmethod
    def discover(skip_ports: set[str] | None = None) -> list[DeviceInfo]:
        """Probe all Adafruit serial ports and return one entry per box.

        CircuitPython exposes two CDC ports per device (console + data); only
        the data port answers the ping. Every candidate is probed, then sibling
        ports are collapsed by USB serial number so the console port of a box
        that answered elsewhere never shows up as a phantom second device.
        """
        candidates = []
        for port_info in serial.tools.list_ports.comports():
            if port_info.vid != ADAFRUIT_VID:
                continue
            desc_lower = (port_info.description or "").lower()
            if "data" in desc_lower or "cdc2" in desc_lower:
                priority = 0
            elif "repl" in desc_lower or "console" in desc_lower or "cdc control" in desc_lower:
                priority = 2
            else:
                priority = 1
            candidates.append((priority, port_info))
        candidates.sort(key=lambda x: x[0])

        results: list[tuple[str | None, DeviceInfo]] = []
        ok_serials: set[str] = set()

        for _, port_info in candidates:
            sn = port_info.serial_number
            if sn and sn in ok_serials:
                continue
            # The port we already hold open is the live connection — register
            # its serial so its sibling console port gets collapsed too.
            if skip_ports and port_info.device in skip_ports:
                if sn:
                    ok_serials.add(sn)
                continue
            dev = Device._probe_port(port_info, skip_ports)
            if dev is None:
                continue
            if dev.status == "ok" and sn:
                ok_serials.add(sn)
            results.append((sn, dev))

        # Drop sibling ports of boxes that answered: a no_response console
        # port sharing a serial with an ok port is the same physical device.
        devices = [
            dev for sn, dev in results
            if dev.status == "ok" or not (sn and sn in ok_serials)
        ]
        # Responding devices first — auto-connect picks the head of this list.
        devices.sort(key=lambda d: 0 if d.status == "ok" else 1)
        return devices

    def connect(self, port_name: str) -> DeviceInfo:
        self.disconnect()
        self._serial_number = next(
            (p.serial_number for p in serial.tools.list_ports.comports()
             if p.device == port_name),
            None,
        )
        try:
            self._port = serial.Serial(port_name, timeout=0.5)
        except (serial.SerialException, OSError) as e:
            raise DeviceError(f"Failed to open {port_name}: {e}")
        # Two attempts: the first ping after enumeration can get lost while
        # CircuitPython is still bringing up the CDC data endpoint.
        resp = self._ping_port(self._port)
        if not resp:
            resp = self._ping_port(self._port)
        if not resp:
            self._port.close()
            self._port = None
            raise TimeoutError("Device did not respond to ping")
        self._port.timeout = 3.0
        info = DeviceInfo(
            product=resp.get("product", ""),
            version=resp.get("version", "?"),
            name=resp.get("name", "Unknown"),
            pid=resp.get("pid", 0),
            board_map=resp.get("board_map", ""),
        )
        self._info = info
        self._info.port = port_name
        self.ensure_keepalive()
        return info

    def disconnect(self):
        self.stop_stream()
        if self._port and self._port.is_open:
            try:
                self._port.close()
            except (serial.SerialException, OSError):
                pass
        self._port = None
        self._info = None
        self._serial_number = None

    def _drain_acks(self):
        """Read and discard complete lines pending from the device (e.g. per-chunk acks)."""
        if not self._port:
            return
        saved = self._port.timeout
        self._port.timeout = 0.005
        try:
            while True:
                line = self._port.readline()
                if not line:
                    break
        finally:
            self._port.timeout = saved

    def _send(self, msg: dict) -> dict:
        if not self._port or not self._port.is_open:
            raise DeviceError("Not connected")
        with self._lock:
            data = json.dumps(msg).encode("utf-8") + b"\n"
            self._port.write(data)
            self._port.flush()
            line = self._port.readline()
            if not line:
                raise TimeoutError("No response from device")
            resp = json.loads(line.decode("utf-8", errors="replace"))
            if not resp.get("ok", False) and "error" in resp:
                raise DeviceError(resp["error"])
            return resp

    def ping(self) -> DeviceInfo:
        resp = self._send({"cmd": "ping"})
        return DeviceInfo(
            product=resp.get("product", ""),
            version=resp.get("version", "?"),
            name=resp.get("name", "Unknown"),
            pid=resp.get("pid", 0),
            board_map=resp.get("board_map", ""),
        )

    def get_info(self) -> FullDeviceInfo:
        resp = self._send({"cmd": "get_info"})
        return FullDeviceInfo(
            product="SIMINPUT",
            version=resp.get("version", "?"),
            name=resp.get("name", "Unknown"),
            pid=resp.get("pid", 0),
            board_map=resp.get("board_map", ""),
            circuitpython=resp.get("circuitpython", "?"),
            board=resp.get("board", "?"),
            nvm_size=resp.get("nvm_size", 0),
            pins=resp.get("pins"),
            bools=resp.get("bools"),
            axes=resp.get("axes"),
            rules_count=resp.get("rules_count", 0),
        )

    def get_config(self) -> dict:
        resp = self._send({"cmd": "get_config"})
        if resp.get("chunked"):
            return json.loads(self._receive_chunks())
        return resp.get("config", {})

    def set_config(self, config: dict) -> None:
        data = json.dumps(config)
        if len(data) < 3072:
            self._send({"cmd": "set_config", "config": config})
        else:
            self._send_chunked("set_config", data.encode("utf-8"))

    def validate_config(self, config: dict) -> None:
        self._send({"cmd": "validate_config", "config": config})

    def get_state(self) -> dict:
        return self._send({"cmd": "get_state"})

    def start_stream(self, callback: Callable[[dict], None], interval_ms: int = 50):
        self.stop_stream()
        self._stream_callback = callback
        self._streaming = True

        self.ensure_keepalive()
        self._evdev = self._keepalive
        if self._evdev:
            caps = self._evdev.capabilities()
            btn_codes = sorted(caps.get(ec.EV_KEY, []))
            self._btn_map = {code: i + 1 for i, code in enumerate(btn_codes)}
            self._stream_thread = threading.Thread(target=self._evdev_reader, daemon=True)
        else:
            if self._port:
                self._port.reset_input_buffer()
            self._send({"cmd": "stream_start", "interval_ms": interval_ms})
            self._stream_thread = threading.Thread(target=self._serial_reader, daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        if not self._streaming:
            return
        self._streaming = False
        # Join the reader first so it can't race _send for the port.
        if self._stream_thread:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        if not self._evdev:
            try:
                self._send({"cmd": "stream_stop"})
                self._drain_acks()  # discard frames queued before the stop took effect
            except (DeviceError, serial.SerialException, OSError, json.JSONDecodeError):
                pass  # port may already be gone (unplugged mid-stream)
        self._evdev = None
        self._stream_callback = None

    def _evdev_reader(self):
        axes = [32767] * 8
        buttons: set[int] = set()
        dev = self._evdev

        try:
            caps = dev.capabilities(absinfo=True)
            for code, absinfo in caps.get(ec.EV_ABS, []):
                if code < 8:
                    axes[code] = absinfo.value
            for code in dev.active_keys():
                btn_num = self._btn_map.get(code)
                if btn_num is not None:
                    buttons.add(btn_num)
        except (OSError, IOError, SystemError):
            pass

        if self._stream_callback:
            self._stream_callback({"a": list(axes), "b": sorted(buttons)})

        while self._streaming and dev:
            try:
                r, _, _ = select.select([dev], [], [], 0.05)
                if not r:
                    continue
                for event in dev.read():
                    if event.type == ec.EV_ABS and event.code < 8:
                        axes[event.code] = event.value
                    elif event.type == ec.EV_KEY:
                        btn_num = self._btn_map.get(event.code)
                        if btn_num is not None:
                            if event.value:
                                buttons.add(btn_num)
                            else:
                                buttons.discard(btn_num)
                    elif event.type == ec.EV_SYN and self._stream_callback:
                        self._stream_callback({"a": list(axes), "b": sorted(buttons)})
            except (OSError, IOError):
                break

    def _serial_reader(self):
        buf = b""
        while self._streaming and self._port and self._port.is_open:
            try:
                n = self._port.in_waiting
                if n:
                    buf += self._port.read(n)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line:
                            continue
                        try:
                            msg = json.loads(line.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            continue  # partial or non-JSON line — skip, keep streaming
                        if "s" in msg and self._stream_callback:
                            self._stream_callback(msg["s"])
                else:
                    time.sleep(0.005)
            except (serial.SerialException, OSError):
                break

    def update_begin(self) -> None:
        self._send({"cmd": "update_begin"})

    def update_commit(self) -> list[str]:
        if not self._port or not self._port.is_open:
            raise DeviceError("Not connected")
        with self._lock:
            data = json.dumps({"cmd": "update_commit"}).encode("utf-8") + b"\n"
            self._port.write(data)
            self._port.flush()
            saved = self._port.timeout
            self._port.timeout = 10.0
            try:
                line = self._port.readline()
            finally:
                self._port.timeout = saved
            if not line:
                raise TimeoutError("No response from device during commit")
            resp = json.loads(line.decode("utf-8", errors="replace"))
            if not resp.get("ok", False) and "error" in resp:
                raise DeviceError(resp["error"])
            return resp.get("committed", [])

    def update_abort(self) -> None:
        try:
            self._send({"cmd": "update_abort"})
        except Exception:
            pass

    def file_write(
        self,
        path: str,
        data: bytes,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        sha = hashlib.sha256(data).hexdigest()
        self._send({"cmd": "file_write", "path": path, "size": len(data), "sha256": sha})

        sent = 0
        seq = 0
        while sent < len(data):
            chunk = data[sent:sent + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            with self._lock:
                self._port.write(json.dumps({"chunk": encoded, "seq": seq}).encode("utf-8") + b"\n")
                self._port.flush()
                self._drain_acks()
            sent += len(chunk)
            seq += 1
            if progress:
                progress(sent, len(data))

        with self._lock:
            self._drain_acks()
            self._port.write(b'{"done":true}\n')
            self._port.flush()
            while True:
                line = self._port.readline()
                if not line:
                    raise TimeoutError("No response after file write")
                resp = json.loads(line.decode("utf-8", errors="replace"))
                if len(resp) > 1 or not resp.get("ok"):
                    break
            if not resp.get("written"):
                raise DeviceError(resp.get("error", "File write failed"))

    def file_read(self, path: str) -> bytes:
        resp = self._send({"cmd": "file_read", "path": path})
        if resp.get("chunked"):
            return self._receive_chunks()
        return base64.b64decode(resp.get("data", ""))

    def reboot(self) -> None:
        self._send({"cmd": "reboot"})
        if self._port:
            self._port.close()
        self._port = None

    def enter_bootloader(self) -> None:
        self._send({"cmd": "bootloader"})
        if self._port:
            self._port.close()
        self._port = None

    def wait_for_reconnect(self, port_name: str, timeout: float = 15.0) -> DeviceInfo:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                return self.connect(port_name)
            except DeviceError:
                continue
        raise TimeoutError(f"Device did not reappear on {port_name} within {timeout}s")

    def _receive_chunks(self) -> bytes:
        parts = []
        while True:
            if not self._port:
                raise DeviceError("Not connected")
            line = self._port.readline()
            if not line:
                raise TimeoutError("Timeout during chunked transfer")
            msg = json.loads(line.decode("utf-8", errors="replace"))
            if msg.get("done"):
                break
            if "chunk" in msg:
                parts.append(base64.b64decode(msg["chunk"]))
        return b"".join(parts)

    def _send_chunked(self, cmd: str, data: bytes):
        self._send({"cmd": cmd, "chunked": True, "size": len(data)})
        sent = 0
        seq = 0
        while sent < len(data):
            chunk = data[sent:sent + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            with self._lock:
                self._port.write(json.dumps({"chunk": encoded, "seq": seq}).encode("utf-8") + b"\n")
                self._port.flush()
                self._drain_acks()
            sent += len(chunk)
            seq += 1
        with self._lock:
            self._drain_acks()
            self._port.write(b'{"done":true}\n')
            self._port.flush()
            while True:
                line = self._port.readline()
                if not line:
                    raise TimeoutError("No response after chunked transfer")
                resp = json.loads(line.decode("utf-8", errors="replace"))
                if len(resp) > 1 or not resp.get("ok"):
                    break
            if not resp.get("ok", False) and "error" in resp:
                raise DeviceError(resp["error"])
