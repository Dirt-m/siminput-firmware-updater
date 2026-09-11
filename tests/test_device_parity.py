"""Device/MockDevice interface parity. The app is verified with --mock, so a
mock that drifts from the real class hides bugs until they reach hardware."""
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.device import Device, DeviceLike  # noqa: E402
from siminput_updater.mock_device import MockDevice  # noqa: E402

PROTOCOL_METHODS = [
    name for name, member in vars(DeviceLike).items()
    if not name.startswith("_") and callable(member)
]


class Parity(unittest.TestCase):
    def test_both_implement_the_protocol_surface(self):
        for cls in (Device, MockDevice):
            for name in PROTOCOL_METHODS:
                self.assertTrue(hasattr(cls, name), f"{cls.__name__} missing {name}")

    def test_signatures_accept_the_same_calls(self):
        for name in PROTOCOL_METHODS:
            proto_sig = inspect.signature(getattr(DeviceLike, name))
            for cls in (Device, MockDevice):
                impl = getattr(cls, name)
                if isinstance(inspect.getattr_static(cls, name), (staticmethod, classmethod)):
                    continue
                impl_params = list(inspect.signature(impl).parameters)
                proto_params = list(proto_sig.parameters)
                self.assertEqual(
                    impl_params[:len(proto_params)], proto_params,
                    f"{cls.__name__}.{name} signature drifted from DeviceLike",
                )


class MockStreamSemantics(unittest.TestCase):
    """The mock is what --mock runs against, so its stream must behave like
    serial_handler.maybe_send_stream: change-driven frames, changes-only pins,
    and a full pin snapshot as the first frame after every stream_start."""

    def setUp(self):
        self.dev = MockDevice()
        self.dev.connect("MOCK")
        self.frames = []
        self.lock = threading.Lock()

    def tearDown(self):
        self.dev.disconnect()

    def _collect(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.frames) >= count:
                    return list(self.frames)
            time.sleep(0.01)
        raise AssertionError("only %d frames" % len(self.frames))

    def _cb(self, frame):
        with self.lock:
            self.frames.append(frame)

    def test_first_frame_is_a_full_pin_snapshot(self):
        self.dev.start_stream(self._cb, interval_ms=10)
        first = self._collect(1)[0]
        self.assertTrue(first["snapshot"])
        self.assertEqual(set(first["p"]), set(self.dev.get_info().pins))

    def test_pin_toggles_arrive_as_deltas(self):
        self.dev._simulate_tick = lambda: None  # deterministic: we drive it
        self.dev.start_stream(self._cb, interval_ms=10)
        self._collect(1)
        pin = sorted(self.dev._pins)[0]
        self.dev._pins[pin] = True
        frame = self._collect(2)[1]
        self.assertFalse(frame.get("snapshot"))
        self.assertEqual(frame["p"], {pin: True})

    def test_no_frames_while_nothing_changes(self):
        self.dev._simulate_tick = lambda: None
        self.dev.start_stream(self._cb, interval_ms=10)
        self._collect(1)
        time.sleep(0.2)
        with self.lock:
            self.assertEqual(len(self.frames), 1)

    def test_rearm_resends_a_snapshot(self):
        self.dev._simulate_tick = lambda: None
        self.dev.start_stream(self._cb, interval_ms=10)
        self._collect(1)
        self.dev.rearm_stream()
        self.assertTrue(self._collect(2)[1]["snapshot"])


if __name__ == "__main__":
    unittest.main()
