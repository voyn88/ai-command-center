"""Verify gate for the design-token package (Wave 0 / F-3).

Two mechanical guarantees keep ``command_center/design`` honest as the single
source of truth for the desktop shell and the mobile theme:

1. ``tokens.css`` is byte-for-byte regenerable from ``tokens.json`` — nobody has
   hand-edited the generated file out of sync with the canonical tokens.
2. ``primitives.css`` contains no raw hex color — the "tokens only" rule holds,
   so a hue changed once in ``tokens.json`` propagates everywhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from command_center.design import build_tokens

DESIGN_DIR = Path(build_tokens.__file__).resolve().parent

# A '#' followed by 3, 4, 6, or 8 hex digits on a word boundary — a raw color.
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")


def test_tokens_css_is_regenerable_from_json() -> None:
    on_disk = (DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
    regenerated = build_tokens.generate_css()
    assert on_disk == regenerated, (
        "tokens.css is out of sync with tokens.json — run "
        "`python -m command_center.design.build_tokens`."
    )


def test_generated_css_covers_every_token() -> None:
    tokens = json.loads((DESIGN_DIR / "tokens.json").read_text(encoding="utf-8"))
    css = (DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
    # Every color and shadow token must surface as a custom property.
    for name in list(tokens["color"]) + list(tokens["shadow"]):
        assert f"--{name}:" in css, f"token --{name} missing from tokens.css"


def test_primitives_use_tokens_only_no_raw_hex() -> None:
    primitives = (DESIGN_DIR / "primitives.css").read_text(encoding="utf-8")
    offenders = HEX_RE.findall(primitives)
    assert not offenders, (
        "primitives.css must reference tokens only (var(--...)); "
        f"found raw hex color(s): {offenders}"
    )
    # Sanity: it actually consumes tokens.
    assert "var(--" in primitives


def test_no_trailing_blank_line_at_eof() -> None:
    for name in ("tokens.json", "tokens.css", "primitives.css"):
        text = (DESIGN_DIR / name).read_text(encoding="utf-8")
        assert text.endswith("\n"), f"{name} should end with a newline"
        assert not text.endswith("\n\n"), f"{name} has a blank line at EOF"
