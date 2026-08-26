"""Generate ``tokens.css`` deterministically from ``tokens.json``.

``tokens.json`` is the single source of truth for the design system. This module
turns it into CSS custom properties that the desktop web shell imports directly
and that a mobile theme adapter can mirror. The mapping is intentionally
mechanical so the output can be regenerated and verified byte-for-byte in CI
(see ``tests/test_design_tokens.py``).

Layout of the emitted stylesheet:

* ``:root`` — theme-independent tokens (typography, spacing, radii, motion) plus
  the **dark** theme as the default.
* ``@media (prefers-color-scheme: light)`` — light theme for users whose OS asks
  for it and who have not made an explicit choice.
* ``:root[data-theme="dark"]`` / ``:root[data-theme="light"]`` — explicit override
  hooks; a shell that stamps ``data-theme`` on the root wins over the OS media
  query in both directions.

Regenerate in place::

    python -m command_center.design.build_tokens
"""

from __future__ import annotations

import json
from pathlib import Path

DESIGN_DIR = Path(__file__).resolve().parent
TOKENS_JSON = DESIGN_DIR / "tokens.json"
TOKENS_CSS = DESIGN_DIR / "tokens.css"

# Themed groups carry a {"dark": ..., "light": ...} value per token and are
# emitted once per theme block. Base groups carry a single value and are emitted
# once, in :root. Each entry maps a JSON group to the CSS custom-property prefix.
THEMED_GROUPS: tuple[tuple[str, str], ...] = (
    ("color", ""),
    ("shadow", ""),
)
BASE_GROUPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("typography", "font"), "font-"),
    (("typography", "size"), "fs-"),
    (("typography", "weight"), "fw-"),
    (("typography", "tracking"), "tracking-"),
    (("space",), "space-"),
    (("radius",), "radius-"),
    (("motion", "duration"), "dur-"),
    (("motion", "easing"), "ease-"),
)


def _dig(data: dict, path: tuple[str, ...]) -> dict:
    node = data
    for key in path:
        node = node[key]
    return node


def _decls(prefix: str, mapping: dict, theme: str | None, indent: str) -> list[str]:
    lines: list[str] = []
    for name, value in mapping.items():
        resolved = value[theme] if theme is not None else value
        lines.append(f"{indent}--{prefix}{name}: {resolved};")
    return lines


def _theme_block(tokens: dict, theme: str, indent: str) -> list[str]:
    lines: list[str] = []
    for group, prefix in THEMED_GROUPS:
        lines.append(f"{indent}/* {group} */")
        lines.extend(_decls(prefix, tokens[group], theme, indent))
    return lines


def generate_css(tokens: dict | None = None) -> str:
    """Return the full ``tokens.css`` text for the given (or on-disk) tokens."""
    if tokens is None:
        tokens = json.loads(TOKENS_JSON.read_text(encoding="utf-8"))

    meta = tokens.get("$meta", {})
    out: list[str] = [
        "/* AUTO-GENERATED from tokens.json by build_tokens.py — do not edit by hand. */",
        f"/* {meta.get('name', 'design-tokens')} v{meta.get('version', '0')} "
        "— dark-first; explicit data-theme overrides the OS media query. */",
        "",
        ":root {",
    ]

    # Base (theme-independent) tokens.
    for path, prefix in BASE_GROUPS:
        label = path[-1] if len(path) > 1 else path[0]
        out.append(f"  /* {label} */")
        out.extend(_decls(prefix, _dig(tokens, path), None, "  "))

    # Dark is the default theme, emitted inside the same :root.
    out.append("  /* --- theme: dark (default) --- */")
    out.extend(_theme_block(tokens, "dark", "  "))
    out.append("}")
    out.append("")

    # Light theme for OS preference, only when no explicit choice was made.
    out.append("@media (prefers-color-scheme: light) {")
    out.append("  :root {")
    out.extend(_theme_block(tokens, "light", "    "))
    out.append("  }")
    out.append("}")
    out.append("")

    # Explicit overrides win over the media query in both directions.
    for theme in ("dark", "light"):
        out.append(f':root[data-theme="{theme}"] {{')
        out.extend(_theme_block(tokens, theme, "  "))
        out.append("}")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def main() -> None:
    TOKENS_CSS.write_text(generate_css(), encoding="utf-8")
    print(f"wrote {TOKENS_CSS.relative_to(DESIGN_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
