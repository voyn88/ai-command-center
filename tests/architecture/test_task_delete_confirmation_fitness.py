"""Fitness gate for AUDIT-W1-007 — UI task deletion stays behind confirm_dialog.

AUDIT-W1-007 closed the one-click Kanban delete. The behavioural AppTests
(``tests/test_app_streamlit.py``, ``tests/test_backlog_reconcile_panel.py``)
cover the two known surfaces, but a third surface that wired ``Удалить``
straight to ``delete_task`` would never trip them. This architecture gate
is the executable half that keeps every UI delete on the shared
``confirm_dialog`` bar — the gap the original closure shipped without
(VOYN-W0-AICC-CLOSURE-WITHOUT-A-GATE-RETRY).
"""

from __future__ import annotations

import ast

from tests.architecture import task_delete_confirmation as gate


def test_every_ui_task_delete_goes_through_confirm_dialog():
    """No ``command_center/ui`` surface may one-click delete a task."""
    problems = gate.scan_ui_tree()
    assert not problems, (
        "AUDIT-W1-007 requires every UI 'Удалить' path to open "
        "confirm_dialog and every delete_task call to sit behind "
        "render_destructive_confirmation(on_confirm=...) "
        "(docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md):\n"
        + "\n".join(problems)
    )


def test_scanner_catches_a_one_click_delete_button():
    """Mutation check: a button that deletes immediately must go red."""
    source = (
        "import streamlit as st\n"
        "from command_center.ui import legacy_task_helpers\n"
        "\n"
        "def render(task_id):\n"
        "    if st.button('Удалить'):\n"
        "        legacy_task_helpers.delete_task(task_id)\n"
    )
    tree = ast.parse(source, filename="command_center/ui/bad.py")
    button_problems = gate.find_unguarded_delete_buttons(
        tree, "command_center/ui/bad.py"
    )
    call_problems = gate.find_unguarded_delete_calls(
        tree, "command_center/ui/bad.py"
    )
    assert button_problems, "one-click delete button must be flagged"
    assert call_problems, "unguarded delete_task call must be flagged"


def test_scanner_accepts_the_confirm_dialog_shape():
    """The live task_cards / backlog_reconcile shape must stay green."""
    source = (
        "import streamlit as st\n"
        "from command_center.ui import confirm_dialog, legacy_task_helpers\n"
        "\n"
        "def render(task_id):\n"
        "    if st.button('Удалить'):\n"
        "        confirm_dialog.open_confirmation('k')\n"
        "    confirm_dialog.render_destructive_confirmation(\n"
        "        key_prefix='k',\n"
        "        dialog_title='t',\n"
        "        warning='w',\n"
        "        checkbox_label='c',\n"
        "        confirm_label='ok',\n"
        "        on_confirm=lambda: legacy_task_helpers.delete_task(task_id),\n"
        "    )\n"
    )
    tree = ast.parse(source, filename="command_center/ui/ok.py")
    assert gate.find_unguarded_delete_buttons(tree, "command_center/ui/ok.py") == []
    assert gate.find_unguarded_delete_calls(tree, "command_center/ui/ok.py") == []


def test_scanner_accepts_a_private_helper_wired_through_on_confirm():
    """backlog_reconcile's ``_delete_finding`` shape must stay green."""
    source = (
        "import streamlit as st\n"
        "from command_center import tasks_repository\n"
        "from command_center.ui import confirm_dialog\n"
        "\n"
        "def _delete_finding(root, finding):\n"
        "    tasks_repository.delete_task(root, finding.task_id)\n"
        "\n"
        "def render(root, finding):\n"
        "    clicked = st.button('Удалить')\n"
        "    if clicked:\n"
        "        confirm_dialog.open_confirmation('k')\n"
        "    confirm_dialog.render_destructive_confirmation(\n"
        "        key_prefix='k',\n"
        "        dialog_title='t',\n"
        "        warning='w',\n"
        "        checkbox_label='c',\n"
        "        confirm_label='ok',\n"
        "        on_confirm=lambda: _delete_finding(root, finding),\n"
        "    )\n"
    )
    tree = ast.parse(source, filename="command_center/ui/ok_helper.py")
    assert gate.find_unguarded_delete_buttons(tree, "command_center/ui/ok_helper.py") == []
    assert gate.find_unguarded_delete_calls(tree, "command_center/ui/ok_helper.py") == []


def test_scanner_rejects_a_delete_helper_that_bypasses_confirm_dialog():
    """A ``_delete_*`` helper not passed as on_confirm is still a gap."""
    source = (
        "from command_center import tasks_repository\n"
        "\n"
        "def _delete_finding(root, task_id):\n"
        "    tasks_repository.delete_task(root, task_id)\n"
        "\n"
        "def render(root, task_id):\n"
        "    _delete_finding(root, task_id)\n"
    )
    tree = ast.parse(source, filename="command_center/ui/bad_helper.py")
    problems = gate.find_unguarded_delete_calls(tree, "command_center/ui/bad_helper.py")
    assert problems, "unconfirmed _delete_* helper must be flagged"


def test_gate_is_not_silently_empty():
    """The UI walk actually finds the known delete surfaces."""
    files = {path.name for path in gate.iter_ui_python_files()}
    assert "task_cards.py" in files
    assert "backlog_reconcile_panel.py" in files
    assert "confirm_dialog.py" not in files
