"""UX-1: AppShell composition (design tokens, theme engine, Sidebar,
TopCommandBar and Content Area).

These tests guard the "do not redesign functionality" constraint the UX-1
increment was scoped under: navigation must still work exactly as before
(same `nav_page` session-state key, same options), the consolidated color
tokens must still resolve to the exact values the old per-app.py dicts used,
and the Inspector placeholder must render without being wired to any real
data or behavior.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import tokens

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
CONFIG_TOML_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml"


def _at_on_page(page_key: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = page_key
    at.run()
    return at


def test_shell_renders_title_and_sidebar_navigation():
    at = _at_on_page("dashboard")
    assert not at.exception
    assert at.title[0].value == "🧭 AI Command Center"
    # The nav is grouped buttons now, not one flat radio. Asserts the property
    # — sections are reachable — rather than the widget implementing it.
    keys = {b.key for b in at.sidebar.button}
    assert "nav_btn_dashboard" in keys
    assert "nav_btn_runs" in keys
    assert "nav_btn_focus" not in keys
    body = "".join(item.value for item in at.markdown)
    compact = body.replace(" ", "")
    assert '[data-testid="stCaptionContainer"]' in body
    assert 'a[aria-label="Link to heading"]' in body
    assert "min-width:24px" in compact
    assert "opacity:1!important" in compact


def test_sidebar_still_has_command_palette_trigger():
    at = _at_on_page("dashboard")
    assert not at.exception
    assert any(b.key == "open_palette_btn" for b in at.sidebar.button)


def test_navigating_via_nav_page_still_switches_page_content():
    at = _at_on_page("kanban")
    assert not at.exception
    assert any(s.value == "Kanban" for s in at.subheader)


def test_top_bar_search_and_inspector_are_wired():
    """UX-2c: the Inspector pane is now real (search + selected detail). With
    nothing selected it shows a guiding empty state — not the old inert
    placeholder. The popover content runs regardless of its visual state, so
    the empty-state `st.info` is present in `at.info`."""
    at = _at_on_page("dashboard")
    assert not at.exception
    assert not any("следующей фазе UX" in info.value for info in at.info)
    assert any(
        "Инспектор" in info.value or "Выберите задачу" in info.value for info in at.info
    ), "Inspector empty-state message not found"
    assert any(button.key == "top_open_palette_btn" for button in at.button)


def test_priority_and_launch_status_color_tokens_are_unchanged():
    """Regression guard for the token-consolidation move (UX-1): these exact
    mappings used to be independent dicts hardcoded in app.py."""
    assert tokens.PRIORITY_COLORS == {
        "Low": "gray",
        "Medium": "blue",
        "High": "orange",
        "Critical": "red",
    }
    assert tokens.LAUNCH_STATUS_COLORS == {
        "Ready": "gray",
        "Launching": "blue",
        "Running": "blue",
        "Completed": "green",
        "Failed": "red",
        "Cancelled": "gray",
        "Requires Attention": "orange",
    }


def test_theme_config_defines_both_light_and_dark_modes():
    config = tomllib.loads(CONFIG_TOML_PATH.read_text(encoding="utf-8"))
    assert "light" in config["theme"]
    assert "dark" in config["theme"]
    for mode in ("light", "dark"):
        section = config["theme"][mode]
        assert section["primaryColor"]
        assert section["backgroundColor"]
        assert section["textColor"]
