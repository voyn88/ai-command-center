"""Navigation and accessibility behaviour of the P2 native shell."""

from __future__ import annotations

from command_center.desktop import i18n
from command_center.desktop.sections import SECTIONS

EXPECTED_ORDER = [
    "home", "projects", "sessions", "execution", "git",
    "artifacts", "reports", "agents", "pipeline", "settings",
]
ENABLED = set(EXPECTED_ORDER)


def test_all_sections_render_in_order(shell):
    assert list(shell.sidebar.items().keys()) == EXPECTED_ORDER


def test_all_sections_are_active(shell):
    items = shell.sidebar.items()
    for key, item in items.items():
        assert item.isEnabled() == (key in ENABLED), key


def test_sections_table_matches_information_architecture():
    # The single source the sidebar reads matches the IA §2 active set.
    active = {s.key for s in SECTIONS if s.enabled}
    assert active == ENABLED


def test_default_section_is_home(shell):
    assert shell.current_section_key == "home"
    assert shell.sidebar.current_key == "home"


def test_clicking_active_item_switches_page(shell, qtbot):
    shell.sidebar.items()["projects"].click()
    assert shell.current_section_key == "projects"
    shell.sidebar.items()["settings"].click()
    assert shell.current_section_key == "settings"


def test_all_items_are_in_tab_order(shell):
    for key, item in shell.sidebar.items().items():
        assert item.focusPolicy().value != 0, key


def test_home_empty_state_navigates_to_projects(shell, qtbot):
    home = shell._pages["home"]
    assert home.section_key == "home"
    # The EmptyState's next-action button routes to Projects (IA §11).
    home_widget = shell.stack.widget(0)
    # Trigger the page's navigate signal through its EmptyState action button.
    # Find the action button and click it.
    from command_center.desktop.components.empty_state import EmptyState

    empty = home_widget.findChild(EmptyState)
    assert empty is not None and empty.action_button is not None
    empty.action_button.click()
    assert shell.current_section_key == "projects"
    assert shell.sidebar.current_key == "projects"


def test_navigation_item_accessibility(shell):
    items = shell.sidebar.items()
    # Enabled item: accessible name is the visible label.
    assert items["home"].accessibleName() == i18n.SECTION_LABELS["home"]
    # Operational item remains visible, active and named.
    sessions = items["sessions"]
    assert sessions.accessibleName() == i18n.SECTION_LABELS["sessions"]
    assert sessions.isEnabled()


def test_sidebar_has_accessible_name(shell):
    assert shell.sidebar.accessibleName() == i18n.NAV_ACCESSIBLE
