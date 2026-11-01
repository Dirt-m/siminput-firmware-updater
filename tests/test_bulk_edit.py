import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.config_model import ALL_KNOWN_PINS, Rule  # noqa: E402
from siminput_updater.rule_list import (  # noqa: E402
    History, count_matches, default_offsets, diff_span, move_to_gap, nudge, pin_range,
    range_rules, remove_indices, replace_refs, rule_matches, rules_from_text, rules_to_text, shift_name,
    shifted_copies, variables_in,
)

PINS = set(ALL_KNOWN_PINS)
is_pin = PINS.__contains__


def _maps(n):
    return [Rule(type="MAP", input=f"D{i}", output=f"B{i}") for i in range(1, n + 1)]


def _ins(rules):
    return [r.input for r in rules]


class Moves(unittest.TestCase):
    def test_move_block_to_gap_keeps_order(self):
        rules, sel = move_to_gap(_maps(5), [0, 2], 5)
        self.assertEqual(_ins(rules), ["D2", "D4", "D5", "D1", "D3"])
        self.assertEqual(sel, [3, 4])

    def test_move_block_up(self):
        rules, sel = move_to_gap(_maps(5), [3, 4], 1)
        self.assertEqual(_ins(rules), ["D1", "D4", "D5", "D2", "D3"])
        self.assertEqual(sel, [1, 2])

    def test_nudge_down_and_up(self):
        rules, sel = nudge(_maps(4), [0, 1], 1)
        self.assertEqual(_ins(rules), ["D3", "D1", "D2", "D4"])
        self.assertEqual(sel, [1, 2])
        rules, sel = nudge(rules, sel, -1)
        self.assertEqual(_ins(rules), ["D1", "D2", "D3", "D4"])
        self.assertEqual(sel, [0, 1])

    def test_nudge_at_edges_is_stable(self):
        rules, sel = nudge(_maps(3), [0], -1)
        self.assertEqual((_ins(rules), sel), (["D1", "D2", "D3"], [0]))
        rules, sel = nudge(_maps(3), [2], 1)
        self.assertEqual((_ins(rules), sel), (["D1", "D2", "D3"], [2]))

    def test_remove_indices(self):
        self.assertEqual(_ins(remove_indices(_maps(4), [1, 3])), ["D1", "D3"])


class Shifting(unittest.TestCase):
    def test_shift_name(self):
        self.assertEqual(shift_name("D19", 2), "D21")
        self.assertEqual(shift_name("TOGGLE", 2), "TOGGLE")
        self.assertEqual(shift_name("D1", -5), "D1")

    def test_encoder_block_copies(self):
        enc = Rule(type="ENCODER", inputs=["D19", "D20"], cw="B19", ccw="B20", axis="AX1")
        self.assertEqual(default_offsets([enc], is_pin), (2, 2))
        copies = shifted_copies([enc], 2, 2, 2, True, is_pin)
        self.assertEqual([c.inputs for c in copies], [["D21", "D22"], ["D23", "D24"]])
        self.assertEqual([(c.cw, c.ccw) for c in copies], [("B21", "B22"), ("B23", "B24")])
        self.assertEqual({c.axis for c in copies}, {"AX1"})   # shared, never renumbered

    def test_toggle_pair_numbers_variables(self):
        pair = [Rule(type="TOGGLE", input="D5", output="TOGGLE1"),
                Rule(type="MAP", input="TOGGLE1", output="B30")]
        copies = shifted_copies(pair, 1, 1, 1, True, is_pin)
        self.assertEqual([(c.input, c.output) for c in copies], [("D6", "TOGGLE2"), ("TOGGLE2", "B31")])
        self.assertEqual(variables_in(copies, is_pin), ["TOGGLE2"])
        plain = shifted_copies(pair, 1, 1, 1, False, is_pin)
        self.assertEqual(plain[0].output, "TOGGLE1")

    def test_copies_do_not_alias_the_source(self):
        src = Rule(type="NOR", inputs=["D1"], output="B1")
        copy = shifted_copies([src], 1, 1, 1, True, is_pin)[0]
        copy.inputs.append("D9")
        self.assertEqual(src.inputs, ["D1"])


class Ranges(unittest.TestCase):
    def test_pin_range_natural_order_and_reversed(self):
        pins = [f"D{i}" for i in range(1, 25)] + ["A6"]
        self.assertEqual(pin_range(pins, "D8", "D11"), ["D8", "D9", "D10", "D11"])
        self.assertEqual(pin_range(pins, "D11", "D8"), ["D8", "D9", "D10", "D11"])

    def test_pin_range_needs_one_prefix(self):
        self.assertEqual(pin_range(["D1", "A1"], "D1", "A1"), [])
        self.assertEqual(pin_range(["D1"], "x", "D1"), [])

    def test_range_rules(self):
        rules = range_rules("PULSE", ["D1", "D2"], 5)
        self.assertEqual([(r.input, r.output, r.pulse_ms) for r in rules],
                         [("D1", "B5", 100), ("D2", "B6", 100)])


