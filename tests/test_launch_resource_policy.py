"""Launch admission tests without starting a real agent."""
from types import SimpleNamespace

import pytest

from command_center import pipeline_settings
from command_center.runtime import api, db


@pytest.mark.parametrize("requested,expected", [(None, 2), (1, 1), (2, 2), (8, 2)])
def test_every_api_launch_obeys_persisted_capacity(monkeypatch, tmp_path, requested, expected):
    captured = {}

    def start_raw(**kwargs):
        captured.update(kwargs)
        return {"id": "fake"}

    monkeypatch.setattr(pipeline_settings, "load_settings", lambda root: SimpleNamespace(max_global_concurrency=2))
    monkeypatch.setattr(db, "append_run_event", lambda *args: None)
    service = api.ExecutionCenterAPI(sup=SimpleNamespace(db_path=tmp_path / "runtime.db", start_raw=start_raw))
    service.start_run(project="AIOS", repository_path="unused", task_type="implementation",
                      instruction="test", confirmed=True, max_global_concurrency=requested)
    assert captured["max_global_concurrency"] == expected


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "2"])
def test_invalid_capacity_is_rejected_before_launch(tmp_path, invalid):
    def forbidden(**kwargs):
        pytest.fail("invalid capacity launched an agent")

    service = api.ExecutionCenterAPI(sup=SimpleNamespace(db_path=tmp_path / "runtime.db", start_raw=forbidden))
    with pytest.raises(ValueError, match="positive integer"):
        service.start_run(project="AIOS", repository_path="unused", task_type="implementation",
                          instruction="test", confirmed=True, max_global_concurrency=invalid)



@pytest.mark.parametrize("same_task,expected", [(False, 2), (True, 1)])
def test_concurrent_launch_admission_has_no_duplicates_or_overflow(tmp_path, same_task, expected):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "runtime.db"
    db.migrate(path)
    tasks = [db.create_task(path, project="CRM", title=f"t{i}", task_type="implementation") for i in range(6)]
    if same_task:
        tasks = [tasks[0]] * 6
    sessions = [db.create_session(path, task_id=task["id"], project="CRM", repository_path=f"/repo/{i}")
                for i, task in enumerate(tasks)]
    barrier = Barrier(6)

    def attempt(i):
        barrier.wait(timeout=10)
        try:
            db.create_run(path, session_id=sessions[i]["id"], task_id=tasks[i]["id"], project="CRM",
                          task_type="implementation", repository_path=f"/repo/{i}", prompt="test",
                          is_resume=False, enforce_workspace_lock=True, max_global_concurrency=2)
            return "started"
        except (db.TaskAlreadyActiveError, db.GlobalConcurrencyLimitError):
            return "rejected"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert outcomes.count("started") == expected
    assert outcomes.count("rejected") == 6 - expected
    assert len(db.list_runs(path, states=db.EXECUTION_CENTER_ACTIVE_STATES)) == expected
