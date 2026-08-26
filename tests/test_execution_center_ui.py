"""Streamlit AppTest coverage for the Live Execution Center v2 dashboard —
a thin UI consumer of the frozen v2 runtime (`command_center.runtime`) plus
the new projection/reconciliation/task-sync modules under
`command_center/runtime/`. Every test here drives `app.py` through
`streamlit.testing.v1.AppTest`, the same harness `tests/test_app_streamlit.py`
uses for the rest of the app, plus the v2 runtime's own `fake_claude`/
`configure_project_repo`/`git_repo` fixtures (`tests/conftest.py`) so no test
launches the real Claude Code CLI or spends API credits.

`st.cache_resource` (used by `app.py`'s `get_execution_center_api()`
singleton) caches process-wide, not per-`AppTest`-instance — `conftest.py`'s
`isolated_data_dir` (autouse) clears that cache every time it resets
`AICC_DATA_DIR`, so each test here still gets a `Supervisor` constructed
fresh against its own isolated data dir without needing its own fixture.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db
from command_center.runtime import identity
from command_center.runtime import session_view

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_on_page(page_key: str, **extra_session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = page_key
    for key, value in extra_session_state.items():
        at.session_state[key] = value
    at.run()
    return at


def _launch_via_ui(
    at: AppTest, *, project: str = "AIOS", task_type: str = "review", instruction: str = "do the thing"
) -> AppTest:
    """Fill in and submit the ad-hoc launch form exactly as a user would, so
    the run ends up owned by *this* `AppTest` session's cached Supervisor
    instance (required for cancellation — see `Supervisor.cancel`'s
    docstring)."""
    at.selectbox(key="exec_center_launch_project").select(project).run()
    at.selectbox(key="exec_center_launch_task_type").select(task_type).run()
    at.text_area(key="exec_center_launch_instruction").set_value(instruction).run()
    at.checkbox(key="exec_center_launch_confirm").check().run()
    at = at.button(key="exec_center_launch_btn").click().run()
    return at.run()


def _most_recent_run_id(db_path) -> str:
    runs = runtime_db.list_runs(db_path, limit=1)
    assert runs, "expected at least one run row in runtime.db"
    return runs[0]["id"]


def _wait_for_report(db_path, run_id: str, *, timeout: float = 10.0) -> None:
    """Block until `Supervisor._supervise`'s background thread has fully
    finished a run — not just until `run.state` turns terminal (set partway
    through that same thread, *before* report-saving), but until its report
    row exists (the last DB write that thread makes, right before it exits).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime_db.get_report(db_path, run_id) is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"run {run_id!r} did not finish in the background within {timeout}s")


# --------------------------------------------------------------------------
# 1. Page renders and navigation entry exists
# --------------------------------------------------------------------------


def test_execution_center_page_renders_and_nav_entry_exists():
    at = _at_on_page("execution_center")
    assert not at.exception
    assert at.subheader[0].value == "Live Execution Center"

    # Nav is grouped buttons now, not one flat radio.
    assert any(b.key == "nav_btn_execution_center" for b in at.sidebar.button)


def test_execution_center_provider_selector_defaults_to_claude_only():
    """Fail-closed: a project with no explicit allow-list offers Claude only,
    never Codex, even though the Codex provider exists in the registry."""
    at = _at_on_page("execution_center")
    selector = at.selectbox(key="exec_center_launch_executor")
    assert selector.options == ["Claude Code"]
    assert selector.value == "claude_code"


def test_execution_center_provider_selector_exposes_codex_only_when_authorized(fake_codex):
    from command_center import models, project_config

    # The launch form defaults to the first project id; authorize Codex there.
    project_config.save_allowed_agents(models.PROJECT_IDS[0], ["claude_code", "codex"])
    at = _at_on_page("execution_center")
    selector = at.selectbox(key="exec_center_launch_executor")
    assert selector.options == ["Claude Code", "Codex CLI"]
    assert selector.value == "claude_code"


