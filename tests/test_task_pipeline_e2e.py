"""End-to-end desktop-autopilot scenario (AICC-DESKTOP-020) — the final gate.

One test drives the whole product objective through the real machinery:

    dependency wave -> parallel launch -> completion -> merge -> next wave

Only two things are stand-ins, and both are the *same* ones the existing
supervisor and completion suites already use:

  * the `claude` binary (`tests/fixtures/fake_claude.py`) — a real
    `subprocess.Popen` with a real pid and real signal handling, so launches,
    reconciliation and run finalization are genuine;
  * the GitHub API (`runtime.github.FakeGitHubClient`) — with its `on_merge`
    hook wired to a *real* git merge into a *real* local bare remote, so
    pushing, merging and target-branch verification exercise actual git.

Everything else — the queue, the scheduler, the launcher, the completion state
machine, the Kanban projection, the task store — is production code.

The one thing the test does on the agent's behalf is `git commit` the change
the fake `claude` writes into the workspace. A real implementation run commits
its own work; the fake only touches a file. Without that commit the completion
evaluator would (correctly) refuse to proceed on an uncommitted tree, which is
a property already covered directly in `test_completion_scenarios.py`.
"""

from __future__ import annotations

import contextlib

import pytest

from command_center import (
    execution_queue,
    models,
    pipeline_settings,
    project_config,
    task_pipeline,
    tasks_repository,
)
from command_center.pipeline_settings import PipelineSettings
from command_center.runtime import api as runtime_api
from command_center.runtime import completion as completion_domain
from command_center.runtime import db as runtime_db
from command_center.runtime import scheduler
from command_center.runtime import supervisor as supervisor_runtime
from command_center.runtime.github import FakeGitHubClient
from tests.completion_helpers import build_repo, git, merge_into_main
from tests.test_task_pipeline import _drain_runs


def _task(task_id, project, workspace, *, branch, depends_on=None):
    task = {
        "id": task_id,
        "project": project,
        "title": f"Task {task_id}",
        "status": "Backlog",
        "priority": "High",
        "depends_on": depends_on or [],
        "task_type": "implementation",
    }
    task.update(models.default_task_execution_fields())
    task.update(models.default_task_workflow_fields())
    task["workspace_path"] = str(workspace)
    task["branch"] = branch
    return task


@pytest.fixture
def api(tmp_path):
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    yield api
    _drain_runs(api)


def _project_repo(tmp_path, project, name):
    """A real bare remote plus the project's primary working clone on `main`,
    registered as `project`'s repository in the (isolated) project config.

    The task's own workspace is deliberately *not* created here. A feature-branch
    launch must run in an isolated linked worktree, never in the primary working
    tree (`workspace_provisioning.is_feature_task` -> the `isolated_worktree_
    required` gate), so the pipeline provisions it itself at launch time. That is
    the production path, and exercising it is the point — an earlier draft of
    this test pointed the task straight at `work` and was correctly refused."""
    remote, work = build_repo(tmp_path / name)
    project_config.save_project_settings(
        project, repository_path=str(work), default_branch="main"
    )
    return remote, work


def _commit_agent_work(work, message="agent work"):
    """Commit whatever the fake agent left in the working tree, as a real
    implementation run would have done itself."""
    status = git(work, "status", "--porcelain").stdout.strip()
    if not status:
        return
    git(work, "add", "-A")
    git(work, "commit", "-m", message)


