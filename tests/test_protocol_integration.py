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
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FW_REPO = Path(os.environ.get("SIMINPUT_FW_PATH",
                              REPO.parent.parent.parent / "siminput-firmware-v2"))
if not (FW_REPO / "lib" / "serial_handler.py").exists():
    FW_REPO = REPO.parent / "siminput-firmware-v2"
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

from siminput_updater.device import Device, DeviceError  # noqa: E402


class FakeBox:
    def __init__(self):
        self.config = {"device": {"name": "ProtoBox", "pid": 0xF001},
                       "bools": [], "axes": [], "rules": []}
        self.pin_names = frozenset({"D1", "D2", "D3", "D4"})
        self.board_map = {"name": "rev2"}
        self.rules = []
        self.b_states = {}
        self.axis_states = {}
        self.axis_output_slot = {}
        self.bool_states = {}
        self.pin_cache = {}
        self.nvm = None


class FirmwareData:
    def __init__(self):
        self.to_host = b""
        self.from_host = b""
        self.write_timeout = None

    @property
    def in_waiting(self):
        return len(self.from_host)

    def read(self, n):
        out, self.from_host = self.from_host[:n], self.from_host[n:]
        return out

    def write(self, b):
        self.to_host += bytes(b)
        return len(b)

    def flush(self):
        pass


class HostPort:
    """Quacks like pyserial; every write is pumped through the firmware
    handler synchronously so replies are immediately readable."""

    def __init__(self, handler, fw_data):
        self.handler = handler
        self.fw = fw_data
        self.is_open = True
        self.timeout = 3.0
        self.write_timeout = 5.0

    @property
    def in_waiting(self):
        return len(self.fw.to_host)

    def read(self, n):
        out, self.fw.to_host = self.fw.to_host[:n], self.fw.to_host[n:]
        return out

    def write(self, b):
        self.fw.from_host += bytes(b)
        while self.fw.in_waiting:
            self.handler.process()
        return len(b)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.fw.to_host = b""

    def close(self):
        self.is_open = False


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
        self.h = serial_handler.SerialHandler(FakeBox())
        self.fw = FirmwareData()
        self.h._data = self.fw
        self.h._buf = bytearray(serial_handler._MAX_LINE)
        self.h._hash_algo = "sha256"
        self.d = Device()
        self.d._port = HostPort(self.h, self.fw)

    def tearDown(self):
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

    def test_bad_config_rejected(self):
        with self.assertRaises(DeviceError) as ctx:
            self.d.set_config({"device": {"pid": 0x80F4}})
        self.assertIn("0x80F4", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
