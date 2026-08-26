"""Design tokens and primitives — single source of truth for shell + mobile.

``tokens.json`` is the one canonical palette. Every downstream surface — the web
shell (`tokens.css`), the desktop Qt shell (:mod:`command_center.desktop.tokens`),
the Streamlit board (:mod:`command_center.ui.board_style` and
``.streamlit/config.toml``) and the mobile theme — derives its colours from it, so
a hue changes in exactly one place. :func:`load_tokens` and :func:`color` are the
Python entry points those consumers use; the CSS side goes through
:mod:`command_center.design.build_tokens`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

Theme = Literal["dark", "light"]


@lru_cache(maxsize=1)
def load_tokens() -> dict:
    """Return the parsed canonical ``tokens.json`` (cached).

    Loaded as a package resource so it resolves identically from source and from
    a frozen desktop build (the PyInstaller specs bundle ``command_center`` data
    files, so ``tokens.json`` travels with the package).
    """
    raw = files("command_center.design").joinpath("tokens.json").read_text(encoding="utf-8")
    return json.loads(raw)


def color(name: str, theme: Theme) -> str:
    """Return the value of colour token ``name`` for ``theme`` (``"dark"``/``"light"``).

    Raises ``KeyError`` with a clear message if the token is unknown, so a typo in
    a downstream consumer fails loudly at import time rather than rendering a
    silently-wrong colour.
    """
    colors = load_tokens()["color"]
    try:
        return colors[name][theme]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(
            f"unknown colour token {name!r}/{theme!r}; "
            f"known tokens: {sorted(colors)}"
        ) from exc


def mix(a: str, b: str, t: float) -> str:
    """Blend two solid ``#rrggbb`` hex colours: ``t`` of the way from ``a`` to ``b``.

    A deterministic, dependency-free sRGB lerp used to derive the handful of
    Qt-only surface tints (selection, hover, sidebar) that have no dedicated
    canonical token — so those, too, trace back to ``tokens.json`` rather than
    being hand-picked hexes.
    """
    if not 0.0 <= t <= 1.0:
        raise ValueError(f"mix factor must be in [0, 1], got {t}")

    def _channels(hex_color: str) -> tuple[int, int, int]:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            raise ValueError(f"mix() needs solid #rrggbb colours, got {hex_color!r}")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    ar, ag, ab = _channels(a)
    br, bg, bb = _channels(b)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"