def test_dependency_wave_parallel_launch_completion_merge_and_next_wave(
    tmp_path, api, fake_claude
):
    # ---------------------------------------------------------------- setup
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True,
            auto_launch=True,
            auto_merge_after_checks=True,
            max_global_concurrency=4,
            max_agent_concurrency=4,
        ),
    )
    remote_a, _work_a = _project_repo(tmp_path, "AIOS", "proj-a")
    remote_b, _work_b = _project_repo(tmp_path, "AICC", "proj-b")
    _remote_c, _work_c = _project_repo(tmp_path, "AICOS", "proj-c")

    # Worktree paths that do not exist yet — the pipeline creates them.
    wt_a = tmp_path / "wt" / "a"
    wt_b = tmp_path / "wt" / "b"
    wt_c = tmp_path / "wt" / "c"
    task_a = _task("a", "AIOS", wt_a, branch="task/a")
    task_b = _task("b", "AICC", wt_b, branch="task/b")
    task_c = _task("c", "AICOS", wt_c, branch="task/c", depends_on=["a", "b"])
    tasks = [task_a, task_b, task_c]
    tasks_repository.save_tasks(tmp_path, tasks)
    tasks_by_id = {t["id"]: t for t in tasks}
    for task in tasks:
        execution_queue.enqueue_and_persist(tmp_path, task, tasks_by_id)

    configs = project_config.load_project_configs()
    # `merge_into_main` clones into `tmp_path/server-<branch>`, so one shared
    # parent directory still gives each branch its own throwaway server clone.
    merges = {
        "task/a": lambda pr: merge_into_main(remote_a, tmp_path, pr.head_ref),
        "task/b": lambda pr: merge_into_main(remote_b, tmp_path, pr.head_ref),
    }
    github = FakeGitHubClient(on_merge=lambda pr: merges[pr.head_ref](pr))

    # ------------------------------------------------- 1. the dependency wave
    first = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    assert first.ran is True

    launched = {d.task_id for d in first.launched()}
    assert launched == {"a", "b"}, [d.as_dict() for d in first.decisions]
    assert len({d.run_id for d in first.launched()}) == 2
    assert len({d.workspace for d in first.launched()}) == 2
    # The isolated worktrees were provisioned by the launch itself.
    assert wt_a.is_dir() and wt_b.is_dir()

    # `c` is dependency-blocked, so it is not even a candidate: the queue keeps
    # it WAITING and it never reaches the planner.
    assert "c" not in {d.task_id for d in first.decisions}
    waiting = execution_queue.waiting_entries(execution_queue.load_queue(tmp_path))
    assert [e["task_id"] for e in waiting] == ["c"]

    # --------------------------------------- 2. the agents finish their work
    for run in api.list_runs():
        assert api.supervisor.wait_for_run(run["id"], timeout=60)["state"] == "COMPLETED"
    _commit_agent_work(wt_a, "implement task a")
    _commit_agent_work(wt_b, "implement task b")

    # ------------------------------- 3. completion -> PR -> merge -> verified
    second = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    assert second.ran is True
    assert not second.errors, second.errors

    # The host-level auto-merge opt-in reached both rows...
    assert {u["to"] for u in second.merge_policy_updates} <= {
        completion_domain.MERGE_AUTO_AFTER_CHECKS
    }
    # ...and the pipeline drove each of them all the way to verified-in-target.
    for task_id in ("a", "b"):
        row = runtime_db.get_completion_by_task(api.db_path, task_id)
        assert row is not None, f"no completion row for {task_id}"
        assert row["completion_state"] == completion_domain.CompletionState.COMPLETED, (
            f"{task_id}: {row['completion_state']} / {row['recommended_action']}"
        )
        assert row["pull_request_number"] is not None
        assert row["merge_commit"]
    assert {pr.head_ref for pr in github.created} == {"task/a", "task/b"}

    # `Done` means the merge is verified in the target branch — not "a PR exists".
    assert set(second.completed_task_ids) == {"a", "b"}
    persisted = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
    assert persisted["a"]["status"] == "Done"
    assert persisted["b"]["status"] == "Done"
    assert persisted["a"]["pull_request_status"] == "merged"
    assert persisted["a"]["progress"] == 100

    # ----------------------------------------------------- 4. the next wave
    # The whole point of the ordering: the Done projection happens *before* the
    # plan step, so `c`'s dependencies are re-evaluated and it is planned and
    # launched on this very same tick — not one tick later.
    by_task = {d.task_id: d for d in second.decisions}
    assert "c" in by_task
    assert by_task["c"].action == scheduler.ACTION_ASSIGN
    assert by_task["c"].agent_id == "claude_code"
    assert by_task["c"].attempt == 1
    assert {d.task_id for d in second.launched()} == {"c"}
    assert wt_c.is_dir()

    # Nothing is left ready afterwards, so the re-planned wave is empty — and a
    # further tick must not start `c` a second time.
    assert second.next_wave == ()
    third = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    assert third.launched() == []
    assert len(api.list_runs()) == 3


