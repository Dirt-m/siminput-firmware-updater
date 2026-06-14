from __future__ import annotations

import copy
import random
import threading
import time
from typing import Callable

from .config_model import Config, pins_for_board, validate
from .device import DeviceError, DeviceInfo, FullDeviceInfo

MOCK_BOARD_MAP = "rev1"

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
        self._streaming = False
        self._stream_thread: threading.Thread | None = None
        self._stream_callback: Callable[[dict], None] | None = None
        self._connected = False
        self._info: DeviceInfo | None = None
        self._buttons: set[int] = set()
        self._axes: list[int] = [32767] * 8
        self._bools: dict[str, bool] = {}
        self._pins: dict[str, bool] = {}
        self._update_staging: dict[str, bytes] | None = None
        self._init_state()

    def _init_state(self):
        self._pins = {p: False for p in pins_for_board(MOCK_BOARD_MAP)}
        cfg = Config.from_dict(self._config)
        self._bools = {b.id: b.default for b in cfg.bools}
        for a in cfg.axes:
            slot = a.output
            if isinstance(slot, int) and 1 <= slot <= 8:
                self._axes[slot - 1] = a.default

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
            version="2.3.0-mock",
            name="Mock SimInput Box",
            pid=0xF000,
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
        )]

    def connect(self, port_name: str) -> DeviceInfo:
        self._connected = True
        self._info = DeviceInfo(
            product="SIMINPUT",
            version="2.3.0-mock",
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
            version="2.3.0-mock",
            name=self._config.get("device", {}).get("name", "Mock SimInput Box"),
            pid=self._config.get("device", {}).get("pid", 0xF000),
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
        )

    def get_info(self) -> FullDeviceInfo:
        return FullDeviceInfo(
            product="SIMINPUT",
            version="2.3.0-mock",
            name=self._config.get("device", {}).get("name", "Mock SimInput Box"),
            pid=self._config.get("device", {}).get("pid", 0xF000),
            port="MOCK",
            board_map=MOCK_BOARD_MAP,
            circuitpython="9.2.0",
            board="raspberry_pi_pico",
            nvm_size=4096,
            pins={
                "expander": [f"D{i}" for i in range(1, 15)],
                "gpio": [f"D{i}" for i in range(15, 25)],
                "adc": ["A6", "A7", "A8"],
            },
            bools=[b["id"] for b in self._config.get("bools", [])],
            axes=[a["id"] for a in self._config.get("axes", [])],
            rules_count=len(self._config.get("rules", [])),
        )

    def get_config(self) -> dict:
        return copy.deepcopy(self._config)

    def set_config(self, config: dict) -> None:
        cfg = Config.from_dict(config)
        errs = validate(cfg, board_map=MOCK_BOARD_MAP)
        if errs:
            raise DeviceError(str(errs[0]))
        self._config = copy.deepcopy(config)
        self._init_state()

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
        }

    def start_stream(self, callback: Callable[[dict], None], interval_ms: int = 50):
        self.stop_stream()
        self._stream_callback = callback
        self._streaming = True
        self._stream_thread = threading.Thread(target=self._stream_loop, args=(interval_ms,), daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        self._streaming = False
        if self._stream_thread:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        self._stream_callback = None

    def _stream_loop(self, interval_ms: int):
        while self._streaming:
            self._simulate_tick()
            if self._stream_callback:
                self._stream_callback({
                    "b": sorted(self._buttons),
                    "a": list(self._axes),
                    "p": {k: v for k, v in self._pins.items() if v},
                })
            time.sleep(interval_ms / 1000.0)

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
            delta = random.randint(-2048, 2048)
            self._axes[idx] = max(0, min(65535, self._axes[idx] + delta))

    def update_begin(self) -> None:
        if self._update_staging is not None:
            raise DeviceError("update already in progress")
        self._update_staging = {}

    def update_commit(self) -> list[str]:
        if self._update_staging is None:
            raise DeviceError("no update in progress")
        committed = list(self._update_staging.keys())
        self._update_staging = None
        return committed

    def update_abort(self) -> None:
        self._update_staging = None

    def file_write(
        self,
        path: str,
        data: bytes,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        total = len(data)
        sent = 0
        while sent < total:
            chunk = min(2048, total - sent)
            sent += chunk
            if progress:
                progress(sent, total)
            time.sleep(0.05)
        if self._update_staging is not None:
            self._update_staging[path] = data

    def file_read(self, path: str) -> bytes:
        return b"# mock file content\n"

    def reboot(self) -> None:
        self._connected = False
        self._info = None

    def enter_bootloader(self) -> None:
        self._connected = False
        self._info = None

    def wait_for_reconnect(self, port_name: str, timeout: float = 15.0) -> DeviceInfo:
        time.sleep(1.0)
        return self.connect(port_name)
