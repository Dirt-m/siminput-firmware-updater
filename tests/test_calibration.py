"""The calibration arithmetic, without a window."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.widgets.calibrate_dialog import calibrated_range, suggest_deadzone  # noqa: E402


class CalibratedRange(unittest.TestCase):
    def test_one_percent_of_travel_on_both_ends(self):
        self.assertEqual(calibrated_range(5000, 60000), (5550, 59450))

    def test_rest_wobble_wins_when_larger(self):
        self.assertEqual(calibrated_range(5000, 60000, rest_spread=900), (5900, 59100))

    def test_center_and_deadzone_stay_strictly_inside(self):
        lo, hi = calibrated_range(1000, 2000, rest_spread=400, center=1050, deadzone=20)
        self.assertLess(lo, 1050 - 20)
        self.assertGreater(hi, 1050 + 20)
        self.assertEqual(lo, 1000 + (1050 - 20 - 1000 - 1))

    def test_tiny_travel_is_left_alone(self):
        self.assertEqual(calibrated_range(100, 101), (100, 101))
        self.assertEqual(calibrated_range(100, 100), (100, 100))


class SuggestedDeadzone(unittest.TestCase):
    def test_wobble_plus_noise_floor_rounded_to_16(self):
        self.assertEqual(suggest_deadzone(32000, 32090), 160)   # 90 + 64 = 154 -> 160
        self.assertEqual(suggest_deadzone(32000, 32000), 64)


if __name__ == "__main__":
    unittest.main()
