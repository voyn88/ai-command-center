"""UI coverage for the backlog reconciliation panel
(`command_center.ui.backlog_reconcile_panel`).

The pure matcher is covered in `test_backlog_reconcile.py`; here we prove the
panel actually renders a finding and that its per-row buttons persist the
resolution through `tasks_repository` — moving a self-completed card to Review and
deleting a duplicate — so the Kanban page reflects the change on the next rerun.
"""
from __future__ import annotations

from streamlit.testing.v1 import AppTest

from command_center import tasks_repository


def _task(task_id, title, *, status="Backlog", goal=None, launch_status=None, created_at="2026-01-01"):
    return {
        "id": task_id,
        "title": title,
        "goal": goal or title,
        "status": status,
        "launch_status": launch_status,
        "created_at": created_at,
        "project": "AICC",
    }


def _panel_script() -> None:
    # Re-exec'd as a standalone script by AppTest, so it must be self-contained:
    # the data dir comes from AICC_DATA_DIR (the isolated_data_dir fixture), never
    # a captured closure variable. These imports live inside the function on
    # purpose — the re-exec'd script does not see this module's top-level imports.
    import os
    from pathlib import Path

    from command_center import tasks_repository
    from command_center.ui import backlog_reconcile_panel

    root = Path(os.environ["AICC_DATA_DIR"])
    tasks = tasks_repository.load_tasks(root)
    backlog_reconcile_panel.render_backlog_reconcile_panel(tasks, root, key_prefix="t")


def _run() -> AppTest:
    return AppTest.from_function(_panel_script, default_timeout=30).run()


def test_self_completed_finding_is_shown_and_moves_to_review(isolated_data_dir):
    root = isolated_data_dir
    tasks_repository.save_tasks(root, [_task("t1", "Ship the thing", launch_status="Completed")])

    at = _run()
    assert any("Ship the thing" in m.value for m in at.markdown)

    at.button(key="t_done_t1").click().run()

    reloaded = {t["id"]: t for t in tasks_repository.load_tasks(root)}
    assert reloaded["t1"]["status"] == "Review"


def test_duplicate_of_done_requires_explicit_confirmation_before_deleting(isolated_data_dir):
    root = isolated_data_dir
    tasks_repository.save_tasks(
        root,
        [
            _task("d1", "Audit Task Model Ordering and Progress", status="Done"),
            _task("o1", "Audit task-model, ordering & progress!", created_at="2026-02-01"),
        ],
    )

    at = _run()
    at = at.button(key="t_delete_o1").click().run()

    # A single click opens the confirmation dialog but must not delete
    # anything yet — the whole point of the gate is that one accidental
    # click on "Удалить" can't remove a task.
    ids = {t["id"] for t in tasks_repository.load_tasks(root)}
    assert "o1" in ids
    confirm_button = at.button(key="t_delete_o1_confirm_btn")
    assert confirm_button.disabled is True

    at = at.checkbox(key="t_delete_o1_confirmed").check().run()
    at = at.button(key="t_delete_o1_confirm_btn").click().run()

    ids = {t["id"] for t in tasks_repository.load_tasks(root)}
    assert "o1" not in ids
    assert "d1" in ids


def test_clean_backlog_reports_nothing_to_reconcile(isolated_data_dir):
    root = isolated_data_dir
    tasks_repository.save_tasks(root, [_task("a", "Alpha subsystem", goal="Build alpha subsystem")])

    at = _run()
    assert any("Чисто" in s.value for s in at.success)
