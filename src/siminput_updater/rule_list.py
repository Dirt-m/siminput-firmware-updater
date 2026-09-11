"""Ordered rule list with the structural operations the editor exposes.

Pure data: no UI or serial imports. The editor keeps one of these as the
source of truth and mirrors it with widgets, so every operation here is
cheap and independently testable.
"""

from __future__ import annotations

import re

from .config_model import ALL_KNOWN_PINS, Rule, _pin_sort_key

BUTTON_REFS = [f"B{i}" for i in range(1, 128)]
_B_PATTERN = re.compile(r"^B(\d+)$")


class RuleList:
    def __init__(self, rules: list[Rule] | None = None):
        self.rules: list[Rule] = [r.copy() for r in (rules or [])]

    def __len__(self) -> int:
        return len(self.rules)

    def __getitem__(self, i: int) -> Rule:
        return self.rules[i]

    def insert(self, index: int, rule: Rule) -> int:
        index = max(0, min(index, len(self.rules)))
        self.rules.insert(index, rule)
        return index

    def append(self, rule: Rule) -> int:
        return self.insert(len(self.rules), rule)

    def remove(self, index: int) -> Rule:
        return self.rules.pop(index)

    def duplicate(self, index: int) -> int:
        """Insert a verbatim copy directly below `index`; returns its index."""
        return self.insert(index + 1, self.rules[index].copy())

    def move(self, src: int, dst: int) -> int:
        """Move the rule at `src` so it ends up at index `dst` in the result.
        Returns the final index (clamped)."""
        rule = self.rules.pop(src)
        dst = max(0, min(dst, len(self.rules)))
        self.rules.insert(dst, rule)
        return dst

    def move_to_insertion(self, src: int, insert_at: int) -> int:
        """Drag semantics: `insert_at` is a gap index in the *current* list
        (0 = before the first card, len = after the last). Returns the final
        index, or src if nothing moved."""
        if insert_at in (src, src + 1):
            return src
        dst = insert_at - 1 if insert_at > src else insert_at
        return self.move(src, dst)

    # ---------------------------------------------------------- new rules

    def used_inputs(self) -> set[str]:
        used: set[str] = set()
        for r in self.rules:
            if r.comment:
                continue
            if r.input:
                used.add(r.input)
            used.update(x for x in r.inputs if x)
        return used

    def used_outputs(self) -> set[str]:
        used: set[str] = set()
        for r in self.rules:
            if r.comment:
                continue
            for ref in (r.output, r.cw, r.ccw):
                if ref:
                    used.add(ref)
        return used

    def new_rule(self, pins: list[str] | None = None) -> Rule:
        """A Direct Map on the next unused pin and button, continuing from the
        last rule so a run of Add clicks walks D1→B1, D2→B2, … without edits."""
        pins = sorted(pins or ALL_KNOWN_PINS, key=_pin_order)
        last = next((r for r in reversed(self.rules) if not r.comment), None)
        last_input = last.input if last and last.input in pins else None
        last_output = None
        if last:
            for ref in (last.output, last.cw, last.ccw):
                if ref and _B_PATTERN.match(ref):
                    last_output = ref
        inp = next_free(pins, self.used_inputs(), after=last_input) or ""
        out = next_free(BUTTON_REFS, self.used_outputs(), after=last_output) or ""
        return Rule(type="MAP", input=inp, output=out)


def _pin_order(p: str):
    """Digital pins first (that's where switches live), then the rest, each
    group in natural numeric order."""
    kind, letters, num = _pin_sort_key(p)
    return (kind, 0 if letters.upper() == "D" else 1, letters, num)


def next_free(candidates: list[str], used: set[str], after: str | None = None) -> str | None:
    """First candidate not in `used`, searching cyclically from just past
    `after` (or from the start when `after` is unknown)."""
    if not candidates:
        return None
    start = 0
    if after in candidates:
        start = candidates.index(after) + 1
    n = len(candidates)
    for k in range(n):
        c = candidates[(start + k) % n]
        if c not in used:
            return c
    return None
