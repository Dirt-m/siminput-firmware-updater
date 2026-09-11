from __future__ import annotations

import copy
import random
import re
import threading
import time
from typing import Callable

from .config_model import ANALOG_MAX, Config, analog_pins_for_board, pins_for_board, validate
from .device import DeviceError, DeviceInfo, FullDeviceInfo

MOCK_BOARD_MAP = "rev1"
MOCK_VERSION = "2.7.0-mock"
ANALOG_STREAM_DELTA = 64   # serial_handler._ANALOG_STREAM_DELTA
ANALOG_DRIFT = 500         # max raw counts a fake sensor moves per tick

MOCK_CONFIG = {
    "device": {"name": "Mock SimInput Box", "pid": 61440, "debounce_ms": 10},
    "bools": [
        {"id": "TOGGLE1", "default": False, "store": True},
    ],
    "axes": [
        {"id": "AX1", "output": 1, "default": 32767, "store": True, "backlight": True},
    ],
    "rules": [
        {"type": "MAP", "input": "D1", "output": "B1"},
        {"type": "MAP", "input": "D2", "output": "B2", "invert": True},
        {"type": "TOGGLE", "input": "D5", "output": "TOGGLE1"},
        {"type": "MAP", "input": "TOGGLE1", "output": "B100"},
        {"type": "NOR", "inputs": ["D17", "D18"], "output": "B30"},
        {"type": "ENCODER", "inputs": ["D19", "D20"], "cw": "B19", "ccw": "B20"},
        {"type": "AXIS_INC", "input": "B19", "axis": "AX1", "step": 2048},
        {"type": "AXIS_DEC", "input": "B20", "axis": "AX1", "step": 2048},
    ],
}


