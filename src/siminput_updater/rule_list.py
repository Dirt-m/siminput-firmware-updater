"""Ordered rule list with the structural operations the editor exposes.

Pure data: no UI or serial imports. The editor keeps one of these as the
source of truth and mirrors it with widgets, so every operation here is
cheap and independently testable.
"""

from __future__ import annotations

import json
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

    def pin_usage(self) -> dict[str, list[int]]:
        """Each name a rule reads → indices of the rules reading it."""
        usage: dict[str, list[int]] = {}
        for i, r in enumerate(self.rules):
            for name in rule_inputs(r):
                usage.setdefault(name, []).append(i)
        return usage

    def button_usage(self) -> dict[int, list[int]]:
        """HID button number → indices of the rules that drive it."""
        usage: dict[int, list[int]] = {}
        for i, r in enumerate(self.rules):
            for n in rule_buttons(r):
                usage.setdefault(n, []).append(i)
        return usage

    def new_rule(self, pins: list[str] | None = None, input_pin: str | None = None) -> Rule:
        """A Direct Map on the next unused pin and button, continuing from the
        last rule so a run of Add clicks walks D1→B1, D2→B2, … without edits.
        `input_pin` fixes the input instead (the side panel's click on a free pin)."""
        pins = sorted(pins or ALL_KNOWN_PINS, key=pin_order)
        last = next((r for r in reversed(self.rules) if not r.comment), None)
        last_input = last.input if last and last.input in pins else None
        last_output = None
        if last:
            for ref in (last.output, last.cw, last.ccw):
                if ref and _B_PATTERN.match(ref):
                    last_output = ref
        inp = input_pin or next_free(pins, self.used_inputs(), after=last_input) or ""
        out = next_free(BUTTON_REFS, self.used_outputs(), after=last_output) or ""
        return Rule(type="MAP", input=inp, output=out)


def rule_inputs(rule: Rule) -> set[str]:
    """Names a rule reads: pins, and also variables or outputs of earlier
    rules (callers that want pins intersect with the board's pin list)."""
    if rule.comment:
        return set()
    names = {x for x in rule.inputs if x}
    if rule.input:
        names.add(rule.input)
    return names


def rule_buttons(rule: Rule) -> set[int]:
    """HID buttons (1-127) a rule drives through output, cw or ccw."""
    if rule.comment:
        return set()
    found = set()
    for ref in (rule.output, rule.cw, rule.ccw):
        m = _B_PATTERN.match(ref or "")
        if m and 1 <= int(m.group(1)) <= 127:
            found.add(int(m.group(1)))
    return found


def pin_order(p: str):
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


# ------------------------------------------------------------- bulk edits
#
# Each takes the current rules and returns a new list (plus the indices to
# select afterwards); the editor applies the result with one reconcile, so
# every one of them is a single undo step.

_NUMBERED = re.compile(r"^(.*?)(\d+)$")
REF_FIELDS = ("input", "output", "cw", "ccw")   # plus every entry of `inputs`


def remove_indices(rules: list[Rule], indices) -> list[Rule]:
    drop = set(indices)
    return [r for i, r in enumerate(rules) if i not in drop]


def insert_rules(rules: list[Rule], at: int, new: list[Rule]) -> tuple[list[Rule], list[int]]:
    at = max(0, min(at, len(rules)))
    return rules[:at] + new + rules[at:], list(range(at, at + len(new)))


def move_to_gap(rules: list[Rule], indices, gap: int) -> tuple[list[Rule], list[int]]:
    """Move the rules at `indices` (kept in their order) to insertion gap
    `gap` of the current list: 0 = top, len = bottom."""
    picked = sorted(set(indices))
    block = [rules[i] for i in picked]
    gap -= sum(1 for i in picked if i < gap)
    rest = remove_indices(rules, picked)
    return insert_rules(rest, gap, block)


