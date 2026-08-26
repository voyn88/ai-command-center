"""WCAG 2.2 AA journey guards (fix/w1-ui-a11y-wcag-aa).

Regression tests for the three violation classes the Wave-1 axe audit flagged on
the aicc-home / aicc-kanban / aicc-execution journeys, plus the day-scoped digest
fix (audit LOW-2). They assert the *token-driven* remedies so the fixes cannot
silently regress:

* **aria-allowed-attr (critical)** — the shell repair must keep removing the
  invalid ``aria-expanded`` Streamlit stamps on the sidebar, including when it is
  re-applied as an attribute mutation (the case a childList-only observer missed).
* **color-contrast (serious, 1.4.3)** — the light ``primaryColor`` (canon
  ``accent``) fails 4.5:1 as a fill and on its own tint; the override re-points
  the two affected surfaces at the darker ``accent-2`` token, never a raw hex.
  The board's tile numbers and section titles drop the failing state hue for the
  high-contrast ``text`` token.
* **target-size (2.5.8, 24×24)** — the help icon, toggle input and code-block
  copy button are floored at 24px.
"""

from __future__ import annotations

import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.design import color
from command_center.ui import accessibility, board_style

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
THEME_SRC = (
    Path(__file__).resolve().parent.parent / "command_center" / "ui" / "theme.py"
).read_text(encoding="utf-8")
BOARD_SRC = (
    Path(__file__).resolve().parent.parent / "command_center" / "ui" / "board_style.py"
).read_text(encoding="utf-8")


def _rendered_css(page_key: str = "dashboard") -> str:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = page_key
    at.run()
    assert not at.exception
    return "".join(item.value for item in at.markdown)


# --------------------------------------------------------------------------
# aria-allowed-attr (critical)
# --------------------------------------------------------------------------


def test_shell_repair_removes_and_watches_aria_expanded() -> None:
    repair = accessibility._SHELL_REPAIR
    assert 'removeAttribute("aria-expanded")' in repair
    # A childList-only observer let the resizable sidebar re-stamp the attribute;
    # the fix must also watch the attribute itself, scoped to aria-expanded so it
    # cannot feed back on the role/label repairs.
    assert 'attributeFilter: ["aria-expanded"]' in repair
    assert "attributes: true" in repair


# --------------------------------------------------------------------------
# color-contrast (serious, WCAG 1.4.3)
# --------------------------------------------------------------------------


def test_contrast_override_uses_accent2_token_not_raw_accent() -> None:
    accent_strong = color("accent-2", "light")
    assert accent_strong == "#3B54D6"
    css = _rendered_css("dashboard").replace(" ", "")
    # The failing surfaces are re-pointed at accent-2 …
    assert f"color:{accent_strong}!important" in css
    assert f"background-color:{accent_strong}!important" in css
    assert '[data-testid="stButtonGroup"]button[data-selected="true"]' in css
    assert '[data-testid="stBaseButton-primary"]' in css
    # The override value is the canonical accent-2 token resolved at runtime, not
    # a literal pasted into a CSS property in the source.
    assert re.search(r":\s*#4[cC]6[eE]?[fF]0", THEME_SRC) is None


def test_board_title_and_value_use_high_contrast_text_token() -> None:
    for t in ("light", "dark"):
        assert board_style._board_palette(t)["strong"] == color("text", t)
    # The tile number and section title render in --aicc-strong (the text token),
    # not the state hue that failed contrast on its faint tint.
    assert ".aicc-tile .aicc-tile-value {" in BOARD_SRC
    value_rule = BOARD_SRC.split(".aicc-tile .aicc-tile-value {", 1)[1].split("}", 1)[0]
    assert "var(--aicc-strong)" in value_rule
    title_rule = BOARD_SRC.split(".aicc-section-head .aicc-title {", 1)[1].split("}", 1)[0]
    assert "var(--aicc-strong)" in title_rule
    # The board still derives every colour from tokens — no raw hex leaks in.
    assert not re.search(r"#(?:[0-9a-fA-F]{3,8})\b", BOARD_SRC)


# --------------------------------------------------------------------------
# target-size (WCAG 2.5.8, 24×24 CSS px)
# --------------------------------------------------------------------------


def test_shell_css_floors_small_targets_at_24px() -> None:
    css = _rendered_css("dashboard").replace(" ", "")
    for selector in (
        'button[aria-label^="Helpfor"]',
        'input[role="switch"]',
        '[data-testid="stBaseButton-elementToolbar"]',
    ):
        assert selector in css, selector
    # Each of those rules carries the 24px floor.
    assert css.count("min-width:24px!important") >= 3
    assert css.count("min-height:24px!important") >= 3


# --------------------------------------------------------------------------
# LOW-2 — the dashboard reads the day-scoped digest, not the whole table
# --------------------------------------------------------------------------


def test_dashboard_client_digest_reads_day_scoped_today(monkeypatch) -> None:
    from command_center.api import wave1_service
    from command_center.ui import dashboard_client

    calls: list[str] = []
    sentinel = object()

    def _today():
        calls.append("today")
        return sentinel

    def _all(*args, **kwargs):  # pragma: no cover - must not be reached
        calls.append("all")
        raise AssertionError("dashboard must not read the whole digest table")

    monkeypatch.setattr(wave1_service, "list_digest_today", _today)
    monkeypatch.setattr(wave1_service, "list_digest_items", _all)

    result = dashboard_client.InProcessDashboardClient().digest()

    assert result is sentinel
    assert calls == ["today"]