def _assert_execution_center_page_rendered(at: AppTest) -> None:
    assert not at.exception
    assert at.subheader[0].value == "Live Execution Center"
    assert any("Запустить новый прогон" in status.label for status in at.status)


def test_api_singleton_not_recreated_across_reruns(monkeypatch):
    construct_calls = []
    original_init = runtime_api.ExecutionCenterAPI.__init__

    def counting_init(self, *args, **kwargs):
        construct_calls.append(1)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(runtime_api.ExecutionCenterAPI, "__init__", counting_init)

    at = _at_on_page("execution_center")
    _assert_execution_center_page_rendered(at)
    assert len(construct_calls) == 1

    for _ in range(3):
        at = at.run()
        _assert_execution_center_page_rendered(at)
        assert len(construct_calls) == 1, "ExecutionCenterAPI must not be reconstructed across reruns"


# --------------------------------------------------------------------------
# 2. Launch controls call the public runtime API with expected validated inputs
# --------------------------------------------------------------------------


def test_launch_calls_start_run_with_expected_inputs(monkeypatch, git_repo, configure_project_repo):
    configure_project_repo("AIOS", git_repo)
    captured: dict = {}

    def fake_start_run(self, **kwargs):
        captured.update(kwargs)
        return {"id": "fake-run-id", "state": "RUNNING", "session_id": "s1"}

    monkeypatch.setattr(runtime_api.ExecutionCenterAPI, "start_run", fake_start_run)

    at = _at_on_page("execution_center")
    at.selectbox(key="exec_center_launch_project").select("AIOS").run()
    at.selectbox(key="exec_center_launch_task_type").select("review").run()
    at.text_area(key="exec_center_launch_instruction").set_value("do the thing").run()
    at.checkbox(key="exec_center_launch_confirm").check().run()
    at = at.button(key="exec_center_launch_btn").click().run()

    assert not at.exception
    assert captured["project"] == "AIOS"
    assert captured["task_type"] == "review"
    assert captured["instruction"] == "do the thing"
    assert captured["confirmed"] is True
    assert captured["repository_path"] == str(git_repo)
    assert captured["timeout_seconds"] == runtime_api.DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# 3 & 4. Sensitive (BANK/LEGAL) confirmation gate
# --------------------------------------------------------------------------


def test_sensitive_launch_blocked_without_extra_confirmation(monkeypatch, git_repo, configure_project_repo):
    """A forged click without the sensitivity acknowledgement must never launch.

    Layered gate, outermost first: streamlit >= 1.61 enforces `disabled`
    server-side — an incoming value for a disabled widget is discarded at
    registration against the current run's `disabled=` predicate
    (`streamlit/runtime/state/widgets.py`), so the forced `.click()` below is
    inert before app code even sees it. The app's own `if not ready` re-check
    ("Запуск заблокирован: подтвердите все необходимые пункты") stays in
    `app.py` as defense in depth for the day the `disabled=` predicate and
    `ready` diverge, but while they match it is unreachable by any client
    message — on streamlit < 1.61 (where AppTest forged clicks did land) it was
    the layer this test exercised directly. Either way the invariant asserted
    here is the same: no `start_run`, no launch confirmation, gate still
    engaged."""
    configure_project_repo("BANK", git_repo)
    calls: list[dict] = []
    monkeypatch.setattr(
        runtime_api.ExecutionCenterAPI,
        "start_run",
        lambda self, **kwargs: (calls.append(kwargs), {"id": "x", "state": "RUNNING", "session_id": "s"})[1],
    )

    at = _at_on_page("execution_center")
    at.selectbox(key="exec_center_launch_project").select("BANK").run()
    at.selectbox(key="exec_center_launch_task_type").select("review").run()
    at.text_area(key="exec_center_launch_instruction").set_value("sensitive task").run()
    at.checkbox(key="exec_center_launch_confirm").check().run()

    launch_button = at.button(key="exec_center_launch_btn")
    assert launch_button.disabled is True  # the UI-level gate is engaged
    at = launch_button.click().run()

    assert not calls, "start_run must not be called for a sensitive project without the extra confirmation"
    assert any("чувствительный" in w.value for w in at.warning)
    # The forged click changed nothing: no launch confirmation appeared and the
    # button is still disabled. (No assertion on the app-level "Запуск
    # заблокирован" message: on streamlit >= 1.61 the forged click is discarded
    # by the framework before that re-check can run.)
    assert not any("Запуск создан" in s.value for s in at.success)
    assert at.button(key="exec_center_launch_btn").disabled is True