class FindReplace(unittest.TestCase):
    RULES = [Rule(type="MAP", input="D1", output="B1"),
             Rule(type="MAP", input="D10", output="B10"),
             Rule(type="NOR", inputs=["D1", "D2"], output="B3"),
             Rule(type="ENCODER", inputs=["D3", "D4"], cw="B5", ccw="B6", axis="AX1")]

    def test_whole_names_never_hit_longer_ones(self):
        self.assertEqual(count_matches(self.RULES, "D1"), (2, 2))
        r = replace_refs(self.RULES[1], "D1", "D7")
        self.assertEqual(r.input, "D10")

    def test_replace_in_lists_and_axis(self):
        self.assertEqual(replace_refs(self.RULES[2], "D1", "D9").inputs, ["D9", "D2"])
        self.assertEqual(replace_refs(self.RULES[3], "AX1", "AX2").axis, "AX2")
        self.assertEqual(self.RULES[3].axis, "AX1")   # source untouched

    def test_substring_mode(self):
        self.assertEqual(count_matches(self.RULES, "D1", whole=False), (3, 3))
        self.assertEqual(replace_refs(self.RULES[1], "D1", "D2", whole=False).input, "D20")


class Filter(unittest.TestCase):
    def test_full_name_is_exact(self):
        self.assertTrue(rule_matches(Rule(type="MAP", input="D1", output="B1"), "d1"))
        self.assertFalse(rule_matches(Rule(type="MAP", input="D10", output="B10"), "D1"))
        self.assertTrue(rule_matches(Rule(type="NOR", inputs=["D3", "D1"], output="B9"), "D1"))

    def test_partial_text_and_type_label(self):
        enc = Rule(type="ENCODER", inputs=["D19", "D20"], cw="B19", ccw="B20", axis="AX1")
        self.assertTrue(rule_matches(enc, "enco", "Rotary Encoder"))
        self.assertTrue(rule_matches(Rule(type="TOGGLE", input="D5", output="TOGGLE1"), "togg"))
        self.assertFalse(rule_matches(enc, "toggle", "Rotary Encoder"))
        self.assertTrue(rule_matches(enc, ""))


class Clipboard(unittest.TestCase):
    def test_round_trip(self):
        rules = [Rule(type="MAP", input="D1", output="B1", invert=True),
                 Rule(type="ENCODER", inputs=["D2", "D3"], cw="B2", ccw="B3")]
        self.assertEqual(rules_from_text(rules_to_text(rules)), rules)

    def test_single_object_and_junk(self):
        self.assertEqual(len(rules_from_text('{"type": "MAP", "input": "D1", "output": "B1"}')), 1)
        for junk in ("D1", "", "[]", "[1, 2]", '{"note": "only a comment"}', "null"):
            self.assertIsNone(rules_from_text(junk), junk)


class UndoRedo(unittest.TestCase):
    def test_commit_undo_redo(self):
        h = History(_maps(1))
        self.assertFalse(h.commit(_maps(1)))       # no change, no step
        self.assertTrue(h.commit(_maps(2)))
        self.assertEqual(len(h.undo()), 1)
        self.assertIsNone(h.undo())
        self.assertEqual(len(h.redo()), 2)
        self.assertIsNone(h.redo())

    def test_new_commit_drops_redo(self):
        h = History(_maps(1))
        h.commit(_maps(2))
        h.undo()
        h.commit(_maps(3))
        self.assertFalse(h.can_redo)

    def test_snapshots_are_copies(self):
        rules = _maps(1)
        h = History(rules)
        rules[0].input = "D9"          # cards edit their rule in place
        h.commit(rules)
        self.assertEqual(h.undo()[0].input, "D1")


class DiffSpan(unittest.TestCase):
    def test_spans(self):
        a = _maps(5)
        self.assertEqual(diff_span(a, _maps(5)), (5, 0))
        b = _maps(5)
        b[2].output = "B9"
        self.assertEqual(diff_span(a, b), (2, 2))
        self.assertEqual(diff_span(a, a[:2] + a[3:]), (2, 2))
        dup = a[:3] + [a[2].copy()] + a[3:]
        self.assertEqual(diff_span(a, dup), (3, 2))


if __name__ == "__main__":
    unittest.main()