def nudge(rules: list[Rule], indices, delta: int) -> tuple[list[Rule], list[int]]:
    """Alt+Up/Down for a selection: the block moves one step past its
    neighbour, gaps inside the selection close up."""
    picked = sorted(set(indices))
    if not picked:
        return list(rules), []
    if delta < 0:
        gap = max(0, picked[0] - 1)
    else:
        gap = min(len(rules), picked[-1] + 2)
    return move_to_gap(rules, picked, gap)


def shift_name(name: str, offset: int) -> str:
    """'D19' + 2 → 'D21'. Names without a trailing number are unchanged; a
    result below zero keeps the original."""
    m = _NUMBERED.match(name or "")
    if not m or not offset:
        return name
    n = int(m.group(2)) + offset
    return f"{m.group(1)}{n}" if n >= 0 else name


def shift_rule(rule: Rule, pin_offset: int, button_offset: int, var_offset: int,
               is_pin) -> Rule:
    """A copy with pins, buttons (B1-B127) and variable names renumbered.
    `is_pin(name)` tells a pin from a variable. The axis is never touched:
    copies of an encoder share its axis."""
    r = rule.copy()
    if r.comment:
        return r

    def shift(ref: str) -> str:
        if not ref:
            return ref
        if _B_PATTERN.match(ref):
            return shift_name(ref, button_offset)
        if is_pin(ref):
            return shift_name(ref, pin_offset)
        return shift_name(ref, var_offset)

    for f in REF_FIELDS:
        setattr(r, f, shift(getattr(r, f)))
    r.inputs = [shift(x) for x in r.inputs]
    return r


def default_offsets(rules: list[Rule], is_pin) -> tuple[int, int]:
    """How far the next copy of a block should sit: the span of pin numbers
    and button numbers it uses, so D19/D20 → B19/B20 suggests 2 and 2."""
    def span(nums):
        return max(nums) - min(nums) + 1 if nums else 0
    pins, buttons = [], []
    for r in rules:
        for name in rule_inputs(r):
            m = _NUMBERED.match(name)
            if m and is_pin(name):
                pins.append(int(m.group(2)))
        buttons.extend(rule_buttons(r))
    return span(pins), span(buttons)


def shifted_copies(rules: list[Rule], copies: int, pin_offset: int, button_offset: int,
                   number_vars: bool, is_pin) -> list[Rule]:
    """`copies` copies of the block, the k-th shifted k times."""
    out = []
    for k in range(1, copies + 1):
        out += [shift_rule(r, k * pin_offset, k * button_offset, k if number_vars else 0, is_pin)
                for r in rules]
    return out


def variables_in(rules: list[Rule], is_pin) -> list[str]:
    """Names the rules use that are neither pins nor buttons, in first-use order."""
    seen: dict[str, None] = {}
    for r in rules:
        if r.comment:
            continue
        for ref in [r.input, *r.inputs, r.output, r.cw, r.ccw]:
            if ref and not _B_PATTERN.match(ref) and not is_pin(ref) and ref != "REFRESH":
                seen.setdefault(ref)
    return list(seen)


def pin_range(pins: list[str], first: str, last: str) -> list[str]:
    """Board pins from `first` to `last` inclusive, in pin order. Both must
    share a prefix (D3-D9, A0-A3); either order works."""
    a, b = _NUMBERED.match(first.strip()), _NUMBERED.match(last.strip())
    if not a or not b or a.group(1).upper() != b.group(1).upper():
        return []
    lo, hi = sorted((int(a.group(2)), int(b.group(2))))
    prefix = a.group(1).upper()
    found = []
    for p in sorted(pins, key=pin_order):
        m = _NUMBERED.match(p)
        if m and m.group(1).upper() == prefix and lo <= int(m.group(2)) <= hi:
            found.append(p)
    return found


def range_rules(rule_type: str, pins: list[str], first_button: int) -> list[Rule]:
    """One rule per pin, buttons counting up from `first_button`."""
    out = []
    for k, p in enumerate(pins):
        r = Rule(type=rule_type, input=p, output=f"B{first_button + k}")
        if rule_type == "PULSE":
            r.pulse_ms = 100
        out.append(r)
    return out


