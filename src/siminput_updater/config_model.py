from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

BOARD_PINS: dict[str, list[str]] = {
    "rev1": [f"D{i}" for i in range(1, 25)] + ["A6", "A7", "A8"],
    "rev2": [f"D{i}" for i in range(1, 39)] + ["A1", "A2", "A3", "A4"],
}


def _pin_sort_key(p: str):
    # Tolerant of any naming a future board revision might use — a plain
    # letter+digits assumption here would crash at import time on e.g. "GP10".
    m = re.match(r"^([A-Za-z]+)(\d+)$", p)
    if m:
        return (0, m.group(1), int(m.group(2)))
    return (1, p, 0)


ALL_KNOWN_PINS: list[str] = sorted(
    {p for pins in BOARD_PINS.values() for p in pins},
    key=_pin_sort_key,
)


def pins_for_board(board_map: str) -> list[str]:
    return BOARD_PINS.get(board_map, ALL_KNOWN_PINS)


# ADC-capable pins per board (the RP2040's GP26-29). Firmware 2.7+ reports
# the real list as `analog_pins` in get_info; this table is the offline
# fallback, so a future board validates correctly once the device is plugged in.
BOARD_ANALOG_PINS: dict[str, list[str]] = {
    "rev1": ["A6", "A7", "A8"],
    "rev2": ["A1", "A2", "A3", "A4"],
}

ALL_KNOWN_ANALOG_PINS: list[str] = sorted(
    {p for pins in BOARD_ANALOG_PINS.values() for p in pins}, key=_pin_sort_key)


def analog_pins_for_board(board_map: str) -> list[str]:
    return BOARD_ANALOG_PINS.get(board_map, ALL_KNOWN_ANALOG_PINS)


ANALOG_RULE_TYPES = ("ANALOG", "THRESHOLD")
ANALOG_MAX = 65535

AXIS_SLOT_LABELS = {
    1: "X", 2: "Y", 3: "Z",
    4: "Rx", 5: "Ry", 6: "Rz",
    7: "Slider", 8: "Dial",
    "BACKLIGHT": "Backlight Only",
}

RULE_TYPE_LABELS = {
    "MAP": "Direct Map",
    "NOR": "All-Off Detector",
    "TOGGLE": "Toggle Switch",
    "PULSE": "Timed Pulse",
    "ENCODER": "Rotary Encoder",
    "AXIS_INC": "Increase Axis",
    "AXIS_DEC": "Decrease Axis",
    "ANALOG": "Analog Axis",
    "THRESHOLD": "Analog Threshold",
}

MAX_CONFIG_BYTES = 32768  # firmware _MAX_CONFIG — larger configs are rejected on save


def _split_extra(d: dict, known: set[str]) -> dict:
    """Keys this model doesn't understand — carried through save untouched, so
    a config written by newer firmware survives a round trip through the
    editor instead of being silently stripped."""
    return {k: v for k, v in d.items() if k not in known}


@dataclass
class ValidationError:
    path: str
    message: str

    def __str__(self):
        return f"{self.path}: {self.message}"


@dataclass
class DeviceSettings:
    name: str = "SimInput Button Box"
    pid: int = 0xF000
    debounce_ms: int | float = 10
    inactivity_refresh: float | bool | None = 1.0
    extra: dict = field(default_factory=dict)

    _KNOWN = {"name", "pid", "debounce_ms", "inactivity_refresh"}

    def to_dict(self) -> dict:
        d: dict[str, Any] = dict(self.extra)
        d["name"] = self.name
        d["pid"] = self.pid
        if self.debounce_ms != 10:
            d["debounce_ms"] = self.debounce_ms
        if self.inactivity_refresh is not True:
            d["inactivity_refresh"] = self.inactivity_refresh
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DeviceSettings:
        return cls(
            name=d.get("name", "SimInput Button Box"),
            pid=d.get("pid", 0xF000),
            debounce_ms=d.get("debounce_ms", 10),
            inactivity_refresh=d.get("inactivity_refresh", 1.0),
            extra=_split_extra(d, cls._KNOWN),
        )


