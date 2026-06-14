"""Central design tokens for the SIMINPUT configurator.

A restrained, information-dense theme in light and dark variants. Every colour
is a (light, dark) tuple — customtkinter widgets pick the right one for the
active appearance mode automatically. One accent (indigo) carries interaction;
green/red/amber are reserved strictly for state semantics (connected / error /
warning) so colour always means the same thing.

Plain-tk widgets (Canvas) can't take tuples — pass colours through resolve().
"""

from __future__ import annotations

import customtkinter as ctk

# -- Surfaces (layered: cool near-white / warm-neutral near-black) --
BG = ("#F1F1F4", "#16171A")          # window base
SURFACE = ("#FAFAFC", "#1E1F23")     # panels / page sections
SURFACE_2 = ("#ECECF0", "#26272C")   # cards, rows, raised blocks
SURFACE_3 = ("#E2E2E8", "#2E2F35")   # inputs, hover targets
HOVER = ("#D9D9E0", "#33343A")       # generic hover fill

BORDER = ("#D5D5DC", "#34353B")
BORDER_STRONG = ("#B9B9C3", "#45464E")

# -- Corner radius -- square, crisp look. One knob for the whole UI.
RADIUS = 0

# -- Text --
TEXT = ("#1B1C20", "#E7E7EA")
TEXT_DIM = ("#5C5D66", "#9B9CA3")
TEXT_MUTED = ("#90919A", "#65666E")

# -- Accent (interaction / brand) --
ACCENT = ("#5E6AD2", "#5E6AD2")
ACCENT_HOVER = ("#4F5BC4", "#6E7AE6")
ACCENT_SOFT = ("#E2E5F9", "#2C2F52")   # tinted surface for selected nav, badges

# -- State semantics --
SUCCESS = ("#1F883D", "#3FB950")
SUCCESS_SOFT = ("#DCF2E2", "#16301E")
ERROR = ("#CF222E", "#E5534B")
ERROR_HOVER = ("#A40E26", "#EE6359")
ERROR_SOFT = ("#FBE3E1", "#33201E")
WARN = ("#9A6700", "#D9A33D")
WARN_SOFT = ("#F6EBCB", "#332915")

# -- Live monitor canvases --
CANVAS_BG = ("#E7E7EC", "#101114")
CELL_OFF = ("#DBDBE1", "#202126")
CELL_BORDER = ("#C8C8D0", "#2C2D33")
CELL_ON = SUCCESS
CELL_ON_TEXT = ("#FFFFFF", "#0B1F12")
AXIS_FILL = ACCENT
AXIS_CENTER = ("#B4B4BE", "#3A3B42")


def resolve(color: tuple[str, str] | str) -> str:
    """Pick the single colour for the active appearance mode.

    For plain-tk widgets (Canvas) that don't understand customtkinter's
    (light, dark) tuples.
    """
    if isinstance(color, str):
        return color
    return color[0] if ctk.get_appearance_mode() == "Light" else color[1]


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight=weight)


def mono(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="monospace", size=size, weight=weight)


def primary_button(master, text: str, command=None, **kw) -> ctk.CTkButton:
    opts = dict(
        text=text, command=command,
        fg_color=ACCENT, hover_color=ACCENT_HOVER,
        text_color="#FFFFFF", corner_radius=RADIUS, height=34,
        font=font(13, "bold"),
    )
    opts.update(kw)
    return ctk.CTkButton(master, **opts)


def ghost_button(master, text: str, command=None, **kw) -> ctk.CTkButton:
    opts = dict(
        text=text, command=command,
        fg_color="transparent", hover_color=HOVER,
        text_color=TEXT, border_width=1, border_color=BORDER_STRONG,
        corner_radius=RADIUS, height=34, font=font(13),
    )
    opts.update(kw)
    return ctk.CTkButton(master, **opts)


def danger_button(master, text: str, command=None, **kw) -> ctk.CTkButton:
    opts = dict(
        text=text, command=command,
        fg_color="transparent", hover_color=ERROR_SOFT,
        text_color=ERROR, border_width=1, border_color=ERROR,
        corner_radius=RADIUS, height=34, font=font(13),
    )
    opts.update(kw)
    return ctk.CTkButton(master, **opts)