def _match(ref: str, find: str, whole: bool) -> bool:
    return ref == find if whole else find in ref


def count_matches(rules: list[Rule], find: str, whole: bool = True) -> tuple[int, int]:
    """(fields, rules) that Replace would change."""
    if not find:
        return 0, 0
    fields = touched = 0
    for r in rules:
        if r.comment:
            continue
        n = sum(1 for f in REF_FIELDS if getattr(r, f) and _match(getattr(r, f), find, whole))
        n += sum(1 for x in r.inputs if x and _match(x, find, whole))
        n += 1 if r.axis and _match(r.axis, find, whole) else 0
        fields += n
        touched += 1 if n else 0
    return fields, touched


def replace_refs(rule: Rule, find: str, repl: str, whole: bool = True) -> Rule:
    """A copy with every pin, button, variable and axis name matching `find`
    replaced. Whole-name matching by default, so D1 never hits D10."""
    r = rule.copy()
    if r.comment or not find:
        return r

    def sub(ref: str) -> str:
        if not ref or not _match(ref, find, whole):
            return ref
        return repl if whole else ref.replace(find, repl)

    for f in (*REF_FIELDS, "axis"):
        setattr(r, f, sub(getattr(r, f)))
    r.inputs = [sub(x) for x in r.inputs]
    return r


# -------------------------------------------------------------- clipboard

def rules_to_text(rules: list[Rule]) -> str:
    """The clipboard form: the same JSON list a config's "rules" holds, so
    rules paste between boxes, configs and a text editor."""
    return json.dumps([r.to_dict() for r in rules], indent=2)


def rules_from_text(text: str) -> list[Rule] | None:
    """Rules from clipboard text (a list of rule objects or one object);
    None when the text is not rules."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        data = [data]
    # Typeless objects are comments; at least one real rule must be there.
    if (not isinstance(data, list) or not all(isinstance(d, dict) for d in data)
            or not any(d.get("type") for d in data)):
        return None
    return [Rule.from_dict(d) for d in data]


# ---------------------------------------------------------------- history

HISTORY_LIMIT = 200


class History:
    """Undo/redo as whole-list snapshots. A rule list is small (well under
    the firmware's 32 KB config cap), so snapshots beat per-edit inverses:
    every operation is undoable by construction."""

    def __init__(self, rules: list[Rule] | None = None):
        self.reset(rules or [])

    def reset(self, rules: list[Rule]):
        self._states = [[r.copy() for r in rules]]
        self._pos = 0

    def commit(self, rules: list[Rule]) -> bool:
        """Record `rules` as a new state; False when nothing changed."""
        if rules == self._states[self._pos]:
            return False
        del self._states[self._pos + 1:]
        self._states.append([r.copy() for r in rules])
        if len(self._states) > HISTORY_LIMIT:
            del self._states[0]
        self._pos = len(self._states) - 1
        return True

    @property
    def can_undo(self) -> bool:
        return self._pos > 0

    @property
    def can_redo(self) -> bool:
        return self._pos < len(self._states) - 1

    def undo(self) -> list[Rule] | None:
        if not self.can_undo:
            return None
        self._pos -= 1
        return [r.copy() for r in self._states[self._pos]]

    def redo(self) -> list[Rule] | None:
        if not self.can_redo:
            return None
        self._pos += 1
        return [r.copy() for r in self._states[self._pos]]


def diff_span(old: list[Rule], new: list[Rule]) -> tuple[int, int]:
    """(prefix, suffix): how many rules at each end are unchanged, never
    overlapping. Only the middle needs new cards."""
    n = min(len(old), len(new))
    pre = 0
    while pre < n and old[pre] == new[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and old[-1 - suf] == new[-1 - suf]:
        suf += 1
    return pre, suf