def test_auto_merge_opt_in_off_stops_at_an_open_pull_request(tmp_path, api, fake_claude):
    """The same pipeline with auto-merge withheld: it still validates, pushes
    and opens the PR, but stops there — nothing is merged and the task is not
    Done. This is what makes the opt-in meaningful rather than decorative."""
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(enabled=True, auto_launch=True, auto_merge_after_checks=False),
    )
    _project_repo(tmp_path, "AIOS", "proj-a")
    worktree = tmp_path / "wt" / "a"
    task = _task("a", "AIOS", worktree, branch="task/a")
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {"a": task})
    configs = project_config.load_project_configs()
    github = FakeGitHubClient()

    first = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    assert first.launched(), [d.as_dict() for d in first.decisions]
    for run in api.list_runs():
        api.supervisor.wait_for_run(run["id"], timeout=60)
    _commit_agent_work(worktree)

    result = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    row = runtime_db.get_completion_by_task(api.db_path, "a")
    assert row["pull_request_number"] is not None
    assert row["completion_state"] != completion_domain.CompletionState.COMPLETED
    assert github.merged == []
    assert result.completed_task_ids == ()
    assert tasks_repository.load_tasks(tmp_path)[0]["status"] != "Done"


def test_dispatcher_restart_mid_run_yields_no_duplicates(tmp_path, api, fake_claude):
    """NIGHT-W7-AICC-AUTONOMY: the dispatcher (tick loop) dies while an agent
    run is in flight; a fresh dispatcher instance reconciles and retries.
    The invariants under test: exactly one live attempt at a time, the
    crashed attempt is classified truthfully (never fabricated COMPLETED),
    and the task ends with exactly one PR and one merge — no duplicates.
    """
    import os
    import signal
    import time

    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True,
            auto_launch=True,
            auto_merge_after_checks=True,
            max_global_concurrency=2,
            max_agent_concurrency=2,
        ),
    )
    remote, _work = _project_repo(tmp_path, "AIOS", "proj-r")
    wt = tmp_path / "wt" / "r"
    task = _task("r", "AIOS", wt, branch="task/r")
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {"r": task})
    configs = project_config.load_project_configs()
    github = FakeGitHubClient(
        on_merge=lambda pr: merge_into_main(remote, tmp_path, pr.head_ref)
    )

    # Keep the fake agent alive until the hold file disappears, so the
    # crash window is deterministic instead of racing a sleep.
    hold = tmp_path / "hold-r"
    hold.write_text("")
    fake_claude["FAKE_CLAUDE_HOLD_FILE"] = str(hold)

    # No working-tree change from the fake, and this is the fix for a real
    # flake rather than a convenience. The fixture's default makes every fake
    # run write an *untracked* file, between emitting its output and waiting on
    # the hold file — so whether it lands before the SIGKILL below is a
    # scheduling question. When it lands, the restarted dispatcher correctly
    # refuses to relaunch into a dirty worktree ("0 changed, 1 untracked") and
    # this test fails; when it does not, the test passes. It failed exactly
    # that way twice today, both times only under `-n auto`, and each failure
    # cost a full CI rerun.
    #
    # The refusal is right and is covered by its own tests. What is wrong is
    # this test arranging for a refusal while asking for a relaunch. So the
    # fake commits its work instead of leaving it loose: HEAD advances, the
    # worktree stays clean, and the restarted dispatcher's cleanliness check
    # has nothing to object to whichever side of the SIGKILL the write lands.
    #
    # Simply dropping the change was the first attempt and was wrong — the
    # suite said so immediately: an implementation run that changes nothing is
    # classified `FAILED` rather than `COMPLETED`
    # (`runtime.outcome.REQUIRES_CHANGES_TASK_TYPES`), so the test needs the
    # work to happen *and* the tree to be clean, which is what committing is.
    fake_claude["FAKE_CLAUDE_COMMIT"] = "agent work committed by the fake"


    first = task_pipeline.tick(tmp_path, api, configs, github=github, advance_wait_seconds=60)
    launched = first.launched()
    assert [d.task_id for d in launched] == ["r"]
    run_id = launched[0].run_id

    # ------------------------------- crash: agent SIGKILLed, dispatcher gone
    # Mid-run, not at startup: wait until the agent produced real output so
    # the crash is a host/dispatcher death, not the provider-dead-on-startup
    # failover path (that one is a different, already-covered behavior).
    deadline = time.time() + 15
    while time.time() < deadline:
        run = api.get_run(run_id)
        if run.get("first_output_at"):
            break
        time.sleep(0.1)
    assert run.get("first_output_at"), "agent never produced output"
    os.kill(run["pid"], signal.SIGKILL)
    deadline = time.time() + 10
    while time.time() > 0 and time.time() < deadline:
        try:
            os.kill(run["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    # The old dispatcher would normally record the exit itself; a true
    # dispatcher death happens BEFORE that finalization. Let the doomed
    # in-process watcher finish, then reconstruct the exact crash window it
    # would have left behind: the run row still RUNNING, the pid dead --
    # precisely the state `Supervisor.reconcile()` exists to classify.
    finished = api.supervisor.wait_for_run(run_id, timeout=30)
    assert finished["state"] in runtime_db.TERMINAL_STATES, finished["state"]
    with runtime_db.connect(api.db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute(
                "UPDATE run SET state=?, completed_at=NULL, finalized_at=NULL, "
                "failure_reason=NULL, exit_code=NULL WHERE id=?",
                ("RUNNING", run_id),
            )
            conn.execute(
                "UPDATE run_finalization_claim SET owner_token=?, owner_pid=?, "
                "owner_identity=?, completed_at=NULL WHERE run_id=?",
                ("dead-owner", 999_999_999, "dead-start|dead-command", run_id),
            )
    # A real dispatcher restart creates a new Python process, so its
    # process-local ownership registry starts empty.  This E2E test keeps the
    # restart inside one pytest process; explicitly reproduce that one piece
    # of process-boundary state after the old watcher has finished.  Creating
    # a second API facade alone must *not* clear the registry: other tests
    # intentionally prove that same-process facades cannot steal live claims.
    with supervisor_runtime._PROCESS_OWNED_RUNS_GUARD:
        supervisor_runtime._PROCESS_OWNED_RUNS.discard(run_id)
    del api  # the old dispatcher, and its in-memory Popen registry, are gone

    # ------------------------------------- restart: fresh dispatcher instance
    api2 = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    try:
        reconciled = api2.reconcile()
        crashed = api2.get_run(run_id)
        # Truthful classification: the crashed attempt is terminal and NOT
        # COMPLETED — reconcile never guesses success.
        assert crashed["state"] in {"INTERRUPTED", "FAILED"}, crashed["state"]
        assert crashed["state"] != "COMPLETED"
        assert any(r["id"] == run_id for r in api2.list_runs()) and reconciled is not None

        # Time passes beyond the deterministic backoff window (simulated by
        # anchoring the crashed attempt in the past — the policy itself is
        # exercised, not bypassed: without this the planner must DEFER).
        deferred = task_pipeline.tick(
            tmp_path, api2, configs, github=github, advance_wait_seconds=60
        )
        assert deferred.launched() == [], "retry must respect backoff, not relaunch instantly"

        with runtime_db.connect(api2.db_path) as conn:
            with runtime_db.transaction(conn):
                conn.execute(
                    "UPDATE run SET completed_at = datetime(completed_at, '-15 minutes') WHERE id = ?",
                    (run_id,),
                )

        hold.unlink()  # let the *next* attempt's agent run to completion
        fake_claude.pop("FAKE_CLAUDE_HOLD_FILE", None)

        retry = task_pipeline.tick(
            tmp_path, api2, configs, github=github, advance_wait_seconds=60
        )
        relaunched = retry.launched()
        assert [d.task_id for d in relaunched] == ["r"], [(d.reason_code, d.explanation) for d in retry.decisions]
        assert relaunched[0].attempt == 2
        assert relaunched[0].run_id != run_id
        # One live attempt only: exactly two runs exist, one terminal crash +
        # one new attempt.
        assert len(api2.list_runs(task_id="r")) == 2

        assert api2.supervisor.wait_for_run(relaunched[0].run_id, timeout=60)[
            "state"
        ] == "COMPLETED"
        _commit_agent_work(wt, "implement task r")

        final = task_pipeline.tick(
            tmp_path, api2, configs, github=github, advance_wait_seconds=60
        )
        assert not final.errors, final.errors
        row = runtime_db.get_completion_by_task(api2.db_path, "r")
        assert row is not None
        assert row["completion_state"] == completion_domain.CompletionState.COMPLETED
        # No duplicates anywhere: one PR for the branch, one merge commit,
        # still exactly two run rows.
        assert [pr.head_ref for pr in github.created] == ["task/r"]
        assert row["merge_commit"]
        assert len(api2.list_runs(task_id="r")) == 2
        persisted = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
        assert persisted["r"]["status"] == "Done"
    finally:
        _drain_runs(api2)


def test_kill_switch_stops_running_work_and_blocks_future_launches(
    tmp_path, api, fake_claude
):
    """NIGHT-W7-AICC-AUTONOMY kill switch: one action cancels the live run
    and persists the master switch off, so no tick — this process or any
    other — launches anything afterwards."""
    import time

    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True, auto_launch=True, max_global_concurrency=2, max_agent_concurrency=2
        ),
    )
    _remote, _work = _project_repo(tmp_path, "AIOS", "proj-k")
    wt = tmp_path / "wt" / "k"
    task = _task("k", "AIOS", wt, branch="task/k")
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {"k": task})
    configs = project_config.load_project_configs()

    hold = tmp_path / "hold-k"
    hold.write_text("")
    fake_claude["FAKE_CLAUDE_HOLD_FILE"] = str(hold)

    first = task_pipeline.tick(tmp_path, api, configs, github=FakeGitHubClient(), advance_wait_seconds=60)
    launched = first.launched()
    assert [d.task_id for d in launched] == ["k"]
    run_id = launched[0].run_id

    # Unconfirmed invocation is refused before any effect (fail-closed).
    with pytest.raises(Exception):
        task_pipeline.kill_switch(tmp_path, api, confirmed=False)
    assert pipeline_settings.load_settings(tmp_path).enabled is True

    report = task_pipeline.kill_switch(tmp_path, api, confirmed=True)
    assert report["disabled"] is True
    assert report["cancelled"] == [run_id]
    assert report["cancel_errors"] == {}

    # The run terminates (CANCELLED after grace) and the switch is durably off.
    deadline = time.time() + 30
    while time.time() < deadline:
        state = api.get_run(run_id)["state"]
        if state in runtime_db.TERMINAL_STATES:
            break
        time.sleep(0.2)
    assert api.get_run(run_id)["state"] == "CANCELLED"
    assert pipeline_settings.load_settings(tmp_path).enabled is False

    # A later tick — same or fresh dispatcher — launches nothing.
    api2 = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    after = task_pipeline.tick(tmp_path, api2, configs, github=FakeGitHubClient(), advance_wait_seconds=60)
    assert after.ran is False
    assert after.launched() == []