def test_sensitive_launch_accepted_with_confirmation(monkeypatch, git_repo, configure_project_repo):
    configure_project_repo("BANK", git_repo)
    calls: list[dict] = []

    def fake_start_run(self, **kwargs):
        calls.append(kwargs)
        return {"id": "x", "state": "RUNNING", "session_id": "s"}

    monkeypatch.setattr(runtime_api.ExecutionCenterAPI, "start_run", fake_start_run)

    at = _at_on_page("execution_center")
    at.selectbox(key="exec_center_launch_project").select("BANK").run()
    at.selectbox(key="exec_center_launch_task_type").select("review").run()
    at.text_area(key="exec_center_launch_instruction").set_value("sensitive task").run()
    at.checkbox(key="exec_center_launch_confirm").check().run()
    at.checkbox(key="exec_center_launch_sensitivity_ack").check().run()
    at = at.button(key="exec_center_launch_btn").click().run()

    assert len(calls) == 1
    assert calls[0]["project"] == "BANK"
    assert calls[0]["confirmed"] is True


# --------------------------------------------------------------------------
# 5. Non-blocking launch; Running status is shown as a card
# --------------------------------------------------------------------------


@pytest.mark.serial  # real subprocess + DB running-state timing; flaky when xdist saturates all cores
def test_launch_is_nonblocking_and_running_status_is_displayed(git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)

    at = _at_on_page("execution_center")
    at.selectbox(key="exec_center_launch_project").select("AIOS").run()
    at.selectbox(key="exec_center_launch_task_type").select("review").run()
    at.text_area(key="exec_center_launch_instruction").set_value("do the thing").run()
    at.checkbox(key="exec_center_launch_confirm").check().run()

    started = time.monotonic()
    at = at.button(key="exec_center_launch_btn").click().run()
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"launch appears to have blocked for {elapsed:.2f}s (fake_claude sleeps 5s before exiting)"

    at = at.run()
    assert not at.exception
    assert any(f"Статус: **{session_view.STATUS_RUNNING}**" in c.value for c in at.caption)

    run_id = _most_recent_run_id(runtime_db.resolve_db_path())
    at.checkbox(key=f"exec_card_cancel_ack_{run_id}").check().run()
    at.button(key=f"exec_card_cancel_btn_{run_id}").click().run()
    _wait_for_report(runtime_db.resolve_db_path(), run_id)


# --------------------------------------------------------------------------
# 6 & 7. Cancellation requires confirmation, calls the public API, and is
# only ever offered while the run is actually Running.
# --------------------------------------------------------------------------


def test_cancel_requires_confirmation_before_calling_api(monkeypatch, git_repo, configure_project_repo, fake_claude):
    hold_file = git_repo.parent / "fake-claude.hold"
    hold_file.touch()
    fake_claude["FAKE_CLAUDE_HOLD_FILE"] = str(hold_file)
    configure_project_repo("AIOS", git_repo)

    cancel_calls: list[tuple] = []
    original_cancel = runtime_api.ExecutionCenterAPI.request_cancel

    def spy_cancel(self, run_id, **kwargs):
        cancel_calls.append((run_id, kwargs))
        return original_cancel(self, run_id, **kwargs)

    monkeypatch.setattr(runtime_api.ExecutionCenterAPI, "request_cancel", spy_cancel)

    run_id = None
    db_path = runtime_db.resolve_db_path()
    try:
        at = _at_on_page("execution_center")
        at = _launch_via_ui(at)
        run_id = _most_recent_run_id(db_path)
        cancel_btn_key = f"exec_card_cancel_btn_{run_id}"

        at = at.button(key=cancel_btn_key).click().run()
        assert not cancel_calls, "request_cancel must not be called before the cancel checkbox is confirmed"

        at.checkbox(key=f"exec_card_cancel_ack_{run_id}").check().run()
        at = at.button(key=cancel_btn_key).click().run()
        assert cancel_calls == [(run_id, {"confirmed": True})]
    finally:
        # On an assertion failure, releasing the hold lets the fake process
        # exit naturally instead of leaking a background Supervisor thread.
        hold_file.unlink(missing_ok=True)
        if run_id is not None:
            _wait_for_report(db_path, run_id)


