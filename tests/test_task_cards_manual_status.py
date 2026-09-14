"""Fitness gate for AUDIT-W2-008 — the task-card "Ручной статус" row.

``command_center/ui/task_cards.py`` closed AUDIT-W2-008 by making
"Приостановить"/"Возобновить"/"К перезапуску" set only a planning label
(``tasks_repository.set_manual_launch_status``); the caption under the row
says explicitly that a synchronous Claude Code run cannot be paused mid-flight
and that real cancellation lives only on the Execution Center run card. That
closure shipped no executable gate for the "only" — a test that merely reads
the resulting ``launch_status``/timeline text after a click would keep passing
if a regression made the same button *also* cancel, kill or relaunch the
underlying process, since the advisory write would still happen too.

Every real process-control seam reachable from the UI is therefore patched to
fail loudly for the duration of a click: ``ExecutionCenterAPI.request_cancel``
/``start_run`` (the API layer other cards use to control a run),
``Supervisor.cancel`` and ``subprocess.Popen`` (what ``request_cancel``/launch
bottom out in — patched too in case a regression reaches past the API layer),
``agent_runner``'s ``subprocess.run`` (used for git/workspace commands during a
launch), and ``execution_queue.enqueue_and_persist`` (the real "К перезапуску"
would need to re-enqueue if it ever became one). Only then is the resulting
state asserted, so both halves of the DoD are covered: the buttons must leave
a plan-only trace, and they must touch nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from command_center import agent_runner, execution_queue, models, storage
from command_center.runtime import api as runtime_api
from command_center.runtime import supervisor as runtime_supervisor

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_on_page(page_key: str, **extra_session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = page_key
    for key, value in extra_session_state.items():
        at.session_state[key] = value
    at.run()
    return at


def _seed_task(**overrides) -> dict:
    data_dir = Path(os.environ["AICC_DATA_DIR"])
    task = {
        "id": "seeded-task-1",
        "project": "AIOS",
        "title": "Seeded task for manual-status AppTest",
        "task_type": "implementation",
        "status": "In Progress",
        "priority": "Medium",
        "owner": "",
        "estimate_hours": 0.0,
        "depends_on": [],
        "launch_status": "Running",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    task.update(models.default_task_workflow_fields())
    task.update(overrides)
    storage.atomic_write_json(data_dir / "tasks.json", [task])
    return task


def _forbid_process_control(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every real process-control seam to a hard failure; return the call log."""
    calls: list[str] = []

    def _forbid(name):
        def _fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"a manual status action must never call {name}")

        return _fail

    monkeypatch.setattr(
        runtime_api.ExecutionCenterAPI, "request_cancel", _forbid("ExecutionCenterAPI.request_cancel")
    )
    monkeypatch.setattr(runtime_api.ExecutionCenterAPI, "start_run", _forbid("ExecutionCenterAPI.start_run"))
    monkeypatch.setattr(runtime_supervisor.Supervisor, "cancel", _forbid("Supervisor.cancel"))
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", _forbid("runtime.supervisor subprocess.Popen"))
    monkeypatch.setattr(agent_runner.subprocess, "run", _forbid("agent_runner subprocess.run"))
    monkeypatch.setattr(
        execution_queue, "enqueue_and_persist", _forbid("execution_queue.enqueue_and_persist")
    )
    return calls


@pytest.mark.parametrize(
    "action_key, expected_status, expected_note_fragment",
    [
        ("action_pause", "Requires Attention", "приостановлено"),
        ("action_resume", "Ready", "возобновлено"),
        ("action_restart", "Ready", "перезапуска"),
    ],
)
def test_manual_status_button_only_sets_advisory_status(
    monkeypatch, action_key, expected_status, expected_note_fragment
):
    calls = _forbid_process_control(monkeypatch)
    task = _seed_task()

    at = _at_on_page("kanban")
    assert not at.exception

    button = next(b for b in at.button if b.key == f"kanban_{task['id']}_{action_key}")
    at = button.click().run()

    assert not at.exception
    assert calls == [], f"process-control seam(s) touched by a manual status click: {calls}"

    tasks_on_disk = storage.read_json(Path(os.environ["AICC_DATA_DIR"]) / "tasks.json", [])
    updated = next(t for t in tasks_on_disk if t["id"] == task["id"])
    assert updated["launch_status"] == expected_status
    timeline = updated.get("timeline") or []
    assert timeline, "the click must record a timeline event"
    assert expected_note_fragment in timeline[-1].get("message", "")


def test_gate_detects_a_regression_that_also_cancels_the_run(monkeypatch):
    """Mutation check: the spy mechanism itself must fire, not just be wired.

    Without it, a regression that made the pause button *also* call
    ``ExecutionCenterAPI.request_cancel`` would still leave ``launch_status``
    looking correct after the click, and pass unnoticed.
    """
    calls = _forbid_process_control(monkeypatch)

    with pytest.raises(AssertionError):
        runtime_api.ExecutionCenterAPI.request_cancel(object(), "run-1", confirmed=True)
    assert calls == ["ExecutionCenterAPI.request_cancel"]

    with pytest.raises(AssertionError):
        runtime_supervisor.Supervisor.cancel(object(), "run-1", confirmed=True)
    assert calls == ["ExecutionCenterAPI.request_cancel", "Supervisor.cancel"]