def test_daily_spend_budget_gates_new_launches_only(tmp_path, api, fake_claude):
    """NIGHT-W7-AICC-AUTONOMY spend budget: with the trailing-24h provider
    cost at/over `max_daily_spend_usd`, a tick launches nothing and says why
    (`daily_spend_budget_exhausted`); with budget off (0) it launches."""

    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True, auto_launch=True, max_daily_spend_usd=1.0,
            max_global_concurrency=2, max_agent_concurrency=2,
        ),
    )
    _remote, _work = _project_repo(tmp_path, "AIOS", "proj-s")
    wt = tmp_path / "wt" / "s"
    task = _task("s", "AIOS", wt, branch="task/s")
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {"s": task})
    configs = project_config.load_project_configs()

    # Seed a prior completed run whose provider reported $1.50 spent today.
    prior_task = runtime_db.create_task(
        api.db_path, project="AIOS", title="prior", task_type="implementation"
    )
    prior_session = runtime_db.create_session(
        api.db_path, task_id=prior_task["id"], project="AIOS", repository_path="/tmp/x"
    )
    prior = runtime_db.create_run(
        api.db_path, session_id=prior_session["id"], task_id=prior_task["id"],
        project="AIOS", task_type="implementation", repository_path="/tmp/x",
        prompt="prior", is_resume=False,
    )
    runtime_db.append_run_event(
        api.db_path, prior["id"], "stream_event",
        {"type": "result", "total_cost_usd": 1.5, "usage": {"input_tokens": 1}},
    )
    with runtime_db.connect(api.db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (models.iso_now(), prior["id"]),
            )

    assert task_pipeline.daily_spend_usd(api.db_path) == pytest.approx(1.5)

    gated = task_pipeline.tick(tmp_path, api, configs, github=FakeGitHubClient(), advance_wait_seconds=60)
    assert gated.launched() == []
    assert gated.launch_status == task_pipeline.LAUNCH_BUDGET_EXHAUSTED

    # Budget off -> the same task launches normally.
    settings = pipeline_settings.load_settings(tmp_path)
    pipeline_settings.save_settings(
        tmp_path, __import__("dataclasses").replace(settings, max_daily_spend_usd=0.0)
    )
    ungated = task_pipeline.tick(tmp_path, api, configs, github=FakeGitHubClient(), advance_wait_seconds=60)
    assert [d.task_id for d in ungated.launched()] == ["s"]


