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


class DeviceTimeout(DeviceError):
    pass


# Backward-compatible alias (the old name shadowed the builtin).
TimeoutError = DeviceTimeout


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
    hash_algo: str = ""


ADAFRUIT_VID = 0x239A
CHUNK_SIZE = 2048  # base64(2048)+envelope ≈ 2756 bytes — must stay under the firmware's 4096-byte line limit
RESPONSE_TIMEOUT = 3.0
WRITE_TIMEOUT = 5.0


def _is_ack(resp: dict) -> bool:
    """A per-chunk ack: ok plus at most a seq echo. Anything else is a real response."""
    return resp.get("ok") is True and not (set(resp.keys()) - {"ok", "seq"})


class Device:
    def __init__(self):
        self._port: serial.Serial | None = None
        self._info: DeviceInfo | None = None
        self._streaming = False
        self._stream_thread: threading.Thread | None = None
        self._stream_callback: Callable[[dict], None] | None = None
        self._stream_end_callback: Callable[[], None] | None = None
        self._lock = threading.Lock()          # serial message traffic
        self._conn_lock = threading.RLock()    # connect/disconnect lifecycle
        self._keepalive_lock = threading.Lock()
        self._keepalive: Any | None = None
        self._evdev: Any | None = None
        self._btn_map: dict[int, int] = {}
        self._serial_number: str | None = None
        self._rxbuf = b""

    @property
    def connected(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def info(self) -> DeviceInfo | None:
        return self._info

    # ------------------------------------------------------------- transport

    def _flush_input(self):
        self._rxbuf = b""
        if self._port:
            try:
                self._port.reset_input_buffer()
            except (serial.SerialException, OSError):
                pass

    def _read_line(self, timeout: float) -> bytes | None:
        """Read one complete newline-terminated line, buffering partials.

        Unlike pyserial's readline (whose timeout is per byte, so a slow
        device can hand back a truncated line), a partial line stays in the
        buffer until its newline arrives or the deadline passes.
        """
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self._rxbuf:
                line, self._rxbuf = self._rxbuf.split(b"\n", 1)
                return line
            port = self._port
            if port is None or not port.is_open:
                return None
            try:
                n = port.in_waiting
                if n:
                    self._rxbuf += port.read(n)
                    continue
            except (serial.SerialException, OSError):
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def _read_response(self, timeout: float = RESPONSE_TIMEOUT, skip_acks: bool = False) -> dict:
        """Read the next real response, discarding noise.

        Stray stream frames, garbage lines, and (optionally) bare chunk acks
        are skipped: only a line carrying an explicit "ok" key counts as a
        response. Previously any line was accepted, so a leftover stream frame
        could impersonate a get_config reply and present an empty config.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            line = self._read_line(remaining)
            if line is None:
                break
            if not line.strip():
                continue
            try:
                resp = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(resp, dict) or "ok" not in resp:
                continue  # stream frame or garbage — not a command response
            if skip_acks and _is_ack(resp):
                continue
            if not resp.get("ok", False) and "error" in resp:
                raise DeviceError(resp["error"])
            return resp
        # Drop whatever arrives late so it can't be taken for the next
        # command's response (responses carry no request identity).
        self._flush_input()
        raise DeviceTimeout("No response from device")

    def _write_line(self, data: bytes):
        self._port.write(data)
        self._port.flush()

    def _send(self, msg: dict, timeout: float = RESPONSE_TIMEOUT, skip_acks: bool = True) -> dict:
        """Send one command and read its response.

        skip_acks defaults to True: a stale bare {"ok": true} left over from a
        chunk transfer or a stream_stop must not be mistaken for the next
        command's response. Only stream_start/stream_stop legitimately answer
        with a bare ok — those two callers pass skip_acks=False.
        """
        if not self._port or not self._port.is_open:
            raise DeviceError("Not connected")
        with self._lock:
            self._write_line(json.dumps(msg).encode("utf-8") + b"\n")
            return self._read_response(timeout, skip_acks=skip_acks)

    def _check_acks(self):
        """Consume pending complete ack lines without blocking.

        A device-reported transfer error surfaces immediately as DeviceError
        instead of being silently discarded while the host keeps sending
        chunks at a device that already aborted.
        """
        port = self._port
        if port is None:
            return
        try:
            n = port.in_waiting
            if n:
                self._rxbuf += port.read(n)
        except (serial.SerialException, OSError):
            return
        while b"\n" in self._rxbuf:
            line, self._rxbuf = self._rxbuf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                resp = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(resp, dict) or "ok" not in resp:
                continue  # stream frame
            if not resp.get("ok", False) and "error" in resp:
                raise DeviceError(resp["error"])

    # ------------------------------------------------------------- discovery

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
        with self._keepalive_lock:
            self._ensure_keepalive_locked()

    def _ensure_keepalive_locked(self) -> None:
        if self._keepalive is not None:
            alive = self._keepalive.fd >= 0
            if alive and self._evdev is None:
                # Cheap liveness probe: a vanished device raises OSError.
                # (Skipped while the monitor reader owns the fd — read_one
                # would steal its events.)
                try:
                    self._keepalive.read_one()
                except OSError:
                    alive = False
                except Exception:
                    pass
            if alive:
                # Keep it unless it demonstrably belongs to a different box
                # than the live connection (uniq is often empty — then keep).
                held = self._keepalive.uniq or ""
                if not self._serial_number or held in ("", self._serial_number):
                    return
            try:
                self._keepalive.close()
            except OSError:
                pass
            self._keepalive = None
        # Prefer the evdev node whose USB serial matches the connected box, so
        # the live monitor never streams a different box's inputs.
        fallback = None
        try:
            paths = evdev.list_devices()
        except OSError:
            paths = []
        for path in paths:
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

    # ------------------------------------------------------------ connection

    def connect(self, port_name: str) -> DeviceInfo:
        with self._conn_lock:
            self.disconnect()
            self._serial_number = next(
                (p.serial_number for p in serial.tools.list_ports.comports()
                 if p.device == port_name),
                None,
            )
            try:
                self._port = serial.Serial(port_name, timeout=0.5, write_timeout=WRITE_TIMEOUT)
            except (serial.SerialException, OSError) as e:
                raise DeviceError(f"Failed to open {port_name}: {e}")
            self._rxbuf = b""
            # Two attempts: the first ping after enumeration can get lost while
            # CircuitPython is still bringing up the CDC data endpoint.
            try:
                resp = self._ping_port(self._port)
                if not resp:
                    resp = self._ping_port(self._port)
            except (serial.SerialException, OSError) as e:
                self._close_port()
                raise DeviceError(f"Ping failed on {port_name}: {e}")
            if not resp:
                self._close_port()
                raise DeviceTimeout("Device did not respond to ping")
            self._port.timeout = RESPONSE_TIMEOUT
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

    def _close_port(self):
        if self._port and self._port.is_open:
            try:
                self._port.close()
            except (serial.SerialException, OSError):
                pass
        self._port = None

    def disconnect(self):
        with self._conn_lock:
            self.stop_stream()
            self._close_port()
            self._info = None
            self._serial_number = None
            self._rxbuf = b""

    def handle_reboot_disconnect(self):
        """The device told us it is rebooting (e.g. after set_config): drop the
        connection quietly, without a stream_stop round trip."""
        with self._conn_lock:
            self._streaming = False
            self._close_port()

    # -------------------------------------------------------------- commands

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
            hash_algo=resp.get("hash", ""),
        )

    def get_config(self) -> dict:
        resp = self._send({"cmd": "get_config"})
        if resp.get("chunked"):
            with self._lock:
                return json.loads(self._receive_chunks())
        return resp.get("config", {})

    def set_config(self, config: dict) -> dict:
        """Returns the device's response; a "rebooting" key means the device
        is restarting to apply the config and the connection is about to drop."""
        data = json.dumps(config)
        if len(data) < 3072:
            return self._send({"cmd": "set_config", "config": config})
        return self._send_chunked("set_config", data.encode("utf-8"))

    def validate_config(self, config: dict) -> None:
        data = json.dumps(config)
        if len(data) < 3072:
            self._send({"cmd": "validate_config", "config": config})
        else:
            self._send_chunked("validate_config", data.encode("utf-8"))

    def get_state(self) -> dict:
        return self._send({"cmd": "get_state"})

    # ------------------------------------------------------------- streaming

    def start_stream(
        self,
        callback: Callable[[dict], None],
        interval_ms: int = 50,
        on_end: Callable[[], None] | None = None,
    ):
        """Start the live monitor. `on_end` fires if the reader dies
        unexpectedly (device vanished), so the UI can show an error and reset
        instead of freezing on stale data forever."""
        self.stop_stream()
        self._stream_callback = callback
        self._stream_end_callback = on_end

        self.ensure_keepalive()
        self._evdev = self._keepalive
        if self._evdev:
            caps = self._evdev.capabilities()
            btn_codes = sorted(caps.get(ec.EV_KEY, []))
            # Bit i of the HID report is B<i>; the first usage (index 0) is B0,
            # which the firmware never drives. Numbering codes by index keeps
            # the monitor consistent with config B-names and the serial stream.
            self._btn_map = {code: i for i, code in enumerate(btn_codes)}
            self._streaming = True
            self._stream_thread = threading.Thread(target=self._evdev_reader, daemon=True)
        else:
            self._flush_input()
            self._send({"cmd": "stream_start", "interval_ms": interval_ms}, skip_acks=False)
            self._streaming = True  # only after the device acknowledged
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
            # Short bounded round trip: this runs on the UI thread during page
            # switches, so waiting a full response timeout would freeze the UI.
            try:
                self._send({"cmd": "stream_stop"}, timeout=0.5, skip_acks=False)
            except (DeviceError, serial.SerialException, OSError, json.JSONDecodeError):
                pass  # port may already be gone (unplugged mid-stream)
            self._flush_input()  # discard frames queued before the stop took effect
        self._evdev = None
        self._stream_callback = None
        self._stream_end_callback = None

    def _reader_ended(self):
        """Called from reader threads on exit. If streaming was still on, the
        reader died unexpectedly — reset state and tell the UI."""
        if not self._streaming:
            return
        self._streaming = False
        cb = self._stream_end_callback
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _evdev_reader(self):
        axes = [32767] * 8
        buttons: set[int] = set()
        dev = self._evdev

        try:
            try:
                caps = dev.capabilities(absinfo=True)
                for code, absinfo in caps.get(ec.EV_ABS, []):
                    if code < 8:
                        axes[code] = absinfo.value
                for code in dev.active_keys():
                    btn_num = self._btn_map.get(code)
                    if btn_num:  # 0 is B0 — unused by the firmware
                        buttons.add(btn_num)
            except (OSError, IOError, SystemError, ValueError):
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
                            if btn_num:
                                if event.value:
                                    buttons.add(btn_num)
                                else:
                                    buttons.discard(btn_num)
                        elif event.type == ec.EV_SYN and self._stream_callback:
                            self._stream_callback({"a": list(axes), "b": sorted(buttons)})
                except (OSError, IOError, ValueError):
                    # ValueError: a closed fd handed to select. Either way the
                    # node is gone — report it rather than dying silently.
                    break
        finally:
            self._reader_ended()

    def _serial_reader(self):
        buf = b""
        try:
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
        finally:
            self._reader_ended()

    # --------------------------------------------------------------- updates

    def update_begin(self) -> None:
        self._send({"cmd": "update_begin"})

    def update_commit(self) -> list[str]:
        if not self._port or not self._port.is_open:
            raise DeviceError("Not connected")
        with self._lock:
            self._write_line(json.dumps({"cmd": "update_commit"}).encode("utf-8") + b"\n")
            resp = self._read_response(timeout=10.0, skip_acks=True)
            return resp.get("committed", [])

    def update_abort(self) -> bool:
        """Best effort. Returns False when the abort could not be confirmed,
        so callers stop claiming the device is unchanged."""
        try:
            self._send({"cmd": "update_abort"})
            return True
        except Exception:
            return False

    def file_write(
        self,
        path: str,
        data: bytes,
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        # Both digests: the firmware verifies with the strongest algorithm its
        # CircuitPython build provides (9.x on RP2040 has sha1 only).
        header = {
            "cmd": "file_write", "path": path, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
        }
        self._send(header)

        sent = 0
        seq = 0
        while sent < len(data):
            if should_cancel and should_cancel():
                raise DeviceError("Cancelled during file transfer")
            chunk = data[sent:sent + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            with self._lock:
                self._write_line(json.dumps({"chunk": encoded, "seq": seq}).encode("utf-8") + b"\n")
                self._check_acks()  # raises on a device-reported transfer error
            sent += len(chunk)
            seq += 1
            if progress:
                progress(sent, len(data))

        with self._lock:
            self._check_acks()
            self._write_line(b'{"done":true}\n')
            resp = self._read_response(timeout=10.0, skip_acks=True)
            if not resp.get("written"):
                raise DeviceError(resp.get("error", "File write failed"))

    def file_read(self, path: str) -> bytes:
        resp = self._send({"cmd": "file_read", "path": path})
        if resp.get("chunked"):
            with self._lock:
                return self._receive_chunks()
        return base64.b64decode(resp.get("data", ""))

    def reboot(self, hard: bool = False) -> None:
        """Reboot the device. No reply is treated as success — the device may
        reset before its ack reaches us — and the port always ends closed.

        hard=True requests a full chip reset (re-runs boot.py, so USB identity
        changes and a freshly flashed boot.py take effect); older firmware
        ignores the flag and soft-reloads.
        """
        try:
            msg: dict = {"cmd": "reboot"}
            if hard:
                msg["hard"] = True
            self._send(msg)
        except (DeviceTimeout, serial.SerialException, OSError):
            pass
        finally:
            with self._conn_lock:
                self._streaming = False
                self._close_port()

    def enter_bootloader(self) -> None:
        try:
            self._send({"cmd": "bootloader"})
        except (DeviceTimeout, serial.SerialException, OSError):
            pass
        finally:
            with self._conn_lock:
                self._close_port()

    def wait_for_reconnect(self, port_name: str, timeout: float = 15.0) -> DeviceInfo:
        """Reconnect after a reboot. Retries the old port name and, if the
        device re-enumerated under a different name (hard reset), falls back
        to matching its USB serial number."""
        target_serial = self._serial_number
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                return self.connect(port_name)
            except (DeviceError, OSError):
                pass
            if target_serial:
                try:
                    other = next(
                        (p.device for p in serial.tools.list_ports.comports()
                         if p.serial_number == target_serial and p.device != port_name),
                        None,
                    )
                except Exception:
                    other = None
                if other:
                    try:
                        return self.connect(other)
                    except (DeviceError, OSError):
                        pass
        raise DeviceTimeout(f"Device did not reappear on {port_name} within {timeout}s")

    # ------------------------------------------------------ chunked transfer

    def _receive_chunks(self) -> bytes:
        """Receive a chunked download. Caller holds self._lock for the whole
        exchange so no other command can interleave."""
        parts = []
        expected_seq = 0
        while True:
            if not self._port:
                raise DeviceError("Not connected")
            line = self._read_line(RESPONSE_TIMEOUT)
            if line is None:
                raise DeviceTimeout("Timeout during chunked transfer")
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("done"):
                break
            if "chunk" in msg:
                seq = msg.get("seq")
                if seq is not None:
                    if seq != expected_seq:
                        raise DeviceError(f"Chunk out of order: expected {expected_seq}, got {seq}")
                    expected_seq += 1
                parts.append(base64.b64decode(msg["chunk"]))
        return b"".join(parts)

    def _send_chunked(self, cmd: str, data: bytes) -> dict:
        self._send({"cmd": cmd, "chunked": True, "size": len(data)})
        sent = 0
        seq = 0
        while sent < len(data):
            chunk = data[sent:sent + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            with self._lock:
                self._write_line(json.dumps({"chunk": encoded, "seq": seq}).encode("utf-8") + b"\n")
                self._check_acks()
            sent += len(chunk)
            seq += 1
        with self._lock:
            self._check_acks()
            self._write_line(b'{"done":true}\n')
            return self._read_response(timeout=10.0, skip_acks=True)