class MockDevice:
    def __init__(self, config: dict | None = None):
        self._config = config or copy.deepcopy(MOCK_CONFIG)
        self._version = MOCK_VERSION
        self._streaming = False
        self._stream_thread: threading.Thread | None = None
        self._stream_callback: Callable[[dict], None] | None = None
        self._connected = False
        self._info: DeviceInfo | None = None
        self._buttons: set[int] = set()
        self._axes: list[int] = [32767] * 8
        self._bools: dict[str, bool] = {}
        self._pins: dict[str, bool] = {}
        # Raw 16-bit samples of the pins the config claims as analog, like
        # the firmware's box.analog_raw; the fake sensors drift each tick.
        self._analog: dict[str, int] = {}
        self._analog_rules: list = []
        self._threshold_rules: list = []
        self._threshold_state: dict[int, bool] = {}
        self._axis_slots: dict[str, int] = {}
        self._snapshot_pending = False
        self._stream_prev_btns: list[int] | None = None
        self._stream_prev_axes: list[int] | None = None
        self._stream_prev_pins: dict[str, bool] = {}
        self._stream_prev_analog: dict[str, int] | None = None
        self._update_staging: dict[str, bytes] | None = None
        self._init_state()

    def _init_state(self):
        self._pins = {p: False for p in pins_for_board(MOCK_BOARD_MAP)}
        cfg = Config.from_dict(self._config)
        self._bools = {b.id: b.default for b in cfg.bools}
        self._axis_slots = {}
        for a in cfg.axes:
            slot = a.output
            if isinstance(slot, int) and 1 <= slot <= 8:
                self._axes[slot - 1] = a.default
                self._axis_slots[a.id] = slot
        adc = set(analog_pins_for_board(MOCK_BOARD_MAP))
        self._analog_rules = [r for r in cfg.rules if not r.comment and r.type == "ANALOG"]
        self._threshold_rules = [r for r in cfg.rules if not r.comment and r.type == "THRESHOLD"]
        self._threshold_state = {}
        claimed = {r.input for r in self._analog_rules + self._threshold_rules if r.input in adc}
        # Keep a sensor's current reading across a config save; new pins
        # start mid-scale like a pot at rest.
        self._analog = {p: self._analog.get(p, ANALOG_MAX // 2) for p in sorted(claimed)}
        self._apply_analog_rules()

    # ---------------------------------------------------------- analog sim

    def _analog_axes(self) -> set[int]:
        return {self._axis_slots[r.axis] - 1 for r in self._analog_rules if r.axis in self._axis_slots}

    def _apply_analog_rules(self):
        """A simplified copy of the firmware pipeline (range, center and
        deadzone, invert; no filter or curve): enough for the configurator
        to show an axis following its sensor and a THRESHOLD lighting a
        button."""
        for r in self._analog_rules:
            slot = self._axis_slots.get(r.axis)
            if slot is None or r.input not in self._analog:
                continue
            raw = self._analog[r.input]
            lo = 0 if r.min is None else r.min
            hi = ANALOG_MAX if r.max is None else r.max
            if hi <= lo:
                continue
            if r.center is None:
                val = (max(lo, min(hi, raw)) - lo) * ANALOG_MAX // (hi - lo)
            else:
                dz = r.deadzone or 0
                c_lo, c_hi = r.center - dz, r.center + dz
                mid = ANALOG_MAX // 2
                if raw <= c_lo:
                    span = max(1, c_lo - lo)
                    val = (max(lo, raw) - lo) * mid // span
                elif raw >= c_hi:
                    span = max(1, hi - c_hi)
                    val = mid + (min(hi, raw) - c_hi) * (ANALOG_MAX - mid) // span
                else:
                    val = mid
            if r.invert:
                val = ANALOG_MAX - val
            self._axes[slot - 1] = max(0, min(ANALOG_MAX, val))

        for i, r in enumerate(self._threshold_rules):
            if r.input in self._analog:
                value = self._analog[r.input]
            elif r.input in self._axis_slots:
                value = self._axes[self._axis_slots[r.input] - 1]
            else:
                continue
            hyst = r.hysteresis or 0
            prev = self._threshold_state.get(i, False)
            if r.above is not None:
                on = value > r.above - hyst if prev else value >= r.above
            elif r.below is not None:
                on = value < r.below + hyst if prev else value <= r.below
            else:
                continue
            self._threshold_state[i] = on
            if r.invert:
                on = not on
            out = r.output
            if out.startswith("B") and out[1:].isdigit():
                if on:
                    self._buttons.add(int(out[1:]))
                else:
                    self._buttons.discard(int(out[1:]))
            elif out in self._bools:
                self._bools[out] = on

    def set_analog(self, pin: str, raw: int) -> None:
        """Test hook: drive a fake sensor to an exact reading."""
        if pin in self._analog:
            self._analog[pin] = max(0, min(ANALOG_MAX, int(raw)))
            self._apply_analog_rules()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def info(self) -> DeviceInfo | None:
        return self._info

    def ensure_keepalive(self) -> None:
        pass

    @staticmethod
    def list_ports() -> set[str]:
        return {"MOCK"}

    @staticmethod
    def discover(skip_ports: set[str] | None = None) -> list[DeviceInfo]:
        if skip_ports and "MOCK" in skip_ports:
            return []
        return [DeviceInfo(
            product="SIMINPUT",
            version=MOCK_VERSION,
            name="Mock SimInput Box",
            pid=0xF000,
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
        )]

    def connect(self, port_name: str) -> DeviceInfo:
        self.disconnect()  # like the real Device: connect always starts clean
        self._connected = True
        self._info = DeviceInfo(
            product="SIMINPUT",
            version=self._version,
            name=self._config.get("device", {}).get("name", "Mock SimInput Box"),
            pid=self._config.get("device", {}).get("pid", 0xF000),
            port=port_name,
            board_map=MOCK_BOARD_MAP,
        )
        return self._info

    def disconnect(self):
        self.stop_stream()
        self._connected = False
        self._info = None

    def ping(self) -> DeviceInfo:
        return DeviceInfo(
            product="SIMINPUT",
            version=self._version,
            name=self._config.get("device", {}).get("name", "Mock SimInput Box"),
            pid=self._config.get("device", {}).get("pid", 0xF000),
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
        )

    def get_info(self) -> FullDeviceInfo:
        return FullDeviceInfo(
            product="SIMINPUT",
            version=self._version,
            name=self._config.get("device", {}).get("name", "Mock SimInput Box"),
            pid=self._config.get("device", {}).get("pid", 0xF000),
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
            circuitpython="9.2.0",
            board="raspberry_pi_pico",
            nvm_size=4096,
            pins=sorted(pins_for_board(MOCK_BOARD_MAP)),
            bools=[b["id"] for b in self._config.get("bools", [])],
            axes=[a["id"] for a in self._config.get("axes", [])],
            rules_count=len(self._config.get("rules", [])),
            hash_algo="sha256",
            protocol=2,
            caps=("staged_update", "hard_reboot", "stream", "chunked_config", "request_id", "analog"),
            limits={"max_line": 4096, "chunk": 2048, "max_config": 32768},
            analog_pins=sorted(analog_pins_for_board(MOCK_BOARD_MAP)),
            analog_active=sorted(self._analog),
        )

    def get_config(self) -> dict:
        return copy.deepcopy(self._config)

    def set_config(self, config: dict) -> dict:
        cfg = Config.from_dict(config)
        errs = validate(cfg, board_map=MOCK_BOARD_MAP)
        if errs:
            raise DeviceError(str(errs[0]))
        self._config = copy.deepcopy(config)
        self._init_state()
        return {"ok": True}  # the mock applies instantly, no reboot

    def validate_config(self, config: dict) -> None:
        cfg = Config.from_dict(config)
        errs = validate(cfg, board_map=MOCK_BOARD_MAP)
        if errs:
            raise DeviceError(str(errs[0]))

    def get_state(self) -> dict:
        return {
            "buttons": sorted(self._buttons),
            "axes": list(self._axes),
            "bools": dict(self._bools),
            "pins": dict(self._pins),
            "analog": dict(self._analog),
        }

    @property
    def stream_uses_evdev(self) -> bool:
        return False

    def start_stream(
        self,
        callback: Callable[[dict], None],
        interval_ms: int = 50,
        on_end: Callable[[], None] | None = None,
    ):
        self.stop_stream()
        self._stream_callback = callback
        self._reset_stream_baselines()
        self._streaming = True
        self._stream_thread = threading.Thread(target=self._stream_loop, args=(interval_ms,), daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        if not self._streaming:
            return
        self._streaming = False
        if self._stream_thread:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        self._stream_callback = None

    def rearm_stream(self) -> None:
        """Like the firmware's stream_start on an already-running stream: the
        change baselines are dropped, so the next frame is a full snapshot."""
        if self._streaming:
            self._reset_stream_baselines()

    def _reset_stream_baselines(self):
        self._snapshot_pending = True
        self._stream_prev_btns = None
        self._stream_prev_axes = None
        self._stream_prev_pins = {}
        self._stream_prev_analog = None

    def _stream_loop(self, interval_ms: int):
        while self._streaming:
            self._simulate_tick()
            frame = self._build_frame()
            if frame is not None and self._stream_callback:
                self._stream_callback(frame)
            time.sleep(interval_ms / 1000.0)

    def _build_frame(self) -> dict | None:
        """Mirror serial_handler.maybe_send_stream: a frame goes out only when
        something changed, "p" carries only the pins that changed, and the
        first frame after every stream_start is a full pin snapshot."""
        btns = sorted(self._buttons)
        axes = list(self._axes)
        snapshot = self._snapshot_pending
        if snapshot:
            pins_changed = dict(self._pins)
        else:
            pins_changed = {k: v for k, v in self._pins.items()
                            if self._stream_prev_pins.get(k) != v}
        # "an" is re-sent as a whole once any sensor drifts past the noise
        # floor since the last frame that carried it (or in the first frame).
        analog = dict(self._analog)
        prev_an = self._stream_prev_analog
        analog_changed = bool(analog) and (
            prev_an is None or any(abs(v - prev_an.get(k, -10 ** 6)) >= ANALOG_STREAM_DELTA
                                   for k, v in analog.items()))
        if not snapshot and not pins_changed and not analog_changed \
                and btns == self._stream_prev_btns and axes == self._stream_prev_axes:
            return None

        self._snapshot_pending = False
        self._stream_prev_btns = btns
        self._stream_prev_axes = axes
        self._stream_prev_pins.update(pins_changed)
        if analog_changed:
            self._stream_prev_analog = analog

        frame = {"src": "serial", "b": btns, "a": axes}
        if pins_changed:
            frame["p"] = pins_changed
        if analog_changed:
            frame["an"] = analog
        if snapshot:
            frame["snapshot"] = True
        return frame

    def _simulate_tick(self):
        if random.random() < 0.1:
            pin = random.choice(list(self._pins.keys()))
            self._pins[pin] = not self._pins[pin]
        if random.random() < 0.05:
            btn = random.randint(1, 24)
            if btn in self._buttons:
                self._buttons.discard(btn)
            else:
                self._buttons.add(btn)
        if random.random() < 0.08:
            idx = random.randint(0, 7)
            if idx not in self._analog_axes():
                delta = random.randint(-2048, 2048)
                self._axes[idx] = max(0, min(65535, self._axes[idx] + delta))
        if self._analog:
            for pin in self._analog:
                # A slow wander plus ADC-like jitter, so the calibration view
                # has something to show even when nobody touches the box.
                delta = random.randint(-ANALOG_DRIFT, ANALOG_DRIFT) + random.randint(-40, 40)
                self._analog[pin] = max(0, min(ANALOG_MAX, self._analog[pin] + delta))
            self._apply_analog_rules()

    def update_begin(self) -> None:
        # Matches firmware ≥2.5.0: stale staging from a dead session is
        # discarded and the update starts clean.
        self._update_staging = {}

    def update_commit(self) -> list[str]:
        if self._update_staging is None:
            raise DeviceError("no update in progress")
        committed = list(self._update_staging.keys())
        # Adopt the version of the flashed firmware so the post-update version
        # check exercises the same path as real hardware.
        handler = self._update_staging.get("lib/serial_handler.py")
        if handler:
            m = re.search(rb'FW_VERSION = "([^"]+)"', handler)
            if m:
                self._version = m.group(1).decode()
        self._update_staging = None
        return committed

    def update_abort(self) -> bool:
        self._update_staging = None
        return True

    def file_write(
        self,
        path: str,
        data: bytes,
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        total = len(data)
        sent = 0
        while sent < total:
            if should_cancel and should_cancel():
                raise DeviceError("Cancelled during file transfer")
            chunk = min(2048, total - sent)
            sent += chunk
            if progress:
                progress(sent, total)
            time.sleep(0.05)
        if self._update_staging is not None:
            self._update_staging[path] = data

    def file_read(self, path: str) -> bytes:
        return b"# mock file content\n"

    def reboot(self, hard: bool = False) -> None:
        self.stop_stream()
        self._connected = False
        self._info = None

    def enter_bootloader(self) -> None:
        self._connected = False
        self._info = None

    def wait_for_reconnect(self, port_name: str, timeout: float = 15.0) -> DeviceInfo:
        time.sleep(1.0)
        return self.connect(port_name)
