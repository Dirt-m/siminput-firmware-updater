"""End-to-end protocol test: the real Device driving the real firmware
SerialHandler over an in-process wire.

Needs the sibling firmware repo (set SIMINPUT_FW_PATH, or have it checked out
next to this repo as siminput-firmware-v2); skipped otherwise.
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

def _find_firmware() -> Path:
    env = os.environ.get("SIMINPUT_FW_PATH")
    if env:
        return Path(env)
    # Walk up so this also resolves from a git worktree under .claude/.
    for base in [REPO, *REPO.parents]:
        candidate = base.parent / "siminput-firmware-v2"
        if (candidate / "lib" / "serial_handler.py").exists():
            return candidate
    return REPO.parent / "siminput-firmware-v2"


FW_REPO = _find_firmware()
HAVE_FW = (FW_REPO / "lib" / "serial_handler.py").exists()

if HAVE_FW:
    mc = types.ModuleType("microcontroller")
    mc.nvm = bytearray(4096)
    mc.reset = lambda: None
    mc.on_next_reset = lambda *a: None
    mc.RunMode = types.SimpleNamespace(BOOTLOADER=None)
    sys.modules.setdefault("microcontroller", mc)
    sv = types.ModuleType("supervisor")
    sv.reload = lambda: None
    sv.ticks_ms = lambda: int(time.monotonic() * 1000) % (1 << 29)
    sys.modules.setdefault("supervisor", sv)
    sys.path.insert(0, str(FW_REPO / "lib"))
    import serial_handler  # noqa: E402

# Analog inputs arrived in firmware 2.7.0 (commit b52cd8e); an older checkout
# still runs the digital suite and skips the analog cases.
HAVE_ANALOG_FW = HAVE_FW and hasattr(serial_handler, "analog_pins_used")

from siminput_updater.config_model import Config, validate  # noqa: E402
from siminput_updater.device import Device, DeviceError  # noqa: E402


class FakeBox:
    def __init__(self):
        self.config = {"device": {"name": "ProtoBox", "pid": 0xF001},
                       "bools": [], "axes": [], "rules": []}
        self.pin_names = frozenset({"D1", "D2", "D3", "D4", "A1", "A2"})
        self.board_map = {"name": "rev2"}
        # Firmware 2.7 analog surface: ADC-capable pins, the AnalogIn objects
        # a config has claimed, and their latest raw samples.
        self.analog_pin_names = frozenset({"A1", "A2"})
        self.analog_ins = {}
        self.analog_raw = {}
        self.rules = []
        self.b_states = {}
        self.axis_states = {}
        self.axis_output_slot = {}
        self.bool_states = {}
        self.pin_cache = {}
        self.nvm = None


class FirmwareData:
    """The firmware's end of the wire. Both buffers are lock-guarded: once the
    device streams, its reader thread reads to_host while the test thread (or
    the pump) writes it."""

    def __init__(self, lock):
        self.lock = lock
        self.to_host = b""
        self.from_host = b""
        self.write_timeout = None

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.from_host)

    def read(self, n):
        with self.lock:
            out, self.from_host = self.from_host[:n], self.from_host[n:]
            return out

    def write(self, b):
        with self.lock:
            self.to_host += bytes(b)
            return len(b)

    def flush(self):
        pass


class HostPort:
    """Quacks like pyserial; every write is pumped through the firmware
    handler synchronously so replies are immediately readable.

    The pump lock keeps the handler single-threaded: a background Pump may be
    turning the firmware's main loop at the same time a test thread writes.
    """

    def __init__(self, handler, fw_data, lock):
        self.handler = handler
        self.fw = fw_data
        self.lock = lock
        self.pump_lock = threading.RLock()
        self.is_open = True
        self.timeout = 3.0
        self.write_timeout = 5.0

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.fw.to_host)

    def read(self, n):
        with self.lock:
            out, self.fw.to_host = self.fw.to_host[:n], self.fw.to_host[n:]
            return out

    def pump(self):
        """One turn of the firmware main loop: drain commands, then offer a
        stream frame (a no-op unless streaming)."""
        with self.pump_lock:
            while self.fw.in_waiting:
                self.handler.process()
            self.handler.maybe_send_stream()

    def write(self, b):
        with self.lock:
            self.fw.from_host += bytes(b)
        self.pump()
        return len(b)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self.lock:
            self.fw.to_host = b""

    def close(self):
        self.is_open = False


class Pump(threading.Thread):
    """Turns the firmware main loop in the background so stream frames appear
    without the host having to write anything."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self._halt = threading.Event()   # not _stop: Thread already owns that

    def run(self):
        while not self._halt.is_set():
            self.port.pump()
            time.sleep(0.002)

    def halt(self):
        self._halt.set()
        self.join(timeout=2.0)


