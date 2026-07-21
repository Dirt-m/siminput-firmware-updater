from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

BOARD_PINS: dict[str, list[str]] = {
    "rev1": [f"D{i}" for i in range(1, 25)] + ["A6", "A7", "A8"],
    "rev2": [f"D{i}" for i in range(1, 39)] + ["A1", "A2", "A3", "A4"],
}

ALL_KNOWN_PINS: list[str] = sorted(
    {p for pins in BOARD_PINS.values() for p in pins},
    key=lambda p: (p[0], int(p[1:])),
)


def pins_for_board(board_map: str) -> list[str]:
    return BOARD_PINS.get(board_map, ALL_KNOWN_PINS)

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
}


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
    debounce_ms: int = 10
    inactivity_refresh: float | bool = 1.0

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "pid": self.pid}
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
        )


@dataclass
class BoolVar:
    id: str = ""
    default: bool = False
    store: bool = False

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"id": self.id, "default": self.default}
        if self.store:
            d["store"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BoolVar:
        return cls(id=d.get("id", ""), default=d.get("default", False), store=d.get("store", False))


@dataclass
class Axis:
    id: str = ""
    output: int | str = 1
    default: int = 32767
    store: bool = False
    backlight: bool = False

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"id": self.id, "output": self.output, "default": self.default}
        if self.store:
            d["store"] = True
        if self.backlight:
            d["backlight"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Axis:
        return cls(
            id=d.get("id", ""),
            output=d.get("output", 1),
            default=d.get("default", 32767),
            store=d.get("store", False),
            backlight=d.get("backlight", False),
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
    pulse_ms: int = 100
    delay_ms: int = 0
    step: int = 1
    divisor: int = 2

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": self.type}
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
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Rule:
        # Tolerate missing keys — validate() reports the problems with paths,
        # which beats a bare KeyError surfacing in the UI.
        tp = d.get("type", "")
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
        )

    def summary(self) -> str:
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

    def to_dict(self) -> dict:
        return {
            "device": self.device.to_dict(),
            "bools": [b.to_dict() for b in self.bools],
            "axes": [a.to_dict() for a in self.axes],
            "rules": [r.to_dict() for r in self.rules],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        return cls(
            device=DeviceSettings.from_dict(d.get("device", {})),
            bools=[BoolVar.from_dict(b) for b in d.get("bools", [])],
            axes=[Axis.from_dict(a) for a in d.get("axes", [])],
            rules=[Rule.from_dict(r) for r in d.get("rules", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> Config:
        return cls.from_dict(json.loads(text))

    def get_input_choices(self, board_map: str = "") -> list[str]:
        choices = list(pins_for_board(board_map))
        choices += [f"B{i}" for i in range(1, 128)]
        choices += [b.id for b in self.bools if b.id]
        return choices

    def get_output_choices(self) -> list[str]:
        choices = [f"B{i}" for i in range(1, 128)]
        choices += [b.id for b in self.bools if b.id]
        choices.append("REFRESH")
        return choices

    def get_axis_choices(self) -> list[str]:
        return [a.id for a in self.axes if a.id]


_B_PATTERN = re.compile(r"^B(\d+)$")


def validate(config: Config, board_map: str = "") -> list[ValidationError]:
    errors: list[ValidationError] = []

    # Device settings
    if not config.device.name or not config.device.name.strip():
        errors.append(ValidationError("device.name", "Device name is required"))
    elif len(config.device.name) > 32:
        errors.append(ValidationError("device.name", "Device name must be 32 characters or fewer"))

    pid = config.device.pid
    if not isinstance(pid, int) or pid < 1 or pid > 0xFFFF:
        errors.append(ValidationError("device.pid", "USB Product ID must be 0x0001-0xFFFF"))
    elif pid == 0x80F4:
        errors.append(ValidationError("device.pid", "PID 0x80F4 is reserved and cannot be used"))

    if not isinstance(config.device.debounce_ms, int) or not (0 <= config.device.debounce_ms <= 100):
        errors.append(ValidationError("device.debounce_ms", "Debounce must be 0-100 ms"))

    ir = config.device.inactivity_refresh
    if ir is not False:
        if not isinstance(ir, (int, float)) or ir <= 0:
            errors.append(ValidationError("device.inactivity_refresh", "Keep-alive must be a positive number or disabled"))

    # Bools
    bool_ids = set()
    reserved = set(pins_for_board(board_map))
    for i, b in enumerate(config.bools):
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

    for i, r in enumerate(config.rules):
        path = f"rules[{i}]"
        if r.type not in RULE_TYPE_LABELS:
            errors.append(ValidationError(f"{path}.type", f"Unknown rule type '{r.type}'"))
            continue

        if r.type in ("MAP", "TOGGLE", "PULSE"):
            if r.input and r.input not in valid_inputs:
                errors.append(ValidationError(f"{path}.input", f"Unknown input '{r.input}'"))
            if r.output and r.output not in valid_outputs:
                errors.append(ValidationError(f"{path}.output", f"Unknown output '{r.output}'"))
            if r.type == "PULSE":
                if not isinstance(r.pulse_ms, int) or r.pulse_ms < 0:
                    errors.append(ValidationError(f"{path}.pulse_ms", "Pulse must be 0 ms or more"))
                if not isinstance(r.delay_ms, int) or r.delay_ms < 0:
                    errors.append(ValidationError(f"{path}.delay_ms", "Delay must be 0 ms or more"))

        elif r.type == "NOR":
            for j, inp in enumerate(r.inputs):
                if inp not in valid_inputs:
                    errors.append(ValidationError(f"{path}.inputs[{j}]", f"Unknown input '{inp}'"))
            if r.output and r.output not in valid_outputs:
                errors.append(ValidationError(f"{path}.output", f"Unknown output '{r.output}'"))

        elif r.type == "ENCODER":
            if len(r.inputs) != 2:
                errors.append(ValidationError(f"{path}.inputs", "Encoder requires exactly 2 pins"))
            for j, inp in enumerate(r.inputs):
                if inp not in reserved:
                    errors.append(ValidationError(f"{path}.inputs[{j}]", f"Encoder pin must be a physical pin, not '{inp}'"))
            if r.divisor not in (1, 2, 4):
                errors.append(ValidationError(f"{path}.divisor", "Steps/detent must be 1, 2, or 4"))
            if not isinstance(r.pulse_ms, int) or r.pulse_ms < 0:
                errors.append(ValidationError(f"{path}.pulse_ms", "Pulse must be 0 ms or more"))

        elif r.type in ("AXIS_INC", "AXIS_DEC"):
            if r.input and r.input not in valid_inputs:
                errors.append(ValidationError(f"{path}.input", f"Unknown input '{r.input}'"))
            if r.axis and r.axis not in axis_ids:
                errors.append(ValidationError(f"{path}.axis", f"Unknown axis '{r.axis}'"))
            if not isinstance(r.step, int) or r.step < 1 or r.step > 65535:
                errors.append(ValidationError(f"{path}.step", "Step must be 1-65535"))

    return errors
