"""Theme engine helper (UX-1 + UX-2a).

The actual theme (colors/radius/borders, light + dark) lives natively in
`.streamlit/config.toml`, per the project's Streamlit skill guidance. This
module is the one place allowed to inject global CSS (the UX-1 boundary) and
to read the *active* theme back at runtime, so no page reaches into
`st.context.theme` directly and duplicates this lookup.
"""

from __future__ import annotations

import streamlit as st

from command_center.design import color


def current_theme_type() -> str:
    """Return the active theme as seen by the browser: "light" or "dark"."""
    return st.context.theme.type


def inject_global_css() -> None:
    """Inject the app-wide CSS layer (UX-2a).

    Bordered cards transition on hover. A 120 ms border/box-shadow
       transition makes interactive cards feel responsive instead of static,
       without adding ambient motion to idle elements (world-class apps stay
       still until you touch them).

    Polling fragments deliberately receive no opacity animation. Even a small
    fade made the whole interface appear to blink whenever the lightweight
    status strip refreshed.

    Emitted on every run: Streamlit drops any ``st.markdown`` a rerun does not
    re-emit, so a once-per-session guard would leave the page unstyled after
    the first interaction (audit H6 on ``home_dashboard.inject_css``). Called
    once from ``shell.render_shell`` after ``st.set_page_config``.
    """
    st.markdown(
        """
<style>
[data-testid="stVerticalBlockBorderWrapper"] {
  transition: border-color 140ms ease, box-shadow 140ms ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
[data-testid="stCaptionContainer"],
kbd[aria-label^="Shortcut"] {
  opacity: 1 !important;
}
[data-testid="stCaptionContainer"] p,
kbd[aria-label^="Shortcut"] {
  color: inherit !important;
}
a[aria-label="Link to heading"] {
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  min-width: 24px;
  min-height: 24px;
}
button:focus-visible, a:focus-visible, [tabindex]:focus-visible {
  outline: 3px solid currentColor !important;
  outline-offset: 3px !important;
}
/* WCAG 2.2 SC 2.5.8 (Target Size, Minimum, 24×24 CSS px). Streamlit ships three
   interactive controls below that floor: the label help "?" icon, the toggle's
   focusable <input>, and the code-block toolbar's "Copy" button. Enlarge their
   hit area (the toggle input is a visually-hidden overlay, so growing it does
   not disturb the rendered switch). */
button[aria-label^="Help for"],
button[aria-label^="Help"] {
  min-width: 24px !important;
  min-height: 24px !important;
}
[data-baseweb="checkbox"] input,
input[role="switch"] {
  min-width: 24px !important;
  min-height: 24px !important;
}
[data-testid="stBaseButton-elementToolbar"] {
  min-width: 24px !important;
  min-height: 24px !important;
}
</style>
""",
        unsafe_allow_html=True,
    )

    _inject_contrast_css()


def _inject_contrast_css() -> None:
    """Raise below-AA token pairings to WCAG 2.2 AA (SC 1.4.3, 4.5:1).

    The light theme sets Streamlit's ``primaryColor`` to the canonical ``accent``
    token (``#4C6EF0``). As a primary fill it gives white text only 4.37:1, and
    as the selected segmented-control label on its own 10 % tint only 3.58:1 —
    both below 4.5:1. ``accent-2`` is the canon's darker sibling of the same hue
    (~5–6:1 in both roles), so we re-point exactly these two surfaces at it
    rather than inventing a hex. The dark accent (``#6C8CFF``) already clears AA,
    so the override is scoped to the light theme only and is emitted from the one
    module sanctioned to inject global CSS.
    """
    try:
        theme_type = current_theme_type()
    except Exception:  # noqa: BLE001 — theme context absent in headless AppTest
        theme_type = "light"
    if theme_type == "dark":
        return
    accent_strong = color("accent-2", "light")
    st.markdown(
        """
<style>
[data-testid="stButtonGroup"] button[data-selected="true"] {
  color: __ACCENT_STRONG__ !important;
  border-color: __ACCENT_STRONG__ !important;
}
[data-testid="stBaseButton-primary"] {
  background-color: __ACCENT_STRONG__ !important;
  border-color: __ACCENT_STRONG__ !important;
}
</style>
""".replace("__ACCENT_STRONG__", accent_strong),
        unsafe_allow_html=True,
    )
