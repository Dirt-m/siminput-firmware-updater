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


if __name__ == "__main__":
    unittest.main()