@pytest.mark.parametrize("state", ["PREPARED", "QUEUED"])
def test_cancel_action_hidden_for_non_running_states(state):
    api = runtime_api.ExecutionCenterAPI()
    task = runtime_db.create_task(api.db_path, project="AIOS", title="t", task_type="review")
    session = runtime_db.create_session(api.db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = runtime_db.create_run(
        api.db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="review",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )
    if state == "QUEUED":
        run = runtime_db.update_run_state(api.db_path, run["id"], expected_version=run["version"], new_state="QUEUED")

    at = _at_on_page("execution_center")
    assert not at.exception
    assert not any(cb.key == f"exec_card_cancel_ack_{run['id']}" for cb in at.checkbox)
    assert not any(b.key == f"exec_card_cancel_btn_{run['id']}" for b in at.button)


@pytest.mark.serial  # real subprocess + DB running-state timing; flaky when xdist saturates all cores
def test_cancel_action_visible_while_running(git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)

    at = _at_on_page("execution_center")
    at = _launch_via_ui(at)
    run_id = _most_recent_run_id(runtime_db.resolve_db_path())

    assert any(cb.key == f"exec_card_cancel_ack_{run_id}" for cb in at.checkbox)
    assert any(b.key == f"exec_card_cancel_btn_{run_id}" for b in at.button)

    at.checkbox(key=f"exec_card_cancel_ack_{run_id}").check().run()
    at.button(key=f"exec_card_cancel_btn_{run_id}").click().run()
    _wait_for_report(runtime_db.resolve_db_path(), run_id)


# --------------------------------------------------------------------------
# 8. UI eventually reflects Cancelled after runtime persistence changes
# --------------------------------------------------------------------------


def test_cancelled_status_eventually_displayed(git_repo, configure_project_repo, fake_claude):
    hold_file = git_repo.parent / "fake-claude-cancelled-status.hold"
    hold_file.touch()
    fake_claude["FAKE_CLAUDE_HOLD_FILE"] = str(hold_file)
    configure_project_repo("AIOS", git_repo)

    db_path = runtime_db.resolve_db_path()
    run_id = None
    try:
        at = _at_on_page("execution_center")
        at = _launch_via_ui(at)
        run_id = _most_recent_run_id(db_path)

        cancel_ack_key = f"exec_card_cancel_ack_{run_id}"
        cancel_btn_key = f"exec_card_cancel_btn_{run_id}"

        # Re-render in a poll loop until the cancel controls are visible.  On a
        # loaded CI host the background supervisor thread may not have advanced
        # the DB row to RUNNING by the time the first render completes, so the
        # cancel section (only shown for live-process states) may be absent.
        # This also serves as the definitive check that the hold file is
        # keeping fake_claude alive: if the process had already exited the
        # cancel controls would never appear.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(cb.key == cancel_ack_key for cb in at.checkbox):
                break
            at = at.run()
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"Cancel controls did not appear for run {run_id!r} within 15 s — "
                "the fake process may have exited before the cancel could be requested"
            )

        at.checkbox(key=cancel_ack_key).check().run()
        at = at.button(key=cancel_btn_key).click().run()
        # supervisor.cancel() blocks until done_event is set (report saved), so
        # by the time the button-click render returns the run is fully terminal.
        _wait_for_report(db_path, run_id)
        final_run = runtime_db.get_run(db_path, run_id)
        assert final_run is not None
        assert final_run["state"] == "CANCELLED"
        # Start a new render after the cancellation thread has committed its final
        # report. Reusing the in-flight AppTest session races its Streamlit script
        # runner on slower CI hosts and can retain an obsolete widget tree.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            at = _at_on_page("execution_center")
            if _shows_status(at, session_view.STATUS_CANCELLED):
                return
            time.sleep(0.2)

        raise AssertionError("durably cancelled run was not projected into the execution center")
    finally:
        hold_file.unlink(missing_ok=True)
        if run_id is not None:
            _wait_for_report(db_path, run_id)


