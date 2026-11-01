import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.rule_list import can_create_variable, rank_suggestions, ref_candidates  # noqa: E402

PINS = ["D10", "D2", "D1", "A1"]


class Candidates(unittest.TestCase):
    def test_inputs_are_pins_then_variables_then_buttons(self):
        c = ref_candidates("in", PINS, {"D1": [0, 4]}, {}, ["TOGGLE1"])
        self.assertEqual([n for n, _ in c[:5]], ["D1", "D2", "D10", "A1", "TOGGLE1"])
        self.assertEqual(c[0][1], "rules 1, 5")
        self.assertEqual(c[1][1], "free")
        self.assertEqual(len(c), 4 + 1 + 127)

    def test_outputs_are_buttons_variables_refresh(self):
        c = ref_candidates("out", PINS, {}, {3: [1]}, ["T1"])
        self.assertEqual(c[2], ("B3", "rule 2"))
        self.assertEqual([n for n, _ in c[-2:]], ["T1", "REFRESH"])


class Ranking(unittest.TestCase):
    C = [("D1", "rule 1"), ("D2", "free"), ("D10", "free"), ("AD1", "free"), ("TOGGLE1", "variable")]

    def test_prefix_before_substring(self):
        self.assertEqual([n for n, _ in rank_suggestions("d1", self.C)], ["D1", "D10", "AD1"])

    def test_empty_text_puts_free_first(self):
        self.assertEqual([n for n, _ in rank_suggestions("", self.C)][:3], ["D2", "D10", "AD1"])

    def test_limit(self):
        self.assertEqual(len(rank_suggestions("", self.C, limit=2)), 2)


class NewVariable(unittest.TestCase):
    def test_rules(self):
        taken = {"D1", "TOGGLE1", "AX1"}
        self.assertTrue(can_create_variable("TOGGLE2", taken))
        for bad in ("", "TOGGLE1", "D1", "AX1", "B12", "REFRESH", "has space", "1ABC"):
            self.assertFalse(can_create_variable(bad, taken), bad)


if __name__ == "__main__":
    unittest.main()
