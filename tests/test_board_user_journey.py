"""End-to-end user journeys through the rebuilt Live Execution Center, driven
through `app.py` with `streamlit.testing.v1.AppTest` exactly as an operator
would drive the UI.

Two journeys, both against the fake executor (no real Claude CLI, no API
spend):

1. **Create → board → launch → running** — a task created from the console
   appears on the board, launches through the queue, and shows as a live run.
2. **Attention → fix** — a failed run surfaces in the triage panel with a
   concrete reason, and the operator's "Исправить" relaunches it as a new
   attempt.
"""

from __future__ import annotations

import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import tasks_repository
from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
APP_ROOT = Path(__file__).resolve().parent.parent


def _at(page_key: str = "execution_center", **session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["nav_page"] = page_key
    for key, value in session_state.items():
        at.session_state[key] = value
    at.run()
    return at


def _wait_until_finalized(db_path, run_id: str, *, timeout: float = 30.0) -> None:
    """Wait for the run's *last* write, not for the first one visible.

    The relaunch below is supervised by the board's own `ExecutionCenterAPI`
    singleton, not by this test's `api`, so `Supervisor.wait_for_run` (an
    in-memory registry private to the launching instance) reads "already
    settled" here and waits for nothing. The durable counterpart is
    `finalized_at`, and it is written *after* the report row — deliberately, so
    that "finalized" means the report and the auto-commit already happened (see
    `db.execution.mark_run_finalized`).

    This used to wait on `db.get_report(...) is not None` instead, and that
    marker is true while the supervising daemon thread is still finishing:
    still writing `finalized_at`, still holding `runtime.db-wal`/`-shm` open
    under `AICC_DATA_DIR`. The test then returned into a fixture that removes
    that directory, and roughly once in twenty runs SQLite recreated the WAL
    pair mid-`rmtree` — `OSError: [Errno 39] Directory not empty`, an ERROR at
    teardown, and a red shard (VOYN-W0-AICC-FLAKY-TEST-DATA-DIR-TEARDOWN-RACE).

    `db.wait_for_run_finalized` is deliberately *not* used: it waits only for
    the `finalized_at` stamp of a run that is already terminal, and returns the
    row untouched the moment it sees a non-terminal state (see its docstring —
    a caller in another process must not read "still RUNNING" as a reason to
    block). The run here is still `RUNNING` when the relaunch returns, so that
    call would come straight back and assert on a run that had not even
    started finishing. Polling the same durable marker covers the whole
    progression — `RUNNING` → terminal → finalized — which is the one that has
    to be over before the data dir can be removed.
    """
    deadline = time.monotonic() + timeout
    while True:
        run = runtime_db.get_run(db_path, run_id)
        assert run is not None, f"run {run_id!r} disappeared while waiting for it to finish"
        if run.get("finalized_at"):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"run {run_id!r} was not finalized within {timeout}s "
                f"(state={run['state']!r})"
            )
        time.sleep(0.05)


# --------------------------------------------------------------------------
# Journey 1 — create a task from the console, see it, launch it, watch it run
# --------------------------------------------------------------------------


def test_create_task_from_console_then_it_appears_and_launches(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)

    # Create a task through the console's inline create panel.
    at = _at(exec_board_open_panel="create")
    assert not at.exception
    at.selectbox(key="console_create_project").select("AIOS").run()
    at.text_input(key="console_create_title").set_value("Пройти путь до конца").run()
    submit = next(b for b in at.button if b.label == "Создать")
    at = submit.click().run()
    assert not at.exception

    tasks = tasks_repository.load_tasks(APP_ROOT)
    created = next((t for t in tasks if t.get("title") == "Пройти путь до конца"), None)
    assert created is not None, "task created from the console must be persisted"

    # Give it the configured workspace so the launch gate is satisfied, then
    # launch it via the board's own confirmed launch path.
    api = runtime_api.ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="review",
        instruction="do the work",
        confirmed=True,
        task_id=created["id"],
        launch_source="test",
    )

    # The board renders the live run without error, in the running bucket.
    at = _at()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    markdown = " ".join(str(m.value) for m in at.markdown)
    assert "Выполняется" in markdown  # the running section/tile
    # The run's own status caption is present (live card).
    assert "claude_code" in captions or "claude_code" in markdown

    # Drive the run to a terminal state so the fake process is not left behind.
    # (The journey under test is create -> appear -> launch -> shown running,
    # all asserted above; the report-writing tail is covered by the runtime's
    # own supervisor tests.)
    api.supervisor.wait_for_run(run["id"], timeout=15)


# --------------------------------------------------------------------------
# Journey 2 — a failed run is triaged and relaunched from the attention panel
# --------------------------------------------------------------------------


def test_attention_triage_fix_relaunches_a_failed_task(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)
    api = runtime_api.ExecutionCenterAPI()

    # A task with a real workspace, and a failed run against it.
    task = tasks_repository.create_task(
        APP_ROOT, "AIOS", "Упавшая задача", "review", "Next",
        workspace_path=str(git_repo), executor="claude_code",
    )
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "10"
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="review",
        instruction="p", confirmed=True, timeout_seconds=1, task_id=task["id"],
    )
    final = api.supervisor.wait_for_run(run["id"], timeout=15)
    assert final["state"] == "FAILED"

    # The board shows it in the attention triage with a concrete reason.
    at = _at()
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Что не так" in errors  # the triage reason box
    markdown = " ".join(str(m.value) for m in at.markdown)
    assert "Что делать" in markdown  # the suggested action

    # Operator clicks the per-row "Исправить" — no instruction to type, the
    # agent works out the fix from the failure carried into its prompt.
    fake_claude.pop("FAKE_CLAUDE_EXTRA_SLEEP", None)
    fix_btn = next(
        (b for b in at.button if b.key and b.key.startswith("exec_attention_fix_one_")), None
    )
    assert fix_btn is not None, "each attention row must carry an Исправить button"
    at = fix_btn.click().run()
    assert not at.exception

    # A new run now exists for the task (the relaunch), beyond the failed one.
    runs_for_task = [r for r in runtime_db.list_runs(api.db_path, limit=50) if r.get("task_id") == task["id"]]
    assert len(runs_for_task) >= 2, "Исправить must create a new attempt for the task"
    newest = max(runs_for_task, key=lambda r: r.get("created_at") or "")
    assert newest["task_type"] == "remediation", (
        "Исправить must launch a write-capable remediation attempt even when "
        "the failed task itself was a read-only review"
    )
    _wait_until_finalized(api.db_path, newest["id"])


# --------------------------------------------------------------------------
# Journey 3 — the capacity panel renders and reflects a live run
# --------------------------------------------------------------------------


def test_capacity_panel_reflects_a_running_agent(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)
    api = runtime_api.ExecutionCenterAPI()
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="review",
        instruction="p", confirmed=True,
    )
    try:
        at = _at()
        assert not at.exception
        markdown = " ".join(str(m.value) for m in at.markdown)
        captions = " ".join(c.value for c in at.caption)
        assert "Загрузка" in markdown
        assert "claude_code" in captions  # per-agent line
    finally:
        api.request_cancel(run["id"], confirmed=True)
        # `cancel()` only signals the process group; the supervising daemon
        # thread still has to persist the terminal state, the report and
        # `finalized_at` — all under `AICC_DATA_DIR`. Wait for it, so the run
        # is not still writing there when the data-dir fixture removes it.
        api.supervisor.wait_for_run(run["id"], timeout=15)