# --------------------------------------------------------------------------
# 9. Terminal run status remains visible after page rerun / revisit
# --------------------------------------------------------------------------


def test_terminal_status_persists_across_page_revisit(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)

    api = runtime_api.ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="review", instruction="p", confirmed=True
    )
    final = api.supervisor.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "COMPLETED"

    at = _at_on_page("dashboard")
    assert not at.exception

    at2 = _at_on_page("execution_center")
    assert not at2.exception
    assert _shows_status(at2, session_view.STATUS_COMPLETED)


# --------------------------------------------------------------------------
# 10. Failed status displays the last error
# --------------------------------------------------------------------------


def test_failed_status_displays_last_error(git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "10"
    configure_project_repo("AIOS", git_repo)

    api = runtime_api.ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="review",
        instruction="p",
        confirmed=True,
        timeout_seconds=1,
    )
    final = api.supervisor.wait_for_run(run["id"], timeout=15)
    assert final["state"] == "FAILED"
    assert final["failure_reason"] == "timeout"

    at = _at_on_page("execution_center")
    assert not at.exception
    assert _shows_status(at, session_view.STATUS_FAILED)
    assert _shows_reason(at, "timeout")


# --------------------------------------------------------------------------
# 11. Existing app navigation and existing pages remain functional
# --------------------------------------------------------------------------


def test_existing_pages_still_render_without_exception():
    for page_key in ("dashboard", "agents", "runs", "executive"):
        at = _at_on_page(page_key)
        assert not at.exception, f"page {page_key!r} raised: {at.exception}"


# --------------------------------------------------------------------------
# 12. Section bucketing: a hand-built run in each state renders under the
# expected dashboard section, reusing the reconciliation-test style of
# building `run` rows directly against `db` (tests/test_runtime_reconciliation.py).
# --------------------------------------------------------------------------


def _make_run_row(
    db_path, *, state: str, cancel_requested: bool = False, failure_reason: str | None = None,
    pid: int | None = None, first_output_at: str | None = "2026-01-01T00:00:01",
) -> dict:
    """`pid=None` (the default) is correct for every terminal state — only a
    hand-built `RUNNING` row needs a *real, currently-alive* pid (see the
    two callers below that spawn a real throwaway process), otherwise
    `Supervisor.reconcile()` — which the dashboard now calls on every render,
    exactly as the mission requires — will honestly (and correctly)
    reclassify a "Running" row with no provable process behind it as
    `INTERRUPTED`, per its own conservative "never assume Running because a
    field says so" contract."""
    task = runtime_db.create_task(db_path, project="AIOS", title="Bucketing task", task_type="review")
    session = runtime_db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="review",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )
    if state != "PREPARED":
        run = runtime_db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    if state not in ("PREPARED", "QUEUED"):
        running_fields = {"started_at": "2026-01-01T00:00:00"}
        # A handshaked running run (first output already received) displays as
        # `Running`; passing `first_output_at=None` leaves it in the
        # awaiting-handshake window, which displays as `Starting`.
        if first_output_at is not None:
            running_fields["first_output_at"] = first_output_at
        if pid is not None:
            running_fields["pid"] = pid
            recorded_identity = identity.capture_identity(pid)
            running_fields["process_start_identity"] = recorded_identity.as_string() if recorded_identity else None
        run = runtime_db.update_run_state(
            db_path, run["id"], expected_version=run["version"], new_state="RUNNING", fields=running_fields
        )
    if cancel_requested:
        run = runtime_db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"cancel_requested": 1})
    if state not in ("PREPARED", "QUEUED", "RUNNING"):
        fields = {"completed_at": "2026-01-01T00:01:00"}
        if failure_reason:
            fields["failure_reason"] = failure_reason
        run = runtime_db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state=state, fields=fields)
    return run