def test_daily_spend_unknown_gates_launches_without_claiming_budget_exhausted(tmp_path, api, fake_claude):
    """Corrupt cost data (here: a negative `total_cost_usd`, which cannot be a
    real spend figure) must block new launches exactly like a genuinely
    exhausted budget does, but the tick must say the spend is *unknown*
    (`LAUNCH_SPEND_UNKNOWN`) rather than falsely claiming it read a known
    figure that happened to be over the ceiling (`LAUNCH_BUDGET_EXHAUSTED`)."""

    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True, auto_launch=True, max_daily_spend_usd=1.0,
            max_global_concurrency=2, max_agent_concurrency=2,
        ),
    )
    _remote, _work = _project_repo(tmp_path, "AIOS", "proj-u")
    wt = tmp_path / "wt" / "u"
    task = _task("u", "AIOS", wt, branch="task/u")
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {"u": task})
    configs = project_config.load_project_configs()

    prior_task = runtime_db.create_task(
        api.db_path, project="AIOS", title="prior", task_type="implementation"
    )
    prior_session = runtime_db.create_session(
        api.db_path, task_id=prior_task["id"], project="AIOS", repository_path="/tmp/x"
    )
    prior = runtime_db.create_run(
        api.db_path, session_id=prior_session["id"], task_id=prior_task["id"],
        project="AIOS", task_type="implementation", repository_path="/tmp/x",
        prompt="prior", is_resume=False,
    )
    runtime_db.append_run_event(
        api.db_path, prior["id"], "stream_event",
        {"type": "result", "total_cost_usd": -5.0, "usage": {"input_tokens": 1}},
    )
    with runtime_db.connect(api.db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (models.iso_now(), prior["id"]),
            )

    with pytest.raises(task_pipeline.SpendUnknownError):
        task_pipeline.daily_spend_usd(api.db_path)

    gated = task_pipeline.tick(tmp_path, api, configs, github=FakeGitHubClient(), advance_wait_seconds=60)
    assert gated.launched() == []
    assert gated.launch_status == task_pipeline.LAUNCH_SPEND_UNKNOWN


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return _FakeCursor(self._rows)


