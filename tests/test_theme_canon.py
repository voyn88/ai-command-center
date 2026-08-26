"""One canon, one source of truth — the cross-surface theme verify gate.

``command_center/design/tokens.json`` is the single source for every themed
surface in the app. This test proves the other surfaces stay on that canon:

* **No divergent brand hex** — the retired palettes (indigo ``#3b5bdb`` desktop,
  GitHub-blue ``#0969da`` board, glass ``#7c5cff``) never reappear anywhere.
* **No web-font CDN / no Inter** — the design system is system-first.
* **Both themes** — every surface defines a dark *and* a light palette.
* **Derived, not re-hardcoded** — the Qt shell and the Streamlit board carry no
  raw colour hex of their own, and the Streamlit config's colours match the ones
  ``tokens.json`` dictates.

It is deliberately file-based (reads sources as text) so it also covers the web
shell's ``tokens.css``, which has no Python to import.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from command_center.design import color, load_tokens

REPO_ROOT = Path(__file__).resolve().parents[1]

DESKTOP_TOKENS = REPO_ROOT / "command_center" / "desktop" / "tokens.py"
BOARD_STYLE = REPO_ROOT / "command_center" / "ui" / "board_style.py"
STREAMLIT_CONFIG = REPO_ROOT / ".streamlit" / "config.toml"
WEB_TOKENS_CSS = REPO_ROOT / "web" / "src" / "theme" / "tokens.css"
DESIGN_TOKENS_JSON = REPO_ROOT / "command_center" / "design" / "tokens.json"
DESIGN_TOKENS_CSS = REPO_ROOT / "command_center" / "design" / "tokens.css"

# Every themed surface that must trace back to the canon.
ALL_SURFACES = (
    DESKTOP_TOKENS,
    BOARD_STYLE,
    STREAMLIT_CONFIG,
    WEB_TOKENS_CSS,
    DESIGN_TOKENS_JSON,
    DESIGN_TOKENS_CSS,
)

# The retired, pre-canon palettes. None of these may appear on any surface.
DIVERGENT_HEXES = (
    # desktop indigo
    "#3b5bdb", "#2f4bc0", "#5b7cff", "#7089ff",
    # GitHub-derived board / streamlit
    "#0969da", "#58a6ff", "#d0d7de", "#30363d", "#161b22", "#0d1117",
    "#1a7f37", "#3fb950", "#bf8700", "#d29922", "#cf222e", "#f85149",
    # glass web
    "#7c5cff", "#4d9fff", "#33e0c0", "#3ddc84", "#f5b23a", "#ff5d6f",
    "#eef2fb", "#aab3c9", "#7c85a0",
)

# A '#' followed by 3/4/6/8 hex digits on a word boundary — a raw colour.
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")

# The Inter web font as it appears in a font stack or a CDN request — precise
# enough not to trip over prose like "interaction" or "interface".
INTER_FONT_RE = re.compile(r"""["']inter["']|family=inter|inter:wght""", re.IGNORECASE)


def test_no_divergent_brand_hex_on_any_surface() -> None:
    for path in ALL_SURFACES:
        text = path.read_text(encoding="utf-8").lower()
        for bad in DIVERGENT_HEXES:
            assert bad not in text, f"{path.relative_to(REPO_ROOT)} still carries retired hex {bad}"


def test_no_inter_or_webfont_cdn_on_any_surface() -> None:
    for path in ALL_SURFACES:
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        assert not INTER_FONT_RE.search(text), (
            f"{path.relative_to(REPO_ROOT)} references the Inter web font"
        )
        assert "fonts.googleapis" not in low, f"{path.relative_to(REPO_ROOT)} pulls a web-font CDN"
        assert "@font-face" not in low, f"{path.relative_to(REPO_ROOT)} bundles a web font"


def test_desktop_and_board_carry_no_raw_colour_hex() -> None:
    # The Qt shell and the board derive every colour from tokens.json, so their
    # own source must contain no raw hex — a hue changes in one place only.
    for path in (DESKTOP_TOKENS, BOARD_STYLE):
        offenders = HEX_RE.findall(path.read_text(encoding="utf-8"))
        assert not offenders, f"{path.relative_to(REPO_ROOT)} hardcodes colour hex {offenders}"


def test_desktop_palette_is_the_canon_accent_family() -> None:
    from command_center.desktop import tokens as desktop_tokens

    assert desktop_tokens.DARK.accent == color("accent", "dark") == "#6C8CFF"
    assert desktop_tokens.LIGHT.accent == color("accent", "light")
    # Both themes exist and are distinct.
    assert desktop_tokens.LIGHT.bg_base != desktop_tokens.DARK.bg_base


def test_board_palette_defines_both_themes_from_canon() -> None:
    from command_center.ui import board_style

    assert set(board_style._PALETTE) == {"dark", "light"}
    assert board_style._PALETTE["dark"]["live_line"] == color("accent", "dark")
    assert board_style._PALETTE["light"]["done_line"] == color("ok", "light")


def test_streamlit_config_colours_match_tokens_json() -> None:
    config = tomllib.loads(STREAMLIT_CONFIG.read_text(encoding="utf-8"))
    # config key -> tokens.json colour token
    mapping = {
        "primaryColor": "accent",
        "backgroundColor": "bg",
        "secondaryBackgroundColor": "surface",
        "textColor": "text",
        "borderColor": "line",
        "greenColor": "ok",
        "orangeColor": "warn",
        "redColor": "crit",
        "blueColor": "accent",
        "grayColor": "text-2",
    }
    for theme in ("light", "dark"):
        block = config["theme"][theme]
        assert set(mapping) <= set(block), f"[theme.{theme}] missing keys"
        for key, token in mapping.items():
            assert block[key] == color(token, theme), (
                f"[theme.{theme}].{key} = {block[key]} but tokens.json {token} = "
                f"{color(token, theme)}"
            )


def test_web_glass_theme_is_dark_first_canon() -> None:
    css = WEB_TOKENS_CSS.read_text(encoding="utf-8")
    # Accent is the canon family; dark values are the default (dark-first).
    assert f"--accent-1:{color('accent', 'dark')}" in css.replace(" ", "")
    assert f"--tx:{color('text', 'dark')}" in css.replace(" ", "")


def test_canon_font_stack_is_system_first() -> None:
    sans = load_tokens()["typography"]["font"]["sans"]
    assert "inter" not in sans.lower()
    assert "system-ui" in sans
