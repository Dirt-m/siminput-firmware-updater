"""Device/MockDevice interface parity. The app is verified with --mock, so a
mock that drifts from the real class hides bugs until they reach hardware."""
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.device import Device, DeviceLike, describe_fault  # noqa: E402
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


ANALOG_CONFIG = {
    "device": {"name": "Analog Mock", "pid": 0xF000},
    "bools": [],
    "axes": [{"id": "THR", "output": 2}],
    "rules": [
        {"type": "ANALOG", "input": "A6", "axis": "THR", "min": 1000, "max": 60000},
        {"type": "THRESHOLD", "input": "THR", "output": "B7", "above": 40000},
    ],
}


class MockAnalogSemantics(unittest.TestCase):
    """Firmware 2.7 analog: get_info reports the ADC pins and the claimed
    ones, get_state carries raw samples, and "an" rides in stream frames as
    ints, only when a sensor moved past the noise floor."""

    def setUp(self):
        self.dev = MockDevice(config=dict(ANALOG_CONFIG))
        self.dev._simulate_tick = lambda: None
        self.dev.connect("MOCK")
        self.frames = []
        self.lock = threading.Lock()

    def tearDown(self):
        self.dev.disconnect()

    def _cb(self, frame):
        with self.lock:
            self.frames.append(frame)

    def _collect(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.frames) >= count:
                    return list(self.frames)
            time.sleep(0.01)
        raise AssertionError("only %d frames" % len(self.frames))

    def test_info_reports_analog_pins_and_claimed_ones(self):
        info = self.dev.get_info()
        self.assertTrue(info.has_analog)
        self.assertEqual(info.analog_pins, ["A6", "A7", "A8"])
        self.assertEqual(info.analog_active, ["A6"])

    def test_state_has_int_samples(self):
        analog = self.dev.get_state()["analog"]
        self.assertEqual(set(analog), {"A6"})
        self.assertIs(type(analog["A6"]), int)

    def test_frames_carry_an_only_past_the_noise_floor(self):
        self.dev.start_stream(self._cb, interval_ms=10)
        first = self._collect(1)[0]
        self.assertTrue(first["snapshot"])
        self.assertEqual(set(first["an"]), {"A6"})
        base = first["an"]["A6"]
        self.dev.set_analog("A6", base + 10)      # below the 64-count floor
        time.sleep(0.15)
        with self.lock:
            # The axis it drives may move a count or two (that is a frame),
            # but the raw sample is not re-sent for jitter.
            self.assertFalse(any("an" in f for f in self.frames[1:]), self.frames[1:])
            seen = len(self.frames)
        self.dev.set_analog("A6", 50000)
        frame = self._collect(seen + 1)[seen]
        self.assertEqual(frame["an"], {"A6": 50000})
        self.assertIs(type(frame["an"]["A6"]), int)

    def test_axis_follows_sensor_and_threshold_lights_button(self):
        self.dev.set_analog("A6", 1000)
        state = self.dev.get_state()
        self.assertEqual(state["axes"][1], 0)
        self.assertNotIn(7, state["buttons"])
        self.dev.set_analog("A6", 60000)
        state = self.dev.get_state()
        self.assertEqual(state["axes"][1], 65535)
        self.assertIn(7, state["buttons"])
        self.dev.set_analog("A6", 30000)
        self.assertNotIn(7, self.dev.get_state()["buttons"])

    def test_digital_only_config_has_no_analog(self):
        dev = MockDevice()
        dev._simulate_tick = lambda: None
        dev.connect("MOCK")
        try:
            self.assertEqual(dev.get_info().analog_active, [])
            self.assertEqual(dev.get_state()["analog"], {})
            dev.start_stream(self._cb, interval_ms=10)
            self.assertNotIn("an", self._collect(1)[0])
        finally:
            dev.disconnect()


class FaultWording(unittest.TestCase):
    """get_info.fault → what the Device page shows."""

    def test_no_fault_renders_nothing(self):
        self.assertEqual(describe_fault(""), "")

    def test_analog_init_names_the_pin(self):
        text = describe_fault("analog_init:A2")
        self.assertTrue(text.startswith("A2 could not be opened as an analog input"), text)
        self.assertIn("check the config and the pin", text)

    def test_no_expander_and_unknown_faults(self):
        self.assertIn("expander", describe_fault("no_expander"))
        self.assertEqual(describe_fault("mystery"), "Device fault: mystery")

    def test_mock_reports_a_fault_through_get_info(self):
        dev = MockDevice()
        dev.fault = "analog_init:A6"
        self.assertEqual(dev.get_info().fault, "analog_init:A6")


if __name__ == "__main__":
    unittest.main()