@unittest.skipUnless(HAVE_FW, "sibling firmware repo not found")
class ProtocolIntegration(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.sandbox = tempfile.mkdtemp(prefix="siminput-proto-")
        os.makedirs(os.path.join(self.sandbox, "lib"))
        os.chdir(self.sandbox)
        with open("config.json", "w") as f:
            json.dump({"device": {"name": "ProtoBox", "pid": 0xF001}}, f)
        with open("code.py", "w") as f:
            f.write("# old\n")
        self.box = FakeBox()
        self.h = serial_handler.SerialHandler(self.box)
        self.wire_lock = threading.RLock()
        self.fw = FirmwareData(self.wire_lock)
        self.h._data = self.fw
        self.h._buf = bytearray(serial_handler._MAX_LINE)
        self.h._hash_algo = "sha256"
        self.d = Device()
        self.d._port = self.port = HostPort(self.h, self.fw, self.wire_lock)
        # Never adopt a real Adafruit HID node that happens to be plugged in:
        # these tests exercise the serial path.
        self.d.ensure_keepalive = lambda: None
        self.pump = None

    def tearDown(self):
        if self.pump is not None:
            self.pump.halt()
        try:
            self.d.stop_stream()
        except Exception:
            pass
        os.chdir(self._cwd)
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def test_round_trips_and_protocol2(self):
        info = self.d.get_info()
        self.assertEqual(info.name, "ProtoBox")
        self.assertGreaterEqual(info.protocol, 2)
        self.assertIn("D1", info.pins)
        cfg = self.d.get_config()
        self.assertEqual(cfg["device"]["name"], "ProtoBox")

    def test_stray_lines_do_not_impersonate_responses(self):
        self.fw.to_host = b'{"s": {"b": [1], "a": [0,0,0,0,0,0,0,0]}}\n{"ok": true}\n'
        cfg = self.d.get_config()
        self.assertEqual(cfg["device"]["name"], "ProtoBox")

    def test_stale_response_with_old_id_skipped(self):
        # A late reply carrying an old request id must not answer a new request.
        self.fw.to_host = json.dumps(
            {"ok": True, "config": {"device": {"name": "STALE"}}, "id": 0}
        ).encode() + b"\n"
        cfg = self.d.get_config()
        self.assertEqual(cfg["device"]["name"], "ProtoBox")

    def test_file_write_and_staged_update(self):
        payload = os.urandom(6000)
        self.d.file_write("code.py", payload)
        self.assertEqual(Path("code.py").read_bytes(), payload)

        self.d.update_begin()
        staged = b"# staged\n" * 50
        self.d.file_write("code.py", staged)
        self.assertEqual(Path("code.py").read_bytes(), payload)
        committed = self.d.update_commit()
        self.assertIn("code.py", committed)
        self.assertEqual(Path("code.py").read_bytes(), staged)

    def test_transfer_error_surfaces(self):
        class FailingFile:
            def __init__(self, real):
                self.real = real
                self.wrote = 0

            def write(self, data):
                self.wrote += 1
                if self.wrote >= 2:
                    raise OSError(28, "No space left on device")
                return self.real.write(data)

            def close(self):
                self.real.close()

        orig = self.h._start_chunk_receive

        def patched(op, size, meta):
            orig(op, size, meta)
            if op == "file_write" and self.h._chunk_file is not None:
                self.h._chunk_file = FailingFile(self.h._chunk_file)

        self.h._start_chunk_receive = patched
        with self.assertRaises(DeviceError) as ctx:
            self.d.file_write("code.py", os.urandom(9000))
        self.assertIn("write error", str(ctx.exception))
        self.h._start_chunk_receive = orig
        self.assertEqual(self.d.ping().name, "ProtoBox")  # handler recovered

    def test_chunked_set_config_rebooting(self):
        cfg = {"device": {"name": "BigCfg", "pid": 0xF002},
               "rules": [{"type": "MAP", "input": "D1", "output": "B%d" % (i % 120 + 1)}
                         for i in range(120)]}
        resp = self.d.set_config(cfg)
        self.assertTrue(resp.get("rebooting"))
        self.assertEqual(json.loads(Path("config.json").read_text())["device"]["name"], "BigCfg")

    # ------------------------------------------------------------ streaming

    def _stream(self, interval_ms=20):
        """Start a background firmware pump and a device stream; returns a
        thread-safe frame list."""
        frames = []
        lock = threading.Lock()

        def cb(frame):
            with lock:
                frames.append(frame)

        self.pump = Pump(self.port)
        self.pump.start()
        self.d.start_stream(cb, interval_ms=interval_ms)
        return frames, lock

    @staticmethod
    def _wait(frames, lock, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                if len(frames) >= count:
                    return list(frames)
            time.sleep(0.005)
        with lock:
            raise AssertionError("only %d frames after %.1fs: %r"
                                 % (len(frames), timeout, frames))

    def test_stream_snapshot_then_delta_then_commands_resume(self):
        self.box.pin_cache = {"D1": False, "D2": False, "D3": False, "D4": False}
        frames, lock = self._stream()

        first = self._wait(frames, lock, 1)[0]
        self.assertEqual(first["src"], "serial")
        self.assertTrue(first["snapshot"], "first frame must be a full pin snapshot")
        self.assertEqual(first["p"], {"D1": False, "D2": False, "D3": False, "D4": False})

        with self.wire_lock:
            self.box.pin_cache["D3"] = True
        delta = self._wait(frames, lock, 2)[1]
        self.assertFalse(delta.get("snapshot"))
        self.assertEqual(delta["p"], {"D3": True})  # changes only

        self.d.stop_stream()
        self.assertFalse(self.h._streaming)
        # The reader released the port cleanly: ordinary commands work again.
        self.assertEqual(self.d.get_config()["device"]["name"], "ProtoBox")

    def test_restarting_the_stream_is_harmless(self):
        self.box.pin_cache = {"D1": False, "D2": True}
        frames, lock = self._stream()
        self._wait(frames, lock, 1)

        # Re-issuing stream_start mid-stream (what the monitor's watchdog does
        # to revive a stream the firmware dropped) must not desync anything.
        self.d.rearm_stream()
        after = self._wait(frames, lock, 2)
        self.assertTrue(after[1]["snapshot"], "a re-arm must re-send a full snapshot")
        self.assertEqual(after[1]["p"], {"D1": False, "D2": True})

        # And a full restart on top of a running stream.
        self.d.start_stream(lambda f: None, interval_ms=20)
        self.assertTrue(self.h._streaming)
        self.d.stop_stream()
        self.assertEqual(self.d.get_config()["device"]["name"], "ProtoBox")

    def test_bad_config_rejected(self):
        with self.assertRaises(DeviceError) as ctx:
            self.d.set_config({"device": {"pid": 0x80F4}})
        self.assertIn("0x80F4", str(ctx.exception))

    # ---------------------------------------------------------- analog (2.7)

    def _needs_analog(self):
        if not HAVE_ANALOG_FW:
            self.skipTest("firmware checkout predates analog support (b52cd8e)")

    def test_get_info_and_get_state_report_analog(self):
        self._needs_analog()
        info = self.d.get_info()
        self.assertTrue(info.has_analog)
        self.assertEqual(info.analog_pins, ["A1", "A2"])
        self.assertEqual(info.analog_active, [])
        self.assertEqual(self.d.get_state()["analog"], {})

        self.box.analog_ins = {"A1": object()}
        self.box.analog_raw = {"A1": 12000}
        self.assertEqual(self.d.get_info().analog_active, ["A1"])
        self.assertEqual(self.d.get_state()["analog"], {"A1": 12000})

    def test_stream_resends_an_only_past_the_noise_floor(self):
        self._needs_analog()
        self.box.pin_cache = {"D1": False}
        self.box.analog_ins = {"A1": object()}
        self.box.analog_raw = {"A1": 1000}
        frames, lock = self._stream()

        first = self._wait(frames, lock, 1)[0]
        self.assertTrue(first["snapshot"])
        self.assertEqual(first["an"], {"A1": 1000})

        with self.wire_lock:
            self.box.analog_raw["A1"] = 1010      # ADC jitter: below the 64-count floor
        time.sleep(0.15)
        with lock:
            self.assertEqual(len(frames), 1, "jitter must not produce frames")

        with self.wire_lock:
            self.box.analog_raw["A1"] = 1100
        second = self._wait(frames, lock, 2)[1]
        self.assertEqual(second["an"], {"A1": 1100})
        self.assertNotIn("p", second, "pins did not change, so no p")

    def test_firmware_validates_analog_rules_like_the_client(self):
        self._needs_analog()
        cfg = {
            "device": {"name": "ProtoBox", "pid": 0xF001},
            "axes": [{"id": "THR", "output": 2}],
            "rules": [
                {"type": "ANALOG", "input": "A1", "axis": "THR", "min": 1000, "max": 60000, "curve": 1.4},
                {"type": "THRESHOLD", "input": "THR", "output": "B7", "above": 40000, "hysteresis": 500},
            ],
        }
        self.d.validate_config(cfg)   # accepted by the firmware
        self.assertEqual([str(e) for e in validate(Config.from_dict(cfg), pins=["D1", "A1", "A2"],
                                                    analog_pins=["A1", "A2"])], [])

        bad = dict(cfg)
        bad["rules"] = cfg["rules"] + [{"type": "MAP", "input": "A1", "output": "B1"}]
        with self.assertRaises(DeviceError) as ctx:
            self.d.validate_config(bad)
        self.assertIn("A1", str(ctx.exception))
        client = [str(e) for e in validate(Config.from_dict(bad), pins=["D1", "A1", "A2"],
                                            analog_pins=["A1", "A2"])]
        self.assertIn("rules[2].input: 'A1' is claimed as an analog input", client)

    def test_firmware_rule_caps_match_the_client(self):
        self._needs_analog()
        if not hasattr(serial_handler, "_MAX_ANALOG_RULES"):
            self.skipTest("firmware checkout predates the analog rule caps (d5b0c86)")
        # The count cap is checked before the per-rule pass, so one shared
        # axis is enough (and keeps the axis-slot check out of the way).
        too_many = {"device": {"name": "ProtoBox", "pid": 0xF001}, "axes": [{"id": "AX0", "output": 1}],
                    "rules": [{"type": "ANALOG", "input": "A1", "axis": "AX0"} for _ in range(17)]}
        with self.assertRaises(DeviceError) as ctx:
            self.d.validate_config(too_many)
        self.assertEqual(str(ctx.exception), "too many ANALOG rules (max 16)")
        self.assertIn("rules: too many ANALOG rules (max 16)",
                      [str(e) for e in validate(Config.from_dict(too_many), pins=["A1", "A2"],
                                                analog_pins=["A1", "A2"])])

        thresholds = {"device": {"name": "ProtoBox", "pid": 0xF001},
                      "rules": [{"type": "THRESHOLD", "input": "A1", "output": f"B{i + 1}", "above": 100}
                                for i in range(33)]}
        with self.assertRaises(DeviceError) as ctx:
            self.d.validate_config(thresholds)
        self.assertEqual(str(ctx.exception), "too many THRESHOLD rules (max 32)")

    def test_malformed_config_reports_the_detail(self):
        self._needs_analog()
        malformed = {"device": {"name": "ProtoBox", "pid": 0xF001},
                     "rules": [{"type": "MAP", "input": ["D1"], "output": "B1"}]}
        with self.assertRaises(DeviceError) as ctx:
            self.d.validate_config(malformed)
        # Shown verbatim in the UI; nothing pattern-matches on it.
        self.assertTrue(str(ctx.exception), "the firmware must say what is wrong")
        # And the chunked path (large configs) surfaces it too instead of going quiet.
        malformed["padding"] = "x" * 4000
        with self.assertRaises(DeviceError):
            self.d.validate_config(malformed)


if __name__ == "__main__":
    unittest.main()