def _fake_daily_spend_rows(monkeypatch, rows):
    @contextlib.contextmanager
    def _fake_connect(_db_path):
        yield _FakeConn(rows)

    monkeypatch.setattr(task_pipeline.runtime_db, "connect", _fake_connect)


def test_daily_spend_usd_tolerates_dict_payloads_and_unrelated_matches(tmp_path, monkeypatch):
    """A `jsonb`-backed read (the PostgreSQL mirror, VOYN-W0-AICC-SRV-01B) hands
    back a `payload` that is already a decoded `dict`, not JSON text, and
    `json.loads(dict)` raises `TypeError`. A prior version caught `TypeError`
    alongside `ValueError` and silently `continue`d — every row in the batch
    was dropped with no error and no log line, so the spend cap read 0 and
    stopped gating without ever saying so. A dict-shaped row must be summed
    like any other. A row that merely matched the `LIKE '%total_cost_usd%'`
    prefilter without carrying the key at its top level (e.g. the phrase only
    appears nested) is not a cost event and must not be treated as corrupt."""

    _fake_daily_spend_rows(
        monkeypatch,
        [
            {"payload": '{"type": "result", "total_cost_usd": 2.0}'},
            {"payload": {"type": "result", "total_cost_usd": 3.5}},
            {"payload": '{"type": "result", "note": "see total_cost_usd in the nested usage blob"}'},
        ],
    )

    total = task_pipeline.daily_spend_usd(tmp_path / "runtime.db")

    assert total == pytest.approx(5.5)