def _shows_status(at, status: str) -> bool:
    """Whether a run with `status` is visible, however it is rendered.

    Live runs render as cards with a status caption; finished ones render in a
    collapsed table, because a wall of stacked cards for finished work buries
    the runs that actually need attention. Both satisfy what these tests are
    about — the run is classified and shown."""
    if any(f"Статус: **{status}**" in c.value for c in at.caption):
        return True
    return any(status in element.value.to_string() for element in at.dataframe)


def _shows_reason(at, fragment: str) -> bool:
    """Whether a failure reason is visible, as an error box or a table cell."""
    if any(fragment in e.value for e in at.error):
        return True
    return any(fragment in element.value.to_string() for element in at.dataframe)


@pytest.mark.parametrize(
    "state,cancel_requested,expected_caption",
    [
        ("RUNNING", False, session_view.STATUS_RUNNING),
        ("RUNNING", True, session_view.STATUS_WAITING),
        ("COMPLETED", False, session_view.STATUS_COMPLETED),
        ("FAILED", False, session_view.STATUS_FAILED),
        ("INTERRUPTED", False, session_view.STATUS_REQUIRES_ATTENTION),
        ("UNKNOWN", False, session_view.STATUS_REQUIRES_ATTENTION),
        ("CANCELLED", False, session_view.STATUS_CANCELLED),
    ],
)
def test_dashboard_renders_each_status_bucket(state, cancel_requested, expected_caption):
    api = runtime_api.ExecutionCenterAPI()
    proc = subprocess.Popen(["sleep", "5"]) if state == "RUNNING" else None
    try:
        _make_run_row(api.db_path, state=state, cancel_requested=cancel_requested, pid=proc.pid if proc else None)

        at = _at_on_page("execution_center")
        assert not at.exception
        # Live runs render as cards (with a status caption); finished ones now
        # render in a collapsed table, because thirty-four stacked cards of
        # finished work is a wall to scroll past rather than information. The
        # property under test is the same either way: the run is classified
        # into its bucket and shown.
        assert _shows_status(at, expected_caption), (
            f"прогон {expected_caption} не показан ни карточкой, ни в таблице"
        )
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait()


def test_dashboard_renders_spawned_but_silent_run_as_starting_not_failed():
    """A RUNNING run with a live PID but no first output yet must render as
    `Starting` (a warning), never `Failed` — the direct UI counterpart of the
    reported defect (a live process shown as timeout/failed)."""
    api = runtime_api.ExecutionCenterAPI()
    proc = subprocess.Popen(["sleep", "5"])
    try:
        _make_run_row(api.db_path, state="RUNNING", pid=proc.pid, first_output_at=None)

        at = _at_on_page("execution_center")
        assert not at.exception
        captions = [c.value for c in at.caption]
        assert any(f"Статус: **{session_view.STATUS_STARTING}**" in c for c in captions)
        assert not any(f"Статус: **{session_view.STATUS_FAILED}**" in c for c in captions)
    finally:
        proc.terminate()
        proc.wait()


# --------------------------------------------------------------------------
# 13. Auto-refresh does not create duplicate runs or duplicate task mutations
# --------------------------------------------------------------------------


def test_auto_refresh_does_not_duplicate_runs_or_task_mutations(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)

    api = runtime_api.ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="review", instruction="p", confirmed=True
    )
    api.supervisor.wait_for_run(run["id"], timeout=10)
    _wait_for_report(api.db_path, run["id"])

    at = _at_on_page("execution_center")
    for _ in range(3):
        at = at.run()
        assert not at.exception

    all_runs = runtime_db.list_runs(api.db_path, limit=100)
    assert len(all_runs) == 1, "repeated refreshes must never create a new run"


