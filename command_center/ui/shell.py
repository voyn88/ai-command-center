"""AppShell (UX-1): composes the TopCommandBar and Sidebar into one entry
point so `app.py` stays thin. Content Area/Inspector are handled separately
by `command_center.ui.content_area` around the page-routing dispatch, since
that dispatch is per-page, not shell-level.

Page config is *not* configured here (VOYN-W0-AICC-CONSOLE-NO-AUTH): it must
be the first Streamlit command of a run if called at all, and the identity
gate (`command_center.ui.console_identity.require_identity`) now runs before
this function, on both the login and the authenticated branch, so it owns
that single call instead.
"""

from __future__ import annotations

from typing import Callable

from command_center.ui import accessibility, sidebar, theme, top_bar


def render_shell(
    *,
    title: str,
    caption: str,
    nav: dict[str, tuple[str, str]],
    project_count: int,
    on_open_palette: Callable[[], None],
    tasks_by_id: dict[str, dict] | None = None,
    api=None,
) -> str:
    """Render the top command bar and the sidebar.

    Returns the active page key (`nav_page` session-state value).
    """
    # App-wide CSS (UX-2a): fragment fade-in + card hover transitions. Emitted
    # every run so it survives reruns (see theme.inject_global_css docstring).
    theme.inject_global_css()
    accessibility.repair_streamlit_shell_semantics()

    top_bar.render_top_bar(
        title,
        caption,
        on_open_palette=on_open_palette,
        tasks_by_id=tasks_by_id,
        api=api,
    )

    return sidebar.render_sidebar(nav, project_count=project_count, on_open_palette=on_open_palette)