def test_daily_spend_usd_raises_on_unparseable_selected_payload(tmp_path, monkeypatch):
    """A row selected by the `total_cost_usd` text scan that fails to parse as
    JSON could be concealing real spend behind broken JSON — it must raise
    `SpendUnknownError`, never be silently skipped (a truncated event like
    this is exactly the shape a corrupted write could leave behind)."""

    _fake_daily_spend_rows(monkeypatch, [{"payload": '{"total_cost_usd":100'}])

    with pytest.raises(task_pipeline.SpendUnknownError) as exc_info:
        task_pipeline.daily_spend_usd(tmp_path / "runtime.db")
    assert exc_info.value.reason == task_pipeline.CORRUPT_COST_EVENT


def test_daily_spend_usd_raises_on_non_dict_selected_payload(tmp_path, monkeypatch):
    _fake_daily_spend_rows(monkeypatch, [{"payload": '["total_cost_usd", 1.0]'}])

    with pytest.raises(task_pipeline.SpendUnknownError):
        task_pipeline.daily_spend_usd(tmp_path / "runtime.db")


@pytest.mark.parametrize(
    "cost",
    [
        "1.0",  # a string, not a number
        None,
        True,
        float("nan"),  # json.loads accepts NaN as a Python float
        float("inf"),
        -1.0,  # negative spend cannot be real
    ],
)
def test_daily_spend_usd_raises_on_untrustworthy_cost_value(tmp_path, monkeypatch, cost):
    import json

    payload = json.dumps({"type": "result", "total_cost_usd": cost}, allow_nan=True)
    _fake_daily_spend_rows(monkeypatch, [{"payload": payload}])

    with pytest.raises(task_pipeline.SpendUnknownError) as exc_info:
        task_pipeline.daily_spend_usd(tmp_path / "runtime.db")
    assert exc_info.value.reason == task_pipeline.CORRUPT_COST_EVENT


def test_daily_spend_usd_raises_on_accumulated_overflow(tmp_path, monkeypatch):
    """Each individual cost is finite, but the running total is not: two
    values near the top of `float` range still sum to `inf`, which must be
    caught the same way a single non-finite value would be."""

    huge = 1e308
    _fake_daily_spend_rows(
        monkeypatch,
        [
            {"payload": f'{{"type": "result", "total_cost_usd": {huge}}}'},
            {"payload": f'{{"type": "result", "total_cost_usd": {huge}}}'},
        ],
    )

    with pytest.raises(task_pipeline.SpendUnknownError) as exc_info:
        task_pipeline.daily_spend_usd(tmp_path / "runtime.db")
    assert exc_info.value.reason == task_pipeline.CORRUPT_COST_EVENT