# --------------------------------------------------------------------------
# 14. The rebuilt board: running-first order, summary, console actions,
#     compact attention rows, and the project dependency tree.
# --------------------------------------------------------------------------

from command_center import tasks_repository  # noqa: E402

APP_ROOT = Path(__file__).resolve().parent.parent


def test_board_summary_and_console_actions_render():
    """The console leads with a state summary and an action bar — create a
    task, waves, reports — instead of the planner's wave and a project grid."""
    api = runtime_api.ExecutionCenterAPI()  # constructs + migrates the isolated db
    _make_run_row(api.db_path, state="RUNNING", pid=None)  # -> Requires Attention (no live pid)
    at = _at_on_page("execution_center")
    assert not at.exception

    # The summary is rendered as `board_style`'s tinted HTML tiles (not
    # `st.metric`), so it appears in the markdown stream.
    markdown = " ".join(str(m.value) for m in at.markdown)
    assert "Выполняется" in markdown
    assert "Требуют внимания" in markdown

    button_labels = [b.label for b in at.button]
    assert "Создать задачу" in button_labels
    assert "Волны" in button_labels
    assert "Отчёты" in button_labels


def test_console_create_task_panel_opens_and_creates_a_task():
    at = _at_on_page("execution_center", exec_board_open_panel="create")
    assert not at.exception

    at.selectbox(key="console_create_project").select("AICC").run()
    at.text_input(key="console_create_title").set_value("Задача из консоли").run()
    # The submit lives inside an st.form; drive it through the form button.
    form_submit = next(b for b in at.button if b.label == "Создать")
    at = form_submit.click().run()
    assert not at.exception

    titles = [t.get("title") for t in tasks_repository.load_tasks(APP_ROOT)]
    assert "Задача из консоли" in titles


def test_attention_row_shows_reason_without_expanding():
    """A failed run's reason must be readable on the row itself — collapsing it
    behind the toggle would trade one unusable screen for another."""
    api = runtime_api.ExecutionCenterAPI()
    _make_run_row(api.db_path, state="FAILED", failure_reason="boom: the thing broke")
    at = _at_on_page("execution_center")
    assert not at.exception

    errors = " ".join(e.value for e in at.error)
    assert "boom: the thing broke" in errors


def _seed_task(project: str, title: str, *, status: str = "Backlog", **fields) -> dict:
    return tasks_repository.create_task(APP_ROOT, project, title, "review", status, **fields)


def test_project_tree_renders_levels_when_a_project_is_selected():
    """Selecting a project opens its plan as dependency levels in the main
    column, coloured by state, with the next task to start flagged."""
    # A run gives the project a card in the side strip; the tree itself is
    # built from the Kanban tasks.
    api = runtime_api.ExecutionCenterAPI()
    _make_run_row(api.db_path, state="RUNNING", pid=None)
    root = _seed_task("AICC", "Корневая задача", status="Done")
    _seed_task("AICC", "Следующая задача", depends_on=[root["id"]])

    at = _at_on_page("execution_center", exec_board_project_tree="AICC")
    assert not at.exception

    markdown = " ".join(str(m.value) for m in at.markdown)
    assert "дерево задач" in markdown
    assert "Уровень 0" in markdown
    assert "Уровень 1" in markdown


def test_project_tree_flags_the_next_task_to_launch():
    api = runtime_api.ExecutionCenterAPI()
    _make_run_row(api.db_path, state="RUNNING", pid=None)
    done = _seed_task("AICC", "Уже сделано", status="Done")
    _seed_task("AICC", "Пора запускать", depends_on=[done["id"]])

    at = _at_on_page("execution_center", exec_board_project_tree="AICC")
    assert not at.exception

    captions = " ".join(c.value for c in at.caption)
    assert "Следующая по плану" in captions
