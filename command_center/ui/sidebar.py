"""Sidebar navigation (UX-1 AppShell).

Twenty-one page handlers in one flat list is a directory listing, not
navigation: everything is equally prominent, so nothing is. Ten daily
destinations remain visible here; related and specialist handlers are
consolidated into those destinations while their old deep links keep working.
The visible destinations are grouped by *what the operator is doing*.

Grouping is presentation only. `nav_page` keeps its exact session-state key and
its values, because tests drive navigation by setting it directly and the URL
carries it; regrouping must not invalidate a bookmarked link.
"""

from __future__ import annotations

from typing import Callable

import streamlit as st

# (title, page keys, open by default). The UX analysis (task 1/2) found 17-20
# flat entries far past the 7±2 scanning limit, with three overview pages, two
# run monitors and several duplicate library pages competing for attention.
#
# The five core destinations sit in one open "Основное" group an operator can
# scan at a glance. Planning and analytics remain collapsed until needed.
# Hidden handlers preserve old bookmarked links but are not duplicated in the
# sidebar or command palette.
NAV_GROUPS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    # The core destinations the redesign keeps: overview, planning, execution,
    # projects, git. Chat/reports/generated/context are no longer their own
    # sidebar entries — they live inside the project view (task 02661825).
    ("Основное", ("dashboard", "command", "kanban", "task_deps", "execution_center", "projects", "git_center"), True),
    ("Планирование", ("waves", "create"), False),
    ("Аналитика", ("runs", "agents", "portfolio"), False),
)

# Pages whose handler is kept (for delegation / an existing deep link) but which
# are no longer their own sidebar entry — everything about a project now opens
# inside the project view as a tab: chat, generated tasks, reports, context.
HIDDEN_FROM_SIDEBAR: frozenset[str] = frozenset(
    {
        "chat",
        "generated",
        "reports",
        "context",
        # Consolidated into the main dashboard.
        "executive",
        "workspace_home",
        # Consolidated into Runs / Portfolio.
        "timeline",
        "portfolio_overview",
        # Specialist/deep-link surfaces; their useful actions are reachable
        # from Projects, Execution Center or the command flow.
        "daily_audit",
        "workspace",
        "focus",
    }
)


def _grouped(nav: dict[str, tuple[str, str]]) -> list[tuple[str, list[str], bool]]:
    """Groups filtered to the keys that actually exist, plus a catch-all.

    A page added to `nav` but forgotten here would otherwise become invisible
    and unreachable — a navigation bug no test would catch, because the page
    itself still works. Anything ungrouped lands in "Прочее" instead."""
    seen: set[str] = set()
    groups: list[tuple[str, list[str], bool]] = []
    for title, keys, expanded in NAV_GROUPS:
        present = [key for key in keys if key in nav and key not in HIDDEN_FROM_SIDEBAR]
        seen.update(present)
        if present:
            groups.append((title, present, expanded))
    # `HIDDEN_FROM_SIDEBAR` pages are intentionally omitted from the catch-all
    # too: their handler stays reachable by URL / delegation, but they are not
    # a sidebar destination (they live inside the project view).
    leftover = [key for key in nav if key not in seen and key not in HIDDEN_FROM_SIDEBAR]
    if leftover:
        groups.append(("Прочее", leftover, False))
    return groups


def _select_page(key: str) -> None:
    st.session_state["nav_page"] = key
    st.session_state["show_command_palette"] = False
    st.query_params["page"] = key


def render_sidebar(
    nav: dict[str, tuple[str, str]],
    *,
    project_count: int,
    on_open_palette: Callable[[], None],
) -> str:
    """Render the primary navigation and return the active page key."""
    # Seeded before any widget exists: Streamlit refuses to reassign a widget's
    # key afterwards, and session state does not survive a browser refresh —
    # the URL does, so that is where "which section am I in" belongs.
    if "nav_page" not in st.session_state:
        requested = st.query_params.get("page")
        if requested in nav:
            st.session_state["nav_page"] = requested
    page_key = st.session_state.get("nav_page") or next(iter(nav))

    with st.sidebar:
        st.button(
            "Поиск и команды",
            icon=":material/search:",
            shortcut="Mod+K",
            on_click=on_open_palette,
            width="stretch",
            key="open_palette_btn",
        )

        for title, keys, expanded in _grouped(nav):
            # The group holding the current page is always open, whatever its
            # default: collapsing the section you are looking at hides where
            # you are.
            with st.expander(title, expanded=expanded or page_key in keys):
                for key in keys:
                    label, icon = nav[key]
                    st.button(
                        label,
                        icon=icon,
                        key=f"nav_btn_{key}",
                        width="stretch",
                        type="primary" if key == page_key else "tertiary",
                        on_click=_select_page,
                        args=(key,),
                    )

        st.divider()
        st.caption(f"Проектов в реестре: {project_count}")
        st.caption("Локальный режим · без внешних сервисов")

    if st.query_params.get("page") != page_key:
        st.query_params["page"] = page_key
    return page_key
