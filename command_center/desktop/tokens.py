"""Design tokens for the desktop shell.

Single source of truth for spacing, typography, radii and control heights, and
the binding of the light/dark colour palettes, implementing
`docs/desktop/DESIGN_SYSTEM.md` §1–§2. Every value is defined once and referenced
by :mod:`command_center.desktop.theme` and the widgets, never hardcoded
per-widget.

The **colours** are not defined here: they are derived from the platform-canonical
``command_center/design/tokens.json`` (see :mod:`command_center.design`), so the
Qt shell shares one palette with the web shell, the Streamlit board and the mobile
theme. A few Qt-only surface tints (sidebar, selection, hover) have no dedicated
canonical token and are computed as deterministic blends of canonical tokens via
:func:`command_center.design.mix`, so they, too, trace back to ``tokens.json``.

This module is pure data (dataclasses + constants) and imports only the equally
pure :mod:`command_center.design` loader — nothing from Qt or from
``command_center`` core — so it stays trivially unit-testable with no Qt side
effects at import time.
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.design import Theme, color, mix

# --- §1.1 Spacing (px @ 1.0 scale) -----------------------------------------
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32

# --- §1.2 Typography (pt) --------------------------------------------------
TYPE_DISPLAY_PT = 20
TYPE_TITLE_PT = 15
TYPE_BODY_PT = 13
TYPE_CAPTION_PT = 11
TYPE_MONO_PT = 12

# --- §1.3 Control heights (px) ---------------------------------------------
CONTROL_HEIGHT_SM = 24
CONTROL_HEIGHT_MD = 32
CONTROL_HEIGHT_LG = 40
CONTROL_HEIGHT_TOPBAR = 48

# --- §1.4 Corner radii (px) ------------------------------------------------
RADIUS_SM = 4
RADIUS_MD = 8
RADIUS_LG = 12

# --- §1.5 Icon sizes (px) --------------------------------------------------
ICON_SM = 16
ICON_MD = 20
ICON_LG = 32

# --- §1.6 Borders ----------------------------------------------------------
BORDER_HAIRLINE_PX = 1
BORDER_FOCUS_PX = 2


@dataclass(frozen=True)
class Palette:
    """A complete theme palette. Every token in §1.10/§2 has a value here so no
    component can bypass theme switching with a hardcoded colour."""

    name: str
    # Surfaces
    bg_base: str
    surface: str
    surface_raised: str
    sidebar_bg: str
    topbar_bg: str
    # Lines
    border: str
    # Text
    text_primary: str
    text_secondary: str
    text_disabled: str
    # Accent / selection
    accent: str
    accent_emphasis: str
    selected_bg: str
    hover_bg: str
    # §1.10 Semantic status colours
    status_neutral: str
    status_info: str
    status_active: str
    status_success: str
    status_warning: str
    status_danger: str
    status_cancelled: str
    status_sensitive: str


def _palette(theme: Theme) -> Palette:
    """Build a :class:`Palette` for ``theme`` from the canonical design tokens.

    Every surface, text, accent and status colour maps to a token in
    ``command_center/design/tokens.json``. The status ramp reuses the canonical
    semantic hues (``info``→accent, ``active``→violet, ``success``→ok,
    ``warning``→warn, ``danger``/``sensitive``→crit, ``neutral``/``cancelled``→
    muted text); the canon carries no distinct "sensitive" tone, so it shares the
    danger hue. Sidebar, selection and hover are subtle blends of canonical
    tokens (see module docstring), not new hand-picked colours.
    """
    def c(name: str) -> str:
        return color(name, theme)

    return Palette(
        name=theme,
        # Surfaces
        bg_base=c("bg"),
        surface=c("surface"),
        surface_raised=c("raise"),
        sidebar_bg=mix(c("bg"), c("surface"), 0.5),
        topbar_bg=c("surface"),
        # Lines
        border=c("line"),
        # Text
        text_primary=c("text"),
        text_secondary=c("text-2"),
        text_disabled=c("text-3"),
        # Accent / selection
        accent=c("accent"),
        accent_emphasis=c("accent-2"),
        selected_bg=mix(c("bg"), c("accent"), 0.14),
        hover_bg=mix(c("surface"), c("text"), 0.05),
        # §1.10 Semantic status colours
        status_neutral=c("text-2"),
        status_info=c("accent"),
        status_active=c("violet"),
        status_success=c("ok"),
        status_warning=c("warn"),
        status_danger=c("crit"),
        status_cancelled=c("text-3"),
        status_sensitive=c("crit"),
    )


LIGHT = _palette("light")

DARK = _palette("dark")
