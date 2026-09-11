"""Config model round-trip and validation tests. Run: python -m unittest discover tests"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.config_model import (  # noqa: E402
    Axis, BoolVar, Config, Rule, validate,
)


class RoundTrip(unittest.TestCase):
    def test_unknown_keys_survive(self):
        src = {
            "device": {"name": "Box", "pid": 0xF000, "future_flag": True},
            "bools": [{"id": "T1", "default": False, "color": "#fff"}],
            "axes": [{"id": "AX1", "output": 1, "default": 30000, "curve": "log"}],
            "rules": [{"type": "MAP", "input": "D1", "output": "B1", "note": "hi"}],
            "profiles": [{"name": "default"}],
        }
        out = Config.from_dict(src).to_dict()
        self.assertEqual(out["device"]["future_flag"], True)
        self.assertEqual(out["bools"][0]["color"], "#fff")
        self.assertEqual(out["axes"][0]["curve"], "log")
        self.assertEqual(out["rules"][0]["note"], "hi")
        self.assertEqual(out["profiles"], [{"name": "default"}])

    def test_comment_rows_survive(self):
        src = {
            "bools": [{"comment": "section"}, {"id": "T1"}],
            "rules": [{"comment": "--- gear ---"}, {"type": "MAP", "input": "D1", "output": "B1"}],
        }
        cfg = Config.from_dict(src)
        self.assertTrue(cfg.bools[0].comment)
        self.assertTrue(cfg.rules[0].comment)
        out = cfg.to_dict()
        self.assertEqual(out["bools"][0], {"comment": "section"})
        self.assertEqual(out["rules"][0], {"comment": "--- gear ---"})
        # Comment rows produce no validation errors.
        errs = [e for e in validate(cfg, board_map="rev2") if "bools[0]" in e.path or "rules[0]" in e.path]
        self.assertEqual(errs, [])

    def test_values_stable(self):
        src = {
            "device": {"name": "Box", "pid": 0xF001, "debounce_ms": 15, "inactivity_refresh": 2.0},
            "axes": [{"id": "AX1", "output": 3, "default": 30000}],
            "rules": [{"type": "ENCODER", "inputs": ["D1", "D2"], "cw": "B5"}],
        }
        out = Config.from_dict(src).to_dict()
        self.assertEqual(out["axes"][0]["default"], 30000)
        self.assertNotIn("pulse_ms", out["rules"][0])  # encoder default is 0, not 100


class Validation(unittest.TestCase):
    def _errs(self, d, **kw):
        return [str(e) for e in validate(Config.from_dict(d), board_map="rev2", **kw)]

    def test_blank_required_fields_rejected(self):
        errs = self._errs({"rules": [
            {"type": "MAP", "input": "", "output": ""},
            {"type": "NOR", "inputs": [], "output": "B1"},
            {"type": "AXIS_INC", "input": "D1", "axis": ""},
        ]})
        self.assertTrue(any("rules[0].input" in e for e in errs))
        self.assertTrue(any("rules[0].output" in e for e in errs))
        self.assertTrue(any("rules[1].inputs" in e for e in errs))
        self.assertTrue(any("rules[2].axis" in e for e in errs))

    def test_encoder_outputs_validated(self):
        errs = self._errs({"rules": [
            {"type": "ENCODER", "inputs": ["D1", "D2"], "cw": "B999", "ccw": "D5"},
        ]})
        self.assertTrue(any("rules[0].cw" in e for e in errs))
        self.assertTrue(any("rules[0].ccw" in e for e in errs))

    def test_duplicate_encoder_pins_rejected(self):
        errs = self._errs({"rules": [
            {"type": "ENCODER", "inputs": ["D1", "D2"]},
            {"type": "ENCODER", "inputs": ["D2", "D3"]},
        ]})
        self.assertTrue(any("already used" in e for e in errs))

    def test_firmware_accepted_values_pass(self):
        # Values the firmware accepts must not be rejected client-side.
        errs = self._errs({"device": {"name": "B", "pid": 1,
                                      "debounce_ms": 10.5,
                                      "inactivity_refresh": None}})
        self.assertEqual(errs, [])

    def test_backlight_case_insensitive(self):
        errs = self._errs({"axes": [{"id": "AX1", "output": "backlight"}]})
        self.assertFalse(any("output" in e for e in errs), errs)

    def test_device_pin_list_used(self):
        d = {"rules": [{"type": "MAP", "input": "GP10", "output": "B1"}]}
        self.assertTrue(any("Unknown input" in e for e in self._errs(d)))
        self.assertEqual(self._errs(d, pins=["GP10", "GP11"]), [])

    def test_size_cap(self):
        d = {"rules": [{"type": "MAP", "input": "D1", "output": "B1"}] * 250,
             "device": {"name": "x" * 32}}
        big = Config.from_dict(d)
        big.extra["padding"] = "y" * 40000
        errs = [str(e) for e in validate(big, board_map="rev2")]
        self.assertTrue(any("too large" in e for e in errs))


EXAMPLE_ANALOG_RULES = [
    {"type": "ANALOG", "input": "A1", "axis": "THROTTLE", "min": 9800, "max": 41200, "filter": 3, "curve": 1.4},
    {"type": "ANALOG", "input": "A2", "axis": "STEER", "min": 1200, "center": 31900, "max": 64000, "deadzone": 400},
    {"type": "THRESHOLD", "input": "A3", "output": "B40", "above": 30000, "hysteresis": 1500},
    {"type": "THRESHOLD", "input": "THROTTLE", "output": "B41", "below": 500},
]


def _analog_cfg(rules=None, axes=None):
    return {
        "axes": axes if axes is not None else [
            {"id": "AX1", "output": 1, "default": 32767, "store": True},
            {"id": "THROTTLE", "output": 2},
            {"id": "STEER", "output": 3},
        ],
        "rules": rules if rules is not None else list(EXAMPLE_ANALOG_RULES),
    }


class AnalogRules(unittest.TestCase):
    """Firmware 2.7 ANALOG/THRESHOLD: fields round-trip exactly and the
    client validator mirrors lib/serial_handler.validate_config."""

    def _errs(self, d, **kw):
        kw.setdefault("board_map", "rev2")
        return [str(e) for e in validate(Config.from_dict(d), **kw)]

    def test_example_rules_round_trip_exactly(self):
        out = Config.from_dict(_analog_cfg()).to_dict()["rules"]
        self.assertEqual(out, EXAMPLE_ANALOG_RULES)

    def test_unset_fields_stay_out(self):
        r = Rule(type="ANALOG", input="A1", axis="AX1")
        self.assertEqual(r.to_dict(), {"type": "ANALOG", "input": "A1", "axis": "AX1"})
        t = Rule(type="THRESHOLD", input="A1", output="B1", below=100)
        self.assertEqual(t.to_dict(), {"type": "THRESHOLD", "input": "A1", "output": "B1", "below": 100})

    def test_copy_does_not_share_curve_table(self):
        r = Rule(type="ANALOG", input="A1", axis="AX1", curve=[[0, 0], [65535, 65535]])
        c = r.copy()
        c.curve.append([1, 1])
        self.assertEqual(len(r.curve), 2)

    def test_uses_analog(self):
        self.assertTrue(Config.from_dict(_analog_cfg()).uses_analog())
        self.assertFalse(Config.from_dict({"rules": [{"type": "MAP", "input": "D1", "output": "B1"}]}).uses_analog())

    def test_example_config_is_valid(self):
        self.assertEqual(self._errs(_analog_cfg()), [])

    def test_claimed_pin_cannot_be_a_digital_input(self):
        errs = self._errs(_analog_cfg(rules=[
            {"type": "ANALOG", "input": "A1", "axis": "THROTTLE"},
            {"type": "MAP", "input": "A1", "output": "B1"},
            {"type": "ENCODER", "inputs": ["A1", "D2"], "cw": "B2"},
        ]))
        self.assertIn("rules[1].input: 'A1' is claimed as an analog input", errs)
        self.assertIn("rules[2].inputs[0]: Pin 'A1' is claimed as an analog input", errs)

    def test_analog_axis_rules(self):
        errs = self._errs(_analog_cfg(rules=[
            {"type": "ANALOG", "input": "A1", "axis": "AX1"},          # AX1 has store
            {"type": "ANALOG", "input": "A2", "axis": "THROTTLE"},
            {"type": "ANALOG", "input": "A3", "axis": "THROTTLE"},     # second driver
            {"type": "AXIS_INC", "input": "D1", "axis": "THROTTLE"},   # stepped analog axis
            {"type": "ANALOG", "input": "A4", "axis": "NOPE"},
        ]))
        self.assertIn("rules[0].axis: Axis 'AX1' cannot use store (its value comes from the sensor)", errs)
        self.assertIn("rules[2].axis: Axis 'THROTTLE' is already driven by rule 2", errs)
        self.assertIn("rules[3].axis: Axis 'THROTTLE' is driven by an ANALOG rule", errs)
        self.assertIn("rules[4].axis: Unknown axis 'NOPE'", errs)

    def test_analog_ranges(self):
        errs = self._errs(_analog_cfg(rules=[
            {"type": "ANALOG", "input": "A1", "axis": "THROTTLE", "min": 5000, "max": 5000},
            {"type": "ANALOG", "input": "A2", "axis": "STEER", "deadzone": 100},
            {"type": "ANALOG", "input": "A3", "axis": "AX1", "min": 1000, "center": 1200, "max": 60000, "deadzone": 400},
            {"type": "ANALOG", "input": "A4", "axis": "THROTTLE", "filter": 9, "hysteresis": 70000, "max": 70000},
        ], axes=[{"id": "AX1", "output": 1}, {"id": "THROTTLE", "output": 2}, {"id": "STEER", "output": 3}]))
        self.assertIn("rules[0].max: max must be greater than min", errs)
        self.assertIn("rules[1].deadzone: deadzone requires center", errs)
        self.assertIn("rules[2].center: center +/- deadzone must lie strictly between min and max", errs)
        self.assertIn("rules[3].filter: filter must be an integer 0-8", errs)
        self.assertIn("rules[3].hysteresis: hysteresis must be an integer 0-65535", errs)
        self.assertIn("rules[3].max: max must be an integer 0-65535", errs)

    def test_curves(self):
        ok = self._errs(_analog_cfg(rules=[
            {"type": "ANALOG", "input": "A1", "axis": "THROTTLE", "curve": [[0, 0], [30000, 10000], [65535, 65535]]},
            {"type": "ANALOG", "input": "A2", "axis": "STEER", "curve": 0.5},
        ]))
        self.assertEqual(ok, [])
        errs = self._errs(_analog_cfg(rules=[
            {"type": "ANALOG", "input": "A1", "axis": "THROTTLE", "curve": [[0, 0]]},
            {"type": "ANALOG", "input": "A2", "axis": "STEER", "curve": [[0, 0], [0, 5]]},
            {"type": "ANALOG", "input": "A3", "axis": "AX1", "curve": 11},
            {"type": "ANALOG", "input": "A4", "axis": "AX1", "curve": "log"},
        ], axes=[{"id": "AX1", "output": 1}, {"id": "THROTTLE", "output": 2}, {"id": "STEER", "output": 3}]))
        self.assertIn("rules[0].curve: curve table needs 2-32 points", errs)
        self.assertIn("rules[1].curve: curve point inputs must be strictly increasing", errs)
        self.assertIn("rules[2].curve: curve exponent must be > 0 and <= 10", errs)
        self.assertIn("rules[3].curve: curve must be a number or a list of [in, out] points", errs)

    def test_threshold_rules(self):
        errs = self._errs(_analog_cfg(rules=[
            {"type": "THRESHOLD", "input": "A1", "output": "B1"},                       # neither
            {"type": "THRESHOLD", "input": "A2", "output": "B2", "above": 1, "below": 2},  # both
            {"type": "THRESHOLD", "input": "D1", "output": "B3", "above": 100},          # digital pin
            {"type": "THRESHOLD", "input": "THROTTLE", "output": "B4", "below": 100},    # axis: fine
            {"type": "THRESHOLD", "input": "A3", "output": "NOPE", "above": 100},
            {"type": "THRESHOLD", "input": "A4", "output": "B5", "above": 70000},
        ]))
        self.assertIn("rules[0].above: set exactly one of above / below", errs)
        self.assertIn("rules[1].above: set exactly one of above / below", errs)
        self.assertIn("rules[2].input: pin 'D1' is not analog capable on this board", errs)
        self.assertFalse(any(e.startswith("rules[3]") for e in errs), errs)
        self.assertIn("rules[4].output: Unknown output 'NOPE'", errs)
        self.assertIn("rules[5].above: threshold must be an integer 0-65535", errs)

    def test_device_reported_analog_pins_win(self):
        d = _analog_cfg(rules=[{"type": "ANALOG", "input": "A9", "axis": "THROTTLE"}])
        self.assertIn("rules[0].input: Input 'A9' is not an analog pin", self._errs(d))
        self.assertEqual(self._errs(d, pins=["A9", "D1"], analog_pins=["A9"]), [])
        # The device says the pin exists but is not ADC-capable.
        self.assertIn("rules[0].input: pin 'A9' is not analog capable on this board",
                      self._errs(d, pins=["A9", "D1"], analog_pins=[]))

    def test_too_many_analog_rules(self):
        axes = [{"id": f"AX{i}", "output": "BACKLIGHT"} for i in range(17)]
        rules = [{"type": "ANALOG", "input": "A1", "axis": f"AX{i}"} for i in range(17)]
        errs = self._errs(_analog_cfg(rules=rules, axes=axes))
        self.assertIn("rules: too many ANALOG rules (max 16)", errs)
        self.assertNotIn("rules: too many ANALOG rules (max 16)",
                         self._errs(_analog_cfg(rules=rules[:16], axes=axes)))

    def test_too_many_threshold_rules(self):
        rules = [{"type": "THRESHOLD", "input": "A1", "output": f"B{i + 1}", "above": 100} for i in range(33)]
        errs = self._errs(_analog_cfg(rules=rules))
        self.assertIn("rules: too many THRESHOLD rules (max 32)", errs)
        self.assertEqual([e for e in self._errs(_analog_cfg(rules=rules[:32])) if "too many" in e], [])

    def test_digital_only_config_unchanged(self):
        src = {"rules": [{"type": "MAP", "input": "D1", "output": "B1"},
                         {"type": "ENCODER", "inputs": ["D2", "D3"], "cw": "B2", "ccw": "B3"}]}
        self.assertEqual(Config.from_dict(src).to_dict()["rules"], src["rules"])


if __name__ == "__main__":
    unittest.main()
