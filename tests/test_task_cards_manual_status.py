"""Gate for Founder Functional Audit 9761459, row AUDIT-W2-008.

The row's finding: the Kanban task card's Pause/Resume/Restart buttons read as
if they controlled a running agent process, when a synchronous Claude Code run
cannot in fact be paused mid-flight. The fix (`command_center/ui/task_cards.py`)
relabeled the row "Ручной статус (метка плана, не управление процессом)" and
added a caption spelling out that real cancellation lives on the Execution
Center run card — i.e. it made the buttons honestly *advisory*.

That fix shipped with no test at all (`git grep -l task_cards tests/` returns
nothing before this file), so a later edit could silently revert the caption or
rewire a button to imply real process control without any test going red. This
module is that gate: it pins both halves — the buttons only ever write the
advisory `launch_status` label via `tasks_repository.set_manual_launch_status`,
and the disclaimer text stays on the card.
"""

from __future__ import annotations

import os
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import models, storage

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_on_kanban() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "kanban"
    at.run()
    return at


def _seed_task(**overrides) -> dict:
    data_dir = Path(os.environ["AICC_DATA_DIR"])
    task = {
        "id": "manual-status-task-1",
        "project": "AIOS",
        "title": "Manual status gate task",
        "task_type": "implementation",
        "status": "Backlog",
        "priority": "Medium",
        "owner": "",
        "estimate_hours": 0.0,
        "depends_on": [],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    task.update(models.default_task_workflow_fields())
    task.update(overrides)
    storage.atomic_write_json(data_dir / "tasks.json", [task])
    return task


def _read_task(task_id: str) -> dict:
    tasks_path = Path(os.environ["AICC_DATA_DIR"]) / "tasks.json"
    tasks = storage.read_json(tasks_path, [])
    return next(task for task in tasks if task["id"] == task_id)


def _latest_timeline_message(task: dict) -> str:
    timeline = task.get("timeline") or []
    assert timeline, "expected the manual-status action to append a timeline event"
    return timeline[-1].get("message", "")


def test_manual_status_row_is_labeled_as_a_planning_label_not_process_control():
    _seed_task()
    at = _at_on_kanban()
    assert not at.exception

    markdowns = [m.value for m in at.markdown]
    assert any(
        "Ручной статус" in value and "не управление процессом" in value
        for value in markdowns
    ), "the honest-framing heading must stay on the card"

    captions = [c.value for c in at.caption]
    assert any(
        "нельзя приостановить на лету" in value and "Live Execution Center" in value
        for value in captions
    ), "the disclaimer explaining these are labels, not control, must stay on the card"


def test_pause_button_only_sets_the_advisory_launch_status():
    task = _seed_task()
    at = _at_on_kanban()
    assert not at.exception

    at = at.button(key=f"kanban_{task['id']}_action_pause").click().run()
    assert not at.exception

    updated = _read_task(task["id"])
    assert updated["launch_status"] == "Requires Attention"
    assert "приостановлено" in _latest_timeline_message(updated)


def test_resume_button_only_sets_the_advisory_launch_status():
    task = _seed_task(launch_status="Requires Attention")
    at = _at_on_kanban()
    assert not at.exception

    at = at.button(key=f"kanban_{task['id']}_action_resume").click().run()
    assert not at.exception

    updated = _read_task(task["id"])
    assert updated["launch_status"] == "Ready"
    assert "возобновлено" in _latest_timeline_message(updated)


def test_restart_button_only_sets_the_advisory_launch_status():
    task = _seed_task(launch_status="Requires Attention")
    at = _at_on_kanban()
    assert not at.exception

    at = at.button(key=f"kanban_{task['id']}_action_restart").click().run()
    assert not at.exception

    updated = _read_task(task["id"])
    assert updated["launch_status"] == "Ready"
    assert "перезапуска" in _latest_timeline_message(updated)