@dataclass
class BoolVar:
    id: str = ""
    default: bool = False
    store: bool = False
    comment: bool = False   # an id-less entry the firmware skips — kept verbatim
    raw: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    _KNOWN = {"id", "default", "store"}

    def to_dict(self) -> dict:
        if self.comment:
            return dict(self.raw)
        d: dict[str, Any] = dict(self.extra)
        d["id"] = self.id
        d["default"] = self.default
        if self.store:
            d["store"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BoolVar:
        if "id" not in d:
            return cls(comment=True, raw=dict(d))
        return cls(id=d.get("id", ""), default=d.get("default", False),
                   store=d.get("store", False), extra=_split_extra(d, cls._KNOWN))


@dataclass
class Axis:
    id: str = ""
    output: int | str = 1
    default: int = 32767
    store: bool = False
    backlight: bool = False
    comment: bool = False
    raw: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    _KNOWN = {"id", "output", "default", "store", "backlight"}

    def to_dict(self) -> dict:
        if self.comment:
            return dict(self.raw)
        d: dict[str, Any] = dict(self.extra)
        d["id"] = self.id
        d["output"] = self.output
        d["default"] = self.default
        if self.store:
            d["store"] = True
        if self.backlight:
            d["backlight"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Axis:
        if "id" not in d:
            return cls(comment=True, raw=dict(d))
        output = d.get("output", 1)
        # The firmware matches "BACKLIGHT" case-insensitively — normalise here
        # so the rest of the app only sees the canonical spelling.
        if isinstance(output, str) and output.upper() == "BACKLIGHT":
            output = "BACKLIGHT"
        return cls(
            id=d.get("id", ""),
            output=output,
            default=d.get("default", 32767),
            store=d.get("store", False),
            backlight=d.get("backlight", False),
            extra=_split_extra(d, cls._KNOWN),
        )


@dataclass
class Rule:
    type: str = "MAP"
    input: str = ""
    inputs: list[str] = field(default_factory=list)
    output: str = ""
    cw: str = ""
    ccw: str = ""
    axis: str = ""
    invert: bool = False
    # 0, not 100: the firmware's ENCODER default is 0 (single-cycle pulse).
    # A 100 default here made UI-built encoder rules hold outputs for 100 ms
    # while identical hand-written configs did not. PULSE rules get 100 set
    # explicitly (from_dict below, and the editor on type switch).
    pulse_ms: int = 0
    delay_ms: int = 0
    step: int = 1
    divisor: int = 2
    # ANALOG / THRESHOLD (firmware 2.7+). None means "not set": the firmware
    # default applies and the key stays out of to_dict().
    min: int | None = None
    max: int | None = None
    center: int | None = None
    deadzone: int | None = None
    filter: int | None = None
    hysteresis: int | None = None
    curve: float | int | list | None = None
    above: int | None = None
    below: int | None = None
    comment: bool = False   # a type-less entry the firmware skips — kept verbatim
    raw: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    _KNOWN = {"type", "input", "inputs", "output", "cw", "ccw", "axis",
              "invert", "pulse_ms", "delay_ms", "step", "divisor",
              "min", "max", "center", "deadzone", "filter", "hysteresis",
              "curve", "above", "below"}
    _ANALOG_OPTIONAL = ("min", "max", "center", "deadzone", "filter", "hysteresis", "curve")

    def to_dict(self) -> dict:
        if self.comment:
            return dict(self.raw)
        d: dict[str, Any] = dict(self.extra)
        d["type"] = self.type
        if self.type == "MAP":
            d["input"] = self.input
            d["output"] = self.output
            if self.invert:
                d["invert"] = True
        elif self.type == "NOR":
            d["inputs"] = list(self.inputs)
            d["output"] = self.output
            if self.invert:
                d["invert"] = True
        elif self.type == "TOGGLE":
            d["input"] = self.input
            d["output"] = self.output
            if self.invert:
                d["invert"] = True
        elif self.type == "PULSE":
            d["input"] = self.input
            d["output"] = self.output
            if self.delay_ms:
                d["delay_ms"] = self.delay_ms
            d["pulse_ms"] = self.pulse_ms
            if self.invert:
                d["invert"] = True
        elif self.type == "ENCODER":
            d["inputs"] = list(self.inputs)
            if self.cw:
                d["cw"] = self.cw
            if self.ccw:
                d["ccw"] = self.ccw
            if self.pulse_ms:
                d["pulse_ms"] = self.pulse_ms
            if self.divisor != 2:
                d["divisor"] = self.divisor
            if self.invert:
                d["invert"] = True
        elif self.type in ("AXIS_INC", "AXIS_DEC"):
            d["input"] = self.input
            d["axis"] = self.axis
            if self.step != 1:
                d["step"] = self.step
        elif self.type == "ANALOG":
            d["input"] = self.input
            d["axis"] = self.axis
            for key in self._ANALOG_OPTIONAL:
                val = getattr(self, key)
                if val is not None:
                    d[key] = list(val) if isinstance(val, list) else val
            if self.invert:
                d["invert"] = True
        elif self.type == "THRESHOLD":
            d["input"] = self.input
            d["output"] = self.output
            if self.above is not None:
                d["above"] = self.above
            if self.below is not None:
                d["below"] = self.below
            if self.hysteresis is not None:
                d["hysteresis"] = self.hysteresis
            if self.invert:
                d["invert"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Rule:
        # Tolerate missing keys — validate() reports the problems with paths,
        # which beats a bare KeyError surfacing in the UI.
        tp = d.get("type", "")
        if not tp:
            return cls(comment=True, raw=dict(d))
        return cls(
            type=tp,
            input=d.get("input", ""),
            inputs=d.get("inputs", []),
            output=d.get("output", ""),
            cw=d.get("cw", ""),
            ccw=d.get("ccw", ""),
            axis=d.get("axis", ""),
            invert=d.get("invert", False),
            pulse_ms=d.get("pulse_ms", 100 if tp == "PULSE" else 0),
            delay_ms=d.get("delay_ms", 0),
            step=d.get("step", 1),
            divisor=d.get("divisor", 2),
            min=d.get("min"), max=d.get("max"), center=d.get("center"),
            deadzone=d.get("deadzone"), filter=d.get("filter"),
            hysteresis=d.get("hysteresis"), curve=d.get("curve"),
            above=d.get("above"), below=d.get("below"),
            extra=_split_extra(d, cls._KNOWN),
        )

    def copy(self) -> Rule:
        c = Rule(**{f: getattr(self, f) for f in (
            "type", "input", "output", "cw", "ccw", "axis", "invert",
            "pulse_ms", "delay_ms", "step", "divisor", "comment",
            "min", "max", "center", "deadzone", "filter", "hysteresis",
            "above", "below")})
        c.inputs = list(self.inputs)
        c.curve = list(self.curve) if isinstance(self.curve, list) else self.curve
        c.raw = dict(self.raw)
        c.extra = dict(self.extra)
        return c

    @property
    def is_analog(self) -> bool:
        return not self.comment and self.type in ANALOG_RULE_TYPES

    def summary(self) -> str:
        if self.comment:
            for v in self.raw.values():
                if isinstance(v, str) and v:
                    return v
            return "(comment)"
        t = self.type
        inv = " (inverted)" if self.invert else ""
        if t == "MAP":
            return f"{self.input} → {_fmt_output(self.output)}{inv}"
        if t == "NOR":
            inputs = " / ".join(self.inputs) if self.inputs else "?"
            return f"{_fmt_output(self.output)} active when none of [{inputs}] pressed{inv}"
        if t == "TOGGLE":
            return f"{self.input} toggles {self.output}{inv}"
        if t == "PULSE":
            delay = f" after {self.delay_ms}ms" if self.delay_ms else ""
            return f"{self.input} pulses {_fmt_output(self.output)} for {self.pulse_ms}ms{delay}{inv}"
        if t == "ENCODER":
            pins = "/".join(self.inputs) if self.inputs else "?"
            parts = []
            if self.cw:
                parts.append(f"CW: {_fmt_output(self.cw)}")
            if self.ccw:
                parts.append(f"CCW: {_fmt_output(self.ccw)}")
            return f"Encoder {pins} → {', '.join(parts) or 'no outputs'}"
        if t == "AXIS_INC":
            return f"{self.input} increases {self.axis} by {self.step}"
        if t == "AXIS_DEC":
            return f"{self.input} decreases {self.axis} by {self.step}"
        if t == "ANALOG":
            lo = 0 if self.min is None else self.min
            hi = ANALOG_MAX if self.max is None else self.max
            rng = f"{lo}–{hi}" if self.center is None else f"{lo}–{self.center}–{hi}"
            return f"{self.input or '?'} drives {self.axis or '?'} over {rng}{inv}"
        if t == "THRESHOLD":
            if self.above is not None:
                cond = f"above {self.above}"
            elif self.below is not None:
                cond = f"below {self.below}"
            else:
                cond = "?"
            return f"{_fmt_output(self.output)} when {self.input or '?'} is {cond}{inv}"
        return f"{t}: ?"


def _fmt_output(ref: str) -> str:
    m = re.match(r"^B(\d+)$", ref)
    if m:
        return f"Button {m.group(1)}"
    return ref


@dataclass
class Config:
    device: DeviceSettings = field(default_factory=DeviceSettings)
    bools: list[BoolVar] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    _KNOWN = {"device", "bools", "axes", "rules"}

    def to_dict(self) -> dict:
        d: dict[str, Any] = dict(self.extra)
        d["device"] = self.device.to_dict()
        d["bools"] = [b.to_dict() for b in self.bools]
        d["axes"] = [a.to_dict() for a in self.axes]
        d["rules"] = [r.to_dict() for r in self.rules]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        return cls(
            device=DeviceSettings.from_dict(d.get("device", {})),
            bools=[BoolVar.from_dict(b) for b in d.get("bools", []) if isinstance(b, dict)],
            axes=[Axis.from_dict(a) for a in d.get("axes", []) if isinstance(a, dict)],
            rules=[Rule.from_dict(r) for r in d.get("rules", []) if isinstance(r, dict)],
            extra=_split_extra(d, cls._KNOWN),
        )

    @classmethod
    def from_json(cls, text: str) -> Config:
        return cls.from_dict(json.loads(text))

    def get_axis_choices(self) -> list[str]:
        return [a.id for a in self.axes if a.id]

    def uses_analog(self) -> bool:
        """True when any rule needs firmware 2.7+ analog support."""
        return any(r.is_analog for r in self.rules)


_B_PATTERN = re.compile(r"^B(\d+)$")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _in_range(v) -> bool:
    return _is_int(v) and 0 <= v <= ANALOG_MAX


def _curve_error(curve) -> str | None:
    """Mirror the firmware's curve rules; None when the value is acceptable."""
    if isinstance(curve, list):
        if len(curve) < 2 or len(curve) > 32:
            return "curve table needs 2-32 points"
        last_x = -1
        for p in curve:
            if (not isinstance(p, list) or len(p) != 2
                    or not _in_range(p[0]) or not _in_range(p[1])):
                return "curve points must be [in, out] integer pairs 0-65535"
            if p[0] <= last_x:
                return "curve point inputs must be strictly increasing"
            last_x = p[0]
        return None
    if isinstance(curve, (int, float)) and not isinstance(curve, bool):
        if curve <= 0 or curve > 10:
            return "curve exponent must be > 0 and <= 10"
        return None
    return "curve must be a number or a list of [in, out] points"


def validate(config: Config, board_map: str = "",
             pins: list[str] | None = None,
             analog_pins: list[str] | None = None) -> list[ValidationError]:
    """Client-side validation, kept in lockstep with the firmware's
    validate_config. `pins` is the device-reported pin list (protocol 2);
    without it, the hardcoded per-revision table is used, falling back to the
    union of all known boards. `analog_pins` is the device-reported ADC pin
    list (firmware 2.7+); without it the per-board table applies."""
    errors: list[ValidationError] = []

    # Device settings
    if not config.device.name or not config.device.name.strip():
        errors.append(ValidationError("device.name", "Device name is required"))
    elif len(config.device.name) > 32:
        errors.append(ValidationError("device.name", "Device name must be 32 characters or fewer"))

    pid = config.device.pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or pid > 0xFFFF:
        errors.append(ValidationError("device.pid", "USB Product ID must be 0x0001-0xFFFF"))
    elif pid == 0x80F4:
        errors.append(ValidationError("device.pid", "PID 0x80F4 is reserved and cannot be used"))

    db = config.device.debounce_ms
    # int or float, like the firmware — a device-written 10.5 must not make a
    # config the device itself accepted fail to re-save.
    if isinstance(db, bool) or not isinstance(db, (int, float)) or not (0 <= db <= 100):
        errors.append(ValidationError("device.debounce_ms", "Debounce must be 0-100 ms"))

    ir = config.device.inactivity_refresh
    if ir is not False and ir is not None:  # firmware: null means disabled too
        if isinstance(ir, bool) or not isinstance(ir, (int, float)) or ir <= 0:
            errors.append(ValidationError("device.inactivity_refresh", "Keep-alive must be a positive number or disabled"))

    # Bools
    bool_ids = set()
    reserved = set(pins) if pins else set(pins_for_board(board_map))
    for i, b in enumerate(config.bools):
        if b.comment:
            continue
        path = f"bools[{i}]"
        if not b.id or not b.id.strip():
            errors.append(ValidationError(f"{path}.id", "Variable name is required"))
        elif " " in b.id:
            errors.append(ValidationError(f"{path}.id", "Variable name cannot contain spaces"))
        elif b.id in reserved:
            errors.append(ValidationError(f"{path}.id", f"'{b.id}' conflicts with a pin name"))
        elif _B_PATTERN.match(b.id):
            errors.append(ValidationError(f"{path}.id", f"'{b.id}' conflicts with button naming (B + digits)"))
        elif b.id in bool_ids:
            errors.append(ValidationError(f"{path}.id", f"Duplicate variable name '{b.id}'"))
        else:
            bool_ids.add(b.id)

    # Axes
    axis_ids = set()
    output_slots: dict[Any, int] = {}
    for i, a in enumerate(config.axes):
        if a.comment:
            continue
        path = f"axes[{i}]"
        if not a.id or not a.id.strip():
            errors.append(ValidationError(f"{path}.id", "Axis name is required"))
        elif " " in a.id:
            errors.append(ValidationError(f"{path}.id", "Axis name cannot contain spaces"))
        elif a.id in reserved:
            errors.append(ValidationError(f"{path}.id", f"'{a.id}' conflicts with a pin name"))
        elif _B_PATTERN.match(a.id):
            errors.append(ValidationError(f"{path}.id", f"'{a.id}' conflicts with button naming"))
        elif a.id in axis_ids or a.id in bool_ids:
            errors.append(ValidationError(f"{path}.id", f"Duplicate name '{a.id}'"))
        else:
            axis_ids.add(a.id)

        out = a.output
        if out != "BACKLIGHT":
            if not isinstance(out, int) or not (1 <= out <= 8):
                errors.append(ValidationError(f"{path}.output", "Output must be 1-8 or Backlight Only"))
            elif out in output_slots:
                errors.append(ValidationError(f"{path}.output", f"Axis slot {out} already used by axes[{output_slots[out]}]"))
            else:
                output_slots[out] = i

        if not isinstance(a.default, int) or not (0 <= a.default <= 65535):
            errors.append(ValidationError(f"{path}.default", "Default must be 0-65535"))

    # Rules
    valid_inputs = reserved | bool_ids | {f"B{i}" for i in range(1, 128)}
    valid_outputs = bool_ids | {f"B{i}" for i in range(1, 128)} | {"REFRESH"}
    encoder_pins: set[str] = set()

    # Analog: pins claimed by ANALOG/THRESHOLD rules have no digital level to
    # read, so they are excluded from every digital input check. Collected up
    # front because a digital rule can come before the analog one.
    analog_capable = set(analog_pins) if analog_pins is not None else set(analog_pins_for_board(board_map))
    analog_used: set[str] = set()
    for r in config.rules:
        if r.is_analog and r.input in reserved:
            analog_used.add(r.input)

    # Axes driven by an ANALOG rule are overwritten every cycle: no store, no
    # AXIS_INC/DEC, and only one ANALOG rule per axis.
    analog_axes: dict[str, int] = {}
    stored_axes = {a.id for a in config.axes if not a.comment and a.store}
    for i, r in enumerate(config.rules):
        if r.comment or r.type != "ANALOG":
            continue
        path = f"rules[{i}].axis"
        if not r.axis:
            errors.append(ValidationError(path, "An axis is required — define one on the Axes tab first"))
        elif r.axis not in axis_ids:
            errors.append(ValidationError(path, f"Unknown axis '{r.axis}'"))
        elif r.axis in analog_axes:
            errors.append(ValidationError(
                path, f"Axis '{r.axis}' is already driven by rule {analog_axes[r.axis] + 1}"))
        elif r.axis in stored_axes:
            errors.append(ValidationError(
                path, f"Axis '{r.axis}' cannot use store (its value comes from the sensor)"))
        else:
            analog_axes[r.axis] = i

    def check_input(path: str, value: str, what: str = "input"):
        if not value:
            errors.append(ValidationError(path, f"An {what} is required"))
        elif value in analog_used:
            errors.append(ValidationError(path, f"'{value}' is claimed as an analog input"))
        elif value not in valid_inputs:
            errors.append(ValidationError(path, f"Unknown {what} '{value}'"))

    def check_output(path: str, value: str, required: bool = True):
        if not value:
            if required:
                errors.append(ValidationError(path, "An output is required"))
        elif value not in valid_outputs:
            errors.append(ValidationError(path, f"Unknown output '{value}'"))

    for i, r in enumerate(config.rules):
        if r.comment:
            continue
        path = f"rules[{i}]"
        if r.type not in RULE_TYPE_LABELS:
            errors.append(ValidationError(f"{path}.type", f"Unknown rule type '{r.type}'"))
            continue

        if r.type in ("MAP", "TOGGLE", "PULSE"):
            check_input(f"{path}.input", r.input)
            check_output(f"{path}.output", r.output)
            if r.type == "PULSE":
                if not isinstance(r.pulse_ms, int) or r.pulse_ms < 0:
                    errors.append(ValidationError(f"{path}.pulse_ms", "Pulse must be 0 ms or more"))
                if not isinstance(r.delay_ms, int) or r.delay_ms < 0:
                    errors.append(ValidationError(f"{path}.delay_ms", "Delay must be 0 ms or more"))

        elif r.type == "NOR":
            if not r.inputs:
                errors.append(ValidationError(f"{path}.inputs", "At least one input is required"))
            for j, inp in enumerate(r.inputs):
                check_input(f"{path}.inputs[{j}]", inp)
            check_output(f"{path}.output", r.output)

        elif r.type == "ENCODER":
            if len(r.inputs) != 2:
                errors.append(ValidationError(f"{path}.inputs", "Encoder requires exactly 2 pins"))
            for j, inp in enumerate(r.inputs):
                if inp not in reserved:
                    errors.append(ValidationError(f"{path}.inputs[{j}]", f"Encoder pin must be a physical pin, not '{inp}'"))
                elif inp in analog_used:
                    errors.append(ValidationError(f"{path}.inputs[{j}]", f"Pin '{inp}' is claimed as an analog input"))
                elif inp in encoder_pins:
                    errors.append(ValidationError(f"{path}.inputs[{j}]", f"Pin '{inp}' is already used by another encoder"))
                else:
                    encoder_pins.add(inp)
            if len(r.inputs) == 2 and r.inputs[0] == r.inputs[1]:
                errors.append(ValidationError(f"{path}.inputs", "Encoder pins must be two different pins"))
            # cw/ccw are optional (an encoder can drive axes only), but a
            # provided output must be valid — a typo'd free-text entry used to
            # slip through and be rejected by the device at save time.
            check_output(f"{path}.cw", r.cw, required=False)
            check_output(f"{path}.ccw", r.ccw, required=False)
            if r.divisor not in (1, 2, 4):
                errors.append(ValidationError(f"{path}.divisor", "Steps/detent must be 1, 2, or 4"))
            if not isinstance(r.pulse_ms, int) or r.pulse_ms < 0:
                errors.append(ValidationError(f"{path}.pulse_ms", "Pulse must be 0 ms or more"))

        elif r.type in ("AXIS_INC", "AXIS_DEC"):
            check_input(f"{path}.input", r.input)
            if not r.axis:
                errors.append(ValidationError(f"{path}.axis", "An axis is required — define one on the Axes tab first"))
            elif r.axis not in axis_ids:
                errors.append(ValidationError(f"{path}.axis", f"Unknown axis '{r.axis}'"))
            elif r.axis in analog_axes:
                errors.append(ValidationError(f"{path}.axis", f"Axis '{r.axis}' is driven by an ANALOG rule"))
            if not isinstance(r.step, int) or r.step < 1 or r.step > 65535:
                errors.append(ValidationError(f"{path}.step", "Step must be 1-65535"))

        elif r.type == "ANALOG":
            if not r.input:
                errors.append(ValidationError(f"{path}.input", "An input is required"))
            elif r.input in reserved and r.input not in analog_capable:
                errors.append(ValidationError(f"{path}.input", f"Pin '{r.input}' is not analog capable on this board"))
            elif r.input not in analog_capable:
                errors.append(ValidationError(f"{path}.input", f"Input '{r.input}' is not an analog pin"))
            # axis checked in the pre-pass above
            lo = 0 if r.min is None else r.min
            hi = ANALOG_MAX if r.max is None else r.max
            bounds_ok = True
            for key, v in (("min", lo), ("max", hi)):
                if not _in_range(v):
                    errors.append(ValidationError(f"{path}.{key}", f"{key} must be an integer 0-65535"))
                    bounds_ok = False
            if bounds_ok and hi <= lo:
                errors.append(ValidationError(f"{path}.max", "max must be greater than min"))
                bounds_ok = False
            dz = 0 if r.deadzone is None else r.deadzone
            dz_ok = _in_range(dz)
            if not dz_ok:
                errors.append(ValidationError(f"{path}.deadzone", "deadzone must be an integer 0-65535"))
            if r.center is not None:
                if not _in_range(r.center):
                    errors.append(ValidationError(f"{path}.center", "center must be an integer 0-65535"))
                elif bounds_ok and dz_ok and not (lo < r.center - dz and r.center + dz < hi):
                    errors.append(ValidationError(
                        f"{path}.center", "center +/- deadzone must lie strictly between min and max"))
            elif dz:
                errors.append(ValidationError(f"{path}.deadzone", "deadzone requires center"))
            if r.filter is not None and not (_is_int(r.filter) and 0 <= r.filter <= 8):
                errors.append(ValidationError(f"{path}.filter", "filter must be an integer 0-8"))
            if r.hysteresis is not None and not _in_range(r.hysteresis):
                errors.append(ValidationError(f"{path}.hysteresis", "hysteresis must be an integer 0-65535"))
            if r.curve is not None:
                msg = _curve_error(r.curve)
                if msg:
                    errors.append(ValidationError(f"{path}.curve", msg))

        elif r.type == "THRESHOLD":
            if not r.input:
                errors.append(ValidationError(f"{path}.input", "An input is required"))
            elif r.input in reserved and r.input not in analog_capable:
                errors.append(ValidationError(f"{path}.input", f"Pin '{r.input}' is not analog capable on this board"))
            elif r.input not in analog_capable and r.input not in axis_ids:
                errors.append(ValidationError(
                    f"{path}.input", f"Input '{r.input}' must be an analog pin or an axis id"))
            check_output(f"{path}.output", r.output)
            if (r.above is None) == (r.below is None):
                errors.append(ValidationError(f"{path}.above", "Set exactly one of above / below"))
            else:
                key = "above" if r.above is not None else "below"
                if not _in_range(getattr(r, key)):
                    errors.append(ValidationError(f"{path}.{key}", "threshold must be an integer 0-65535"))
            if r.hysteresis is not None and not _in_range(r.hysteresis):
                errors.append(ValidationError(f"{path}.hysteresis", "hysteresis must be an integer 0-65535"))

    # Firmware caps
    if len(config.bools) > 255:
        errors.append(ValidationError("bools", "Too many variables (max 255)"))
    if len(config.axes) > 255:
        errors.append(ValidationError("axes", "Too many axes (max 255)"))
    if len(config.rules) > 256:
        errors.append(ValidationError("rules", "Too many rules (max 256)"))
    if not errors:
        size = len(json.dumps(config.to_dict()))
        if size > MAX_CONFIG_BYTES:
            errors.append(ValidationError(
                "config", f"Configuration is too large ({size} bytes, max {MAX_CONFIG_BYTES})"))

    return errors
