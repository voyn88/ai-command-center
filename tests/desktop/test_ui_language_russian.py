"""Automated linguistic gate: every user-visible string in the shell is Russian.

The master requirement is a fully Russian UI. This walks the live widget tree of
the assembled shell and asserts each user-facing string (button/label text,
placeholders, tooltips, accessible names/descriptions, the window title) either
contains Cyrillic or is composed only of allowed proper nouns / abbreviations
(the product name and the conventional technical tokens that stay Latin, e.g. CI,
PR, SHA, API, URL, JSON, CPU, macOS). Object names (widget identifiers) are not
user-visible and are intentionally not checked.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import QWidget

from command_center.desktop import i18n
from command_center.desktop.pages.home import HomePage

# Latin tokens allowed to appear without Cyrillic (proper noun + conventional
# abbreviations kept Latin per the UI-language policy).
_ALLOWED_LATIN = {
    "AI", "Command", "Center",  # the product name "AI Command Center"
    "CI", "PR", "SHA", "API", "URL", "JSON", "CPU", "macOS", "QR", "PDF",
    "ID", "HTTP", "HTTPS", "Git", "GitHub",
    # Canonical product acronyms shown unchanged as project names.
    "AICC", "AIOS", "AICOS", "ESF", "AML", "CRM", "VOYN",
    # Git domain terms kept in their conventional Latin form (like "Git"),
    # clearer to a developer operator than a literal translation.
    "worktree", "Worktree",
}

_LATIN_WORD = re.compile(r"[A-Za-z]+")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# The user-visible getters we audit on every widget. objectName() is excluded on
# purpose — it is an identifier, not shown to users.
_TEXT_GETTERS = (
    "windowTitle",
    "title",  # QGroupBox et al. — a visible title, not exposed via text()
    "text",
    "placeholderText",
    "toolTip",
    "accessibleName",
    "accessibleDescription",
    "whatsThis",
)


def _is_russian_or_allowed(value: str) -> bool:
    latin_words = _LATIN_WORD.findall(value)
    if not latin_words:
        return True  # pure Cyrillic / digits / punctuation
    if _CYRILLIC.search(value):
        # mixed: every Latin run must be an allowed token (e.g. "Проект в PR")
        return all(w in _ALLOWED_LATIN for w in latin_words)
    # no Cyrillic at all: allowed only if it is purely an allowed proper noun
    return all(w in _ALLOWED_LATIN for w in latin_words)


def _collect_user_visible_strings(root: QWidget) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    widgets = [root, *root.findChildren(QWidget)]
    for w in widgets:
        for getter in _TEXT_GETTERS:
            fn = getattr(w, getter, None)
            if not callable(fn):
                continue
            try:
                value = fn()
            except TypeError:
                continue
            if isinstance(value, str) and value.strip():
                seen.append((f"{type(w).__name__}.{getter}()", value))
    return seen


def test_all_shell_strings_are_russian(shell):
    offenders = [
        (where, value)
        for where, value in _collect_user_visible_strings(shell)
        if not _is_russian_or_allowed(value)
    ]
    assert not offenders, "Непереведённые (английские) строки в UI:\n" + "\n".join(
        f"  {where}: {value!r}" for where, value in offenders
    )


def _assert_widget_tree_is_russian(root: QWidget) -> None:
    offenders = [
        (where, value)
        for where, value in _collect_user_visible_strings(root)
        if not _is_russian_or_allowed(value)
    ]
    assert not offenders, "Непереведённые строки в динамическом UI:\n" + "\n".join(
        f"  {where}: {value!r}" for where, value in offenders
    )


def test_home_dynamic_populated_state_is_russian(qtbot):
    page = HomePage()
    qtbot.addWidget(page)
    page.render_snapshot(
        {
            "projects": [{
                "id": "AIOS",
                "display_name": "AIOS",
                "repository_state": "future_state",
                "task_count": 1,
                "active_run_count": 1,
            }],
            "worktrees_by_project": {},
            "active_runs": [{
                "source": "v2",
                "run_id": "r1",
                "project": "AIOS",
                "task_type": "future_task",
                "state": "FUTURE_STATE",
            }],
            "recent_runs": [],
            "artifacts": [{"project": "AIOS", "task_type": "future_task"}],
            "reports": [{
                "run_id": "r1",
                "project": "AIOS",
                "verdict": "FUTURE_VERDICT",
            }],
            "recent_activity": [{
                "event_type": "future_event",
                "project": "AIOS",
            }],
        }
    )
    _assert_widget_tree_is_russian(page)


def test_home_dynamic_loading_and_error_states_are_russian(qtbot):
    page = HomePage()
    qtbot.addWidget(page)
    page._show_loading_state()
    _assert_widget_tree_is_russian(page)
    page._on_load_error()
    _assert_widget_tree_is_russian(page)
    assert i18n.HOME_LOAD_ERROR_DETAIL
