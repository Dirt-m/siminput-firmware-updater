"""LiveMonitor's decision logic, without Tk.

The merge rule and the stream watchdog are the parts that go wrong silently on
hardware, so they live in plain classes/functions that can be driven directly.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.monitor import (  # noqa: E402
    REARM_GAP_S, SILENCE_S, StateMerger, should_rearm,
)


def serial(b=None, a=None, p=None, an=None, snapshot=False):
    frame = {"src": "serial"}
    if b is not None:
        frame["b"] = b
    if a is not None:
        frame["a"] = a
    if p is not None:
        frame["p"] = p
    if an is not None:
        frame["an"] = an
    if snapshot:
        frame["snapshot"] = True
    return frame


def evdev(b=None, a=None):
    frame = {"src": "evdev"}
    if b is not None:
        frame["b"] = b
    if a is not None:
        frame["a"] = a
    return frame


class MergeWithoutEvdev(unittest.TestCase):
    """Windows, or Linux without a readable HID node: serial supplies all three."""

    def setUp(self):
        self.m = StateMerger(evdev_active=False)

    def test_serial_supplies_buttons_axes_and_pins(self):
        state = self.m.feed(serial(b=[1, 4], a=[0] * 8, p={"D1": True, "D2": False},
                                   snapshot=True))
        self.assertEqual(state["b"], {1, 4})
        self.assertEqual(state["a"], [0] * 8)
        self.assertEqual(state["p"], {"D1": True, "D2": False})

    def test_short_axis_list_is_padded_to_eight(self):
        state = self.m.feed(serial(a=[1, 2, 3]))
        self.assertEqual(state["a"], [1, 2, 3, 32767, 32767, 32767, 32767, 32767])

    def test_buttons_are_replaced_not_accumulated(self):
        self.m.feed(serial(b=[1, 2]))
        self.assertEqual(self.m.feed(serial(b=[2]))["b"], {2})


class MergeWithEvdev(unittest.TestCase):
    """Linux: evdev owns buttons and axes, the serial stream owns pins only."""

    def setUp(self):
        self.m = StateMerger(evdev_active=True)

    def test_serial_buttons_and_axes_are_ignored(self):
        self.m.feed(evdev(b=[7], a=[100] * 8))
        # A serial frame from before the press must not un-press B7.
        state = self.m.feed(serial(b=[], a=[32767] * 8, p={"D9": True}, snapshot=True))
        self.assertEqual(state["b"], {7}, "a late serial frame un-pressed a button")
        self.assertEqual(state["a"], [100] * 8)
        self.assertEqual(state["p"], {"D9": True})

    def test_evdev_never_touches_pins(self):
        self.m.feed(serial(p={"D1": True}, snapshot=True))
        self.assertEqual(self.m.feed(evdev(b=[1]))["p"], {"D1": True})


class PinDeltasAndSnapshots(unittest.TestCase):
    def setUp(self):
        self.m = StateMerger()

    def test_snapshot_sets_the_baseline_without_reporting_transitions(self):
        state = self.m.feed(serial(p={"D1": True, "D2": False}, snapshot=True))
        self.assertTrue(state["snapshot"])
        self.assertEqual(state["changed"], set(),
                         "a snapshot restates every pin; nothing transitioned")

    def test_deltas_merge_into_the_full_pin_map(self):
        self.m.feed(serial(p={"D1": False, "D2": False, "D3": False}, snapshot=True))
        state = self.m.feed(serial(p={"D2": True}))
        self.assertEqual(state["p"], {"D1": False, "D2": True, "D3": False})
        self.assertEqual(state["changed"], {"D2"})

    def test_restating_a_pin_at_its_current_value_is_not_a_change(self):
        self.m.feed(serial(p={"D1": True}, snapshot=True))
        self.assertEqual(self.m.feed(serial(p={"D1": True}))["changed"], set())

    def test_a_rearm_snapshot_does_not_refire_held_pins(self):
        self.m.feed(serial(p={"D1": False}, snapshot=True))
        self.m.feed(serial(p={"D1": True}))
        # Watchdog re-arm: the firmware resends everything it knows.
        state = self.m.feed(serial(p={"D1": True}, snapshot=True))
        self.assertEqual(state["changed"], set())
        self.assertEqual(state["p"], {"D1": True})

    def test_state_is_copied_out_not_aliased(self):
        state = self.m.feed(serial(b=[1], p={"D1": True}, snapshot=True))
        state["p"]["D1"] = False
        state["b"].add(9)
        self.assertEqual(self.m.pins, {"D1": True})
        self.assertEqual(self.m.buttons, {1})


class AnalogSamples(unittest.TestCase):
    """Firmware 2.7 "an": raw ADC samples, ints, merged not replaced."""

    def setUp(self):
        self.m = StateMerger(evdev_active=True)

    def test_samples_stay_ints(self):
        state = self.m.feed(serial(an={"A1": 1, "A2": 0}, snapshot=True))
        self.assertEqual(state["an"], {"A1": 1, "A2": 0})
        self.assertIs(type(state["an"]["A1"]), int)
        self.assertIsNot(state["an"]["A1"], True)

    def test_partial_frames_merge(self):
        self.m.feed(serial(an={"A1": 100, "A2": 200}, snapshot=True))
        state = self.m.feed(serial(an={"A1": 150}))
        self.assertEqual(state["an"], {"A1": 150, "A2": 200})

    def test_frames_without_an_keep_the_last_samples(self):
        self.m.feed(serial(an={"A1": 100}, snapshot=True))
        self.assertEqual(self.m.feed(serial(p={"D1": True}))["an"], {"A1": 100})
        self.assertEqual(self.m.feed(evdev(b=[1]))["an"], {"A1": 100})

    def test_garbage_values_are_ignored(self):
        state = self.m.feed(serial(an={"A1": True, "A2": "x", "A3": 7.0}))
        self.assertEqual(state["an"], {"A3": 7})

    def test_state_copies_the_map(self):
        state = self.m.feed(serial(an={"A1": 5}))
        state["an"]["A1"] = 9
        self.assertEqual(self.m.analog["A1"], 5)


class Watchdog(unittest.TestCase):
    def test_quiet_stream_is_rearmed(self):
        self.assertTrue(should_rearm(now=100.0, last_frame=100.0 - SILENCE_S,
                                     last_rearm=0.0))

    def test_a_live_stream_is_left_alone(self):
        self.assertFalse(should_rearm(now=100.0, last_frame=99.5, last_rearm=0.0))

    def test_rearms_are_rate_limited(self):
        # Still silent, but we re-armed a moment ago: don't spam the device.
        now = 100.0
        self.assertFalse(should_rearm(now, last_frame=0.0,
                                      last_rearm=now - REARM_GAP_S / 2))
        self.assertTrue(should_rearm(now, last_frame=0.0,
                                     last_rearm=now - REARM_GAP_S))


if __name__ == "__main__":
    unittest.main()
