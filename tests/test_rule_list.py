import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.config_model import Rule  # noqa: E402
from siminput_updater.rule_list import RuleList, next_free, rule_buttons, rule_inputs  # noqa: E402


def _maps(n):
    return [Rule(type="MAP", input=f"D{i}", output=f"B{i}") for i in range(1, n + 1)]


def _inputs(rl):
    return [r.input for r in rl.rules]


class RuleListStructure(unittest.TestCase):
    def test_copies_on_load(self):
        src = _maps(2)
        rl = RuleList(src)
        rl[0].input = "D9"
        self.assertEqual(src[0].input, "D1")

    def test_insert_clamps(self):
        rl = RuleList(_maps(2))
        rl.insert(99, Rule(type="MAP", input="D9", output="B9"))
        rl.insert(-5, Rule(type="MAP", input="D8", output="B8"))
        self.assertEqual(_inputs(rl), ["D8", "D1", "D2", "D9"])

    def test_remove_returns_rule(self):
        rl = RuleList(_maps(3))
        gone = rl.remove(1)
        self.assertEqual(gone.input, "D2")
        self.assertEqual(_inputs(rl), ["D1", "D3"])

    def test_duplicate_is_verbatim_and_independent(self):
        rl = RuleList([Rule(type="NOR", inputs=["D1", "D2"], output="B5", invert=True)])
        i = rl.duplicate(0)
        self.assertEqual(i, 1)
        self.assertEqual(rl[1].to_dict(), rl[0].to_dict())
        rl[1].inputs.append("D3")
        self.assertEqual(rl[0].inputs, ["D1", "D2"])

    def test_move_down_and_up(self):
        rl = RuleList(_maps(5))
        self.assertEqual(rl.move(0, 3), 3)
        self.assertEqual(_inputs(rl), ["D2", "D3", "D4", "D1", "D5"])
        self.assertEqual(rl.move(3, 0), 0)
        self.assertEqual(_inputs(rl), ["D1", "D2", "D3", "D4", "D5"])

    def test_move_clamps(self):
        rl = RuleList(_maps(3))
        self.assertEqual(rl.move(0, 99), 2)
        self.assertEqual(_inputs(rl), ["D2", "D3", "D1"])

    def test_move_to_insertion_gap_semantics(self):
        rl = RuleList(_maps(5))
        # Dropping into the gap right before or after itself is a no-op.
        self.assertEqual(rl.move_to_insertion(2, 2), 2)
        self.assertEqual(rl.move_to_insertion(2, 3), 2)
        self.assertEqual(_inputs(rl), ["D1", "D2", "D3", "D4", "D5"])
        # Gap after the last card: ends up last.
        self.assertEqual(rl.move_to_insertion(0, 5), 4)
        self.assertEqual(_inputs(rl), ["D2", "D3", "D4", "D5", "D1"])
        # Gap before the first card: ends up first.
        self.assertEqual(rl.move_to_insertion(4, 0), 0)
        self.assertEqual(_inputs(rl), ["D1", "D2", "D3", "D4", "D5"])
        # Down past one card.
        self.assertEqual(rl.move_to_insertion(1, 3), 2)
        self.assertEqual(_inputs(rl), ["D1", "D3", "D2", "D4", "D5"])


class NextFree(unittest.TestCase):
    def test_cyclic_from_after(self):
        self.assertEqual(next_free(["a", "b", "c"], {"c"}, after="b"), "a")
        self.assertEqual(next_free(["a", "b", "c"], {"a", "c"}, after="b"), "b")

    def test_unknown_after_starts_at_beginning(self):
        self.assertEqual(next_free(["a", "b"], set(), after="zz"), "a")

    def test_exhausted(self):
        self.assertIsNone(next_free(["a"], {"a"}))
        self.assertIsNone(next_free([], set()))


class NewRuleDefaults(unittest.TestCase):
    PINS = ["D1", "D2", "D3", "A1"]

    def test_empty_list_starts_at_first_pin_and_button(self):
        r = RuleList().new_rule(self.PINS)
        self.assertEqual((r.type, r.input, r.output), ("MAP", "D1", "B1"))

    def test_continues_from_last_rule(self):
        rl = RuleList([Rule(type="MAP", input="D2", output="B7")])
        r = rl.new_rule(self.PINS)
        self.assertEqual((r.input, r.output), ("D3", "B8"))

    def test_skips_used_refs_including_encoder_and_nor(self):
        rl = RuleList([
            Rule(type="ENCODER", inputs=["D1", "D2"], cw="B1", ccw="B2"),
            Rule(type="NOR", inputs=["D3"], output="B3"),
        ])
        r = rl.new_rule(self.PINS)
        self.assertEqual((r.input, r.output), ("A1", "B4"))

    def test_wraps_around(self):
        rl = RuleList([Rule(type="MAP", input="A1", output="B3")])
        r = rl.new_rule(self.PINS)
        self.assertEqual((r.input, r.output), ("D1", "B4"))

    def test_pins_sorted_naturally(self):
        rl = RuleList([Rule(type="MAP", input="D9", output="B1")])
        r = rl.new_rule(["D10", "D9", "D2"])
        self.assertEqual(r.input, "D10")

    def test_comment_rules_ignored(self):
        rl = RuleList([Rule(type="MAP", input="D1", output="B1"), Rule(comment=True, raw={"note": "x"})])
        r = rl.new_rule(self.PINS)
        self.assertEqual((r.input, r.output), ("D2", "B2"))

    def test_falls_back_to_known_pins(self):
        r = RuleList().new_rule(None)
        self.assertEqual(r.input, "D1")  # digital pins first, then analog

    def test_input_pin_fixes_the_input(self):
        rl = RuleList([Rule(type="MAP", input="D1", output="B1")])
        r = rl.new_rule(self.PINS, input_pin="A1")
        self.assertEqual((r.input, r.output), ("A1", "B2"))


class Usage(unittest.TestCase):
    """What the Rules side panel paints as used: pins read, buttons driven."""

    RULES = [
        Rule(type="MAP", input="D1", output="B1"),
        Rule(type="ENCODER", inputs=["D2", "D3"], cw="B5", ccw="B6"),
        Rule(type="TOGGLE", input="D1", output="T1"),
        Rule(type="MAP", input="T1", output="B0"),
        Rule(type="NOR", inputs=["D2", ""], output="B128"),
        Rule(comment=True, raw={"note": "x"}),
    ]

    def test_rule_inputs_and_buttons(self):
        enc = self.RULES[1]
        self.assertEqual(rule_inputs(enc), {"D2", "D3"})
        self.assertEqual(rule_buttons(enc), {5, 6})
        self.assertEqual(rule_inputs(self.RULES[4]), {"D2"})  # blank entries dropped

    def test_buttons_outside_1_to_127_ignored(self):
        self.assertEqual(rule_buttons(self.RULES[3]), set())   # B0 is never driven
        self.assertEqual(rule_buttons(self.RULES[4]), set())   # B128 does not exist

    def test_comments_use_nothing(self):
        self.assertEqual(rule_inputs(self.RULES[5]), set())
        self.assertEqual(rule_buttons(self.RULES[5]), set())

    def test_usage_maps_to_rule_indices(self):
        rl = RuleList(self.RULES)
        self.assertEqual(rl.pin_usage(), {"D1": [0, 2], "D2": [1, 4], "D3": [1], "T1": [3]})
        self.assertEqual(rl.button_usage(), {1: [0], 5: [1], 6: [1]})


if __name__ == "__main__":
    unittest.main()
