import json
import subprocess

from command_center import report_parser
from command_center.runtime import api as runtime_api
from command_center.runtime import db, task_sync


def _make_task(**overrides) -> dict:
    task = {
        "id": "task-1",
        "project": "AIOS",
        "title": "Deploy P1",
        "launch_status": "Ready",
        "current_run_id": None,
        "progress": 0,
        "progress_mode": "auto",
        "timeline": [],
    }
    task.update(overrides)
    return task


def _make_run(db_path, *, state: str, task_id: str = "task-1", provider_id: str = "claude_code", **fields) -> dict:
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation", task_id=task_id)
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
        provider_id=provider_id,
    )
    if state != "PREPARED":
        run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    if state not in ("PREPARED", "QUEUED"):
        run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="RUNNING")
    if state not in ("PREPARED", "QUEUED", "RUNNING"):
        run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state=state, fields=fields)
    if state in db.TERMINAL_STATES:
        # Every terminal fixture here is meant to represent a run whose
        # finalization (report write, auto-commit, `process_exited` event —
        # see `runtime.run_finalizer`) has already landed, matching what
        # `sync_task_from_run`/`_seed_and_project_completion` now require
        # before publishing terminal fields or seeding a completion. The
        # pre-finalization window itself is exercised by dedicated tests
        # below, not by every fixture that just wants a "done" run.
        db.mark_run_finalized(db_path, run["id"])
        run = db.get_run(db_path, run["id"])
    return run


# --------------------------------------------------------------------------
# sync_task_from_run — status mapping
# --------------------------------------------------------------------------


def test_sync_task_from_run_running_sets_launch_status_running(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    task = _make_task()

    mutated = task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert mutated is True
    assert task["launch_status"] == "Running"
    assert task["current_run_id"] == run["id"]


def test_sync_task_from_run_completed_advances_progress_and_extracts_fields(tmp_path):
    """A `Completed` run's terminal sync now advances `current_stage`/
    `progress` (mirroring the retired v1 synchronous launch flow) instead of
    leaving it frozen at whatever `launch.begin_launch` set it to
    at launch time. A PR-ready result reaches "PR Ready" (progress 95) — not
    100 ("Merged", which no agent run here is ever permitted to reach on its
    own, since none of them call `git commit`/`push`/`merge`) — so per the
    "Completed implies progress == 100" invariant (see `task_sync.
    _resolve_target_launch_status`), `launch_status` becomes "Needs Review",
    not "Completed"."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    db.append_run_event(
        db_path, run["id"], "result",
        {"result": "Verdict: APPROVED FOR COMMIT\nPR: https://example.invalid/pr/1\nCommit: abcdef1"},
    )
    db.create_report(db_path, run["id"], f"reports/AIOS/{run['id'][:8]}.md")
    task = _make_task()

    mutated = task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert mutated is True
    assert task["launch_status"] == "Needs Review"
    assert task["current_stage"] == "PR Ready"
    assert task["progress"] == 95
    assert task["latest_verdict"] == "APPROVED_FOR_COMMIT"
    assert task["pull_request_url"] == "https://example.invalid/pr/1"
    assert task["report_path"] == f"reports/AIOS/{run['id'][:8]}.md"
    assert [event["type"] for event in task["timeline"]] == ["pr_created", "completed"]

    reloaded_run = db.get_run(db_path, run["id"])
    assert reloaded_run["commit_hash"] == "abcdef1"
    assert reloaded_run["pull_request_url"] == "https://example.invalid/pr/1"


def test_sync_task_from_run_completed_becomes_launch_status_completed_only_at_progress_100(tmp_path):
    """The one way `launch_status` legitimately reaches "Completed": the task
    was already manually advanced to progress 100 (`progress_mode="manual"`,
    e.g. by a human marking the PR merged) *before* this run's terminal
    sync — `models.set_current_stage`'s own manual-override guard means the
    sync's stage advancement is then a no-op, so progress stays at 100."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    db.append_run_event(db_path, run["id"], "result", {"result": "Verdict: APPROVED FOR COMMIT"})
    task = _make_task(progress=100, progress_mode="manual", current_stage="Merged")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Completed"
    assert task["progress"] == 100


def test_sync_task_from_run_completed_with_no_verdict_or_pr_needs_review(tmp_path):
    """No extractable verdict/PR at all: progress is never advanced (neither
    `set_current_stage` branch fires) and a fresh task's progress (0, below
    100) means launch_status is "Needs Review", never "Completed" — an
    ambiguous/empty report is never treated as a confident success."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Needs Review"
    assert task["progress"] == 0


def test_sync_task_from_run_failed_sets_launch_status_failed(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="FAILED", completed_at="2026-01-01T00:01:00")
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Failed"
    assert task["timeline"][0]["type"] == "launch_requires_attention"


def test_sync_task_from_run_interrupted_with_no_output_auto_retries(tmp_path):
    """An INTERRUPTED run that never produced output (startup failure — e.g.
    expired OAuth) records the dead executor and flips back to "Ready" so the
    next launch tries the next agent in the chain (AICC-DESKTOP-017)."""
    db_path = tmp_path / "runtime-INTERRUPTED.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="INTERRUPTED", completed_at="2026-01-01T00:01:00")
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Ready"
    assert "claude_code" in (task.get("failed_executors") or [])


def test_sync_task_from_run_interrupted_with_output_requeues_without_blaming_executor(tmp_path):
    """An INTERRUPTED run that *did* produce output before dying is a host/
    dispatcher death, not a provider fault (NIGHT-W7-AICC-AUTONOMY): the task
    is re-queued for an automatic bounded retry (launch_status "Ready" +
    relaunch_requested for the pipeline's re-enqueue step) and the executor
    is NOT recorded as failed — nothing here is evidence against it. The
    scheduler's retry budget and backoff still bound the relaunch."""
    db_path = tmp_path / "runtime-INTERRUPTED-output.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="INTERRUPTED", completed_at="2026-01-01T00:01:00")
    run = db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"first_output_at": "2026-01-01T00:00:30"})
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Ready"
    assert task.get("relaunch_requested") is True
    assert not task.get("failed_executors")


def test_sync_task_from_run_session_expired_triggers_executor_failover(tmp_path):
    """A FAILED run whose `failure_reason` names a provider-unavailable cause
    (here `session_expired`, the real-world claude_code OAuth-expiry case) must
    record the dead executor and flip the task back to "Ready" so the next
    launch tries the next agent — even though the run produced output (the
    synthetic auth-error message) and so is state=FAILED, not INTERRUPTED.
    This is the gap AICC-DESKTOP-017 originally missed: it only fired for
    INTERRUPTED-with-no-output, so provider failures with `first_output_at`
    set never populated `failed_executors` and the task kept relaunching on
    the same dead provider (or stranded once the retry budget ran out)."""
    db_path = tmp_path / "runtime-session-expired.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="session_expired", provider_id="claude_code",
    )
    run = db.update_run_fields(
        db_path, run["id"], expected_version=run["version"],
        fields={"first_output_at": "2026-01-01T00:00:30"},
    )
    task = _make_task(executor="claude_code")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Ready"
    assert task.get("failed_executors") == ["claude_code"]
    assert task["timeline"][-1]["type"] == "executor_failed"


def test_provider_failure_projection_is_idempotent_across_refresh_and_restart(tmp_path):
    """A provider failure returns the task to Ready, but repeated read-model
    reconciliation must not manufacture another terminal event pair."""
    db_path = tmp_path / "runtime-idempotent-provider.db"
    db.migrate(db_path)
    run = _make_run(
        db_path,
        state="FAILED",
        completed_at="2026-01-01T00:01:00",
        failure_reason="provider_api_error",
        provider_id="claude_code",
    )
    task = _make_task(executor="claude_code")

    assert task_sync.sync_task_from_run(task, run, db_path=db_path) is True
    first_events = list(task["timeline"])
    assert task["launch_status"] == "Ready"
    assert task["terminal_projection_run_id"] == run["id"]

    for _ in range(100):
        # JSON round-trip models a fresh Streamlit process/restart.
        restarted_task = json.loads(json.dumps(task))
        assert task_sync.sync_task_from_run(restarted_task, run, db_path=db_path) is False
        assert restarted_task["timeline"] == first_events
        task = restarted_task

    assert [event["type"] for event in task["timeline"]].count(
        "launch_requires_attention"
    ) == 1
    assert [event["type"] for event in task["timeline"]].count("executor_failed") == 1


def test_sync_task_from_run_quota_limit_triggers_executor_failover(tmp_path):
    """Any provider-unavailable reason — not just `session_expired` — flips the
    task to failover. `quota_limit` is the other common claude_code cause."""
    db_path = tmp_path / "runtime-quota.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="quota_limit", provider_id="claude_code",
    )
    task = _make_task(executor="claude_code")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Ready"
    assert task.get("failed_executors") == ["claude_code"]


def test_sync_task_from_run_permission_denied_does_not_trigger_failover(tmp_path):
    """A `blocked:permission_denied:*` failure is environment policy, identical
    on every executor, so switching agents cannot help — it must NOT populate
    `failed_executors` and must stay on the task's configured executor."""
    db_path = tmp_path / "runtime-perm.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="blocked:permission_denied:Write", provider_id="claude_code",
    )
    task = _make_task(executor="claude_code")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Blocked"
    assert not task.get("failed_executors")


def test_sync_task_from_run_incomplete_does_not_trigger_failover(tmp_path):
    """`incomplete:working_tree_unchanged` is task content, not provider
    unavailability — failover would only send the same too-hard task to another
    agent. It must stay "Incomplete" on its configured executor."""
    db_path = tmp_path / "runtime-incomplete.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="incomplete:working_tree_unchanged", provider_id="copilot_cli",
    )
    task = _make_task(executor="copilot_cli")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Incomplete"
    assert not task.get("failed_executors")


def test_sync_task_from_run_failed_executors_accumulate_across_providers(tmp_path):
    """Two distinct provider-unavailable failures (claude_code then copilot_cli)
    accumulate into `failed_executors` so the chain advances through every
    allowed agent before the task strands."""
    db_path = tmp_path / "runtime-accumulate.db"
    db.migrate(db_path)
    task = _make_task(executor="claude_code")

    run1 = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="session_expired", task_id="task-1", provider_id="claude_code",
    )
    run1 = db.update_run_fields(
        db_path, run1["id"], expected_version=run1["version"],
        fields={"first_output_at": "2026-01-01T00:00:30"},
    )
    task_sync.sync_task_from_run(task, run1, db_path=db_path)
    assert task.get("failed_executors") == ["claude_code"]

    # Second run on copilot_cli also fails for a provider reason. Reuse the
    # task/session created by the first `_make_run` (a fresh create_task would
    # hit the task-id unique constraint).
    session = db.list_sessions(db_path, task_id="task-1")[0]
    run2 = db.create_run(
        db_path, session_id=session["id"], task_id="task-1", project="AIOS",
        task_type="implementation", repository_path="/tmp/x", prompt="p",
        is_resume=False, provider_id="copilot_cli",
    )
    run2 = db.update_run_state(
        db_path, run2["id"], expected_version=run2["version"], new_state="QUEUED",
    )
    run2 = db.update_run_state(
        db_path, run2["id"], expected_version=run2["version"], new_state="RUNNING",
    )
    run2 = db.update_run_state(
        db_path, run2["id"], expected_version=run2["version"], new_state="FAILED",
        fields={"failure_reason": "provider_api_error", "completed_at": "2026-01-01T00:02:00"},
    )
    db.mark_run_finalized(db_path, run2["id"])
    run2 = db.get_run(db_path, run2["id"])
    task_sync.sync_task_from_run(task, run2, db_path=db_path)

    assert task.get("failed_executors") == ["claude_code", "copilot_cli"]


def test_sync_task_from_run_completed_clears_failed_executors(tmp_path):
    """A clean completion resets the chain so a future re-launch tries the
    configured executor from scratch (existing behaviour, now also reached via
    provider failover)."""
    db_path = tmp_path / "runtime-clear.db"
    db.migrate(db_path)
    task = _make_task(executor="claude_code", failed_executors=["claude_code"])
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    db.append_run_event(db_path, run["id"], "result", {"result": "Verdict: APPROVED FOR COMMIT"})

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert not task.get("failed_executors")


def test_sync_task_from_run_unknown_maps_to_requires_attention(tmp_path):
    db_path = tmp_path / "runtime-UNKNOWN.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="UNKNOWN", completed_at="2026-01-01T00:01:00")
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Requires Attention"


def test_sync_task_from_run_cancelled_gets_cancelled_status_and_closed_lane(tmp_path):
    """An operator-stopped run is not a failure: launch status is "Cancelled"
    and the task retires to the read-side "Closed" lane (AICC-IMP-006)."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="CANCELLED", completed_at="2026-01-01T00:01:00")
    task = _make_task(status="Next")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Cancelled"
    assert task["status"] == "Closed"


def test_sync_task_from_run_cancelled_never_demotes_a_done_task(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="CANCELLED", completed_at="2026-01-01T00:01:00")
    task = _make_task(status="Done")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["status"] == "Done"
    assert task["launch_status"] == "Cancelled"


def test_sync_task_from_run_blocked_maps_to_blocked_launch_status(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="blocked:permission_denied:Write",
    )
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Blocked"


def test_sync_task_from_run_incomplete_maps_to_incomplete_launch_status(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(
        db_path, state="FAILED", completed_at="2026-01-01T00:01:00",
        failure_reason="incomplete:working_tree_unchanged",
    )
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Incomplete"


def test_sync_task_from_run_waiting_maps_to_requires_attention(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    run = db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"cancel_requested": 1})
    task = _make_task()

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Requires Attention"


# --------------------------------------------------------------------------
# Manual progress override protection
# --------------------------------------------------------------------------


def test_sync_task_from_run_never_touches_progress_or_workflow_stage(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    task = _make_task(progress=77, progress_mode="manual", workflow_stage="Final Review", current_stage="Validation")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["progress"] == 77
    assert task["progress_mode"] == "manual"
    assert task["workflow_stage"] == "Final Review"
    assert task["current_stage"] == "Validation"


# --------------------------------------------------------------------------
# Idempotency: re-syncing an already-terminal run must not re-append
# timeline events or re-parse the report text a second time.
# --------------------------------------------------------------------------


def test_sync_task_from_run_is_idempotent_for_the_same_terminal_run(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="COMPLETED", completed_at="2026-01-01T00:01:00")
    db.append_run_event(db_path, run["id"], "result", {"result": "Verdict: APPROVED FOR COMMIT"})
    task = _make_task()

    parse_calls = []
    original_parse = report_parser.parse_report

    def counting_parse(text):
        parse_calls.append(text)
        return original_parse(text)

    monkeypatch.setattr(task_sync.report_parser, "parse_report", counting_parse)

    first = task_sync.sync_task_from_run(task, run, db_path=db_path)
    second = task_sync.sync_task_from_run(dict(task), run, db_path=db_path)

    assert first is True
    # Re-running against a task that already reflects this exact run's
    # terminal outcome must not mutate it again or re-parse the report.
    assert second is False
    assert len(parse_calls) == 1
    # "APPROVED FOR COMMIT" is a passing verdict with no PR URL -> stage
    # advances to "Validation" ("tests_passed") in addition to "completed".
    assert [event["type"] for event in task["timeline"]] == ["tests_passed", "completed"]


def test_sync_task_from_run_running_with_output_advances_to_implementation(tmp_path):
    """A RUNNING run that has produced output (first_output_at set) should
    advance the task stage from "Workspace Verified" to "Implementation" so
    the board shows live progress instead of staying frozen at 5%."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"first_output_at": "2026-01-01T00:00:01"})
    run = db.get_run(db_path, run["id"])
    task = _make_task(current_stage="Workspace Verified", progress=5)

    mutated = task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert mutated is True
    assert task["launch_status"] == "Running"
    assert task["current_stage"] == "Implementation"
    assert task["progress"] == 40


def test_sync_task_from_run_running_with_output_does_not_regress_advanced_stage(tmp_path):
    """If the task is already past 'Implementation' (e.g. manual override),
    a RUNNING sync must not regress it."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"first_output_at": "2026-01-01T00:00:01"})
    run = db.get_run(db_path, run["id"])
    task = _make_task(current_stage="Validation", progress=75)

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    # Only the launch_status changes; stage/progress stay.
    assert task["current_stage"] == "Validation"
    assert task["progress"] == 75


def test_sync_task_from_run_running_without_output_stays_at_workspace_verified(tmp_path):
    """A RUNNING run that has NOT yet produced output (no first_output_at)
    should NOT advance progress — it's still in the 'Starting' phase."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    task = _make_task(current_stage="Workspace Verified", progress=5)

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["launch_status"] == "Running"
    assert task["current_stage"] == "Workspace Verified"
    assert task["progress"] == 5


def test_sync_task_from_run_running_with_output_respects_manual_progress_mode(tmp_path):
    """A manual progress override must not be clobbered by the RUNNING
    advancement."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    db.update_run_fields(db_path, run["id"], expected_version=run["version"], fields={"first_output_at": "2026-01-01T00:00:01"})
    run = db.get_run(db_path, run["id"])
    task = _make_task(current_stage="Workspace Verified", progress=5, progress_mode="manual")

    task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert task["progress_mode"] == "manual"
    assert task["current_stage"] == "Workspace Verified"
    assert task["progress"] == 5


def test_sync_task_from_run_running_updates_are_cheap_and_repeatable(tmp_path):
    """Recomputing `derive_status` every refresh tick for a still-Running
    run is expected and cheap — only the one-time terminal work is guarded."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, state="RUNNING")
    task = _make_task()

    first = task_sync.sync_task_from_run(task, run, db_path=db_path)
    second = task_sync.sync_task_from_run(task, run, db_path=db_path)

    assert first is True
    assert second is False  # nothing changed the second time — same run, same status
    assert task["launch_status"] == "Running"


# --------------------------------------------------------------------------
# reconcile_and_sync — orchestration over ExecutionCenterAPI + task list
# --------------------------------------------------------------------------


def test_reconcile_and_sync_skips_tasks_without_current_run_id(tmp_path):
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    tasks = [_make_task(current_run_id=None)]

    mutated = task_sync.reconcile_and_sync(api, tasks)

    assert mutated == []
    assert tasks[0]["launch_status"] == "Ready"


def test_reconcile_and_sync_does_not_reopen_done_task_from_failed_run(tmp_path):
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    run = _make_run(
        api.db_path,
        state="FAILED",
        failure_reason="incomplete:working_tree_unchanged",
        completed_at="2026-01-01T00:01:00",
    )
    task = _make_task(
        status="Done",
        current_run_id=run["id"],
        launch_status="Completed",
        current_stage="Merged",
        progress=100,
    )

    mutated = task_sync.reconcile_and_sync(api, [task])

    assert mutated == []
    assert task["launch_status"] == "Completed"
    assert task["current_stage"] == "Merged"
    assert task["progress"] == 100


def test_reconcile_and_sync_reconciles_dead_process_and_marks_task_ready_for_retry(tmp_path):
    """A dead process that never produced output is a startup failure (e.g.
    expired OAuth). The executor is recorded in `failed_executors` and the task
    is sent back to "Ready" for auto-retry with the next available agent
    (AICC-DESKTOP-017)."""
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    run = _make_run(api.db_path, state="RUNNING")

    dead = subprocess.Popen(["true"])
    dead.wait()
    run = db.update_run_fields(api.db_path, run["id"], expected_version=run["version"], fields={"pid": dead.pid})

    task = _make_task(current_run_id=run["id"])
    tasks = [task]

    mutated = task_sync.reconcile_and_sync(api, tasks)

    assert mutated == [task]
    assert task["launch_status"] == "Ready"
    assert "claude_code" in (task.get("failed_executors") or [])
    reconciled_run = db.get_run(api.db_path, run["id"])
    assert reconciled_run["state"] == "INTERRUPTED"


def test_reconcile_and_sync_ignores_missing_run(tmp_path):
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    task = _make_task(current_run_id="does-not-exist")

    mutated = task_sync.reconcile_and_sync(api, [task])

    assert mutated == []


def test_reconcile_and_sync_recovers_stale_current_run_id_pointer(tmp_path):
    """Self-heal for the lost-update bug: a launch's `upsert_task` was clobbered
    by a concurrent full-list `save_tasks`, so `current_run_id` still points at
    an old INTERRUPTED run while a newer FAILED run (here session_expired) exists
    in the DB. `sync_tasks` must fall back to the actually-newest run for the
    task so the FAILED run is synced and executor failover fires — instead of
    stranding the task on the stale run's status forever."""
    db_path = tmp_path / "runtime-stale.db"
    db.migrate(db_path)
    old_run = _make_run(db_path, state="INTERRUPTED", provider_id="claude_code")
    # A newer run for the same task, failing with a provider-unavailable cause.
    # Reuse the task/session created by the first `_make_run` (a fresh
    # `create_task` would hit the task-id unique constraint).
    session = db.list_sessions(db_path, task_id="task-1")[0]
    new_run = db.create_run(
        db_path, session_id=session["id"], task_id="task-1", project="AIOS",
        task_type="implementation", repository_path="/tmp/x", prompt="p",
        is_resume=False, provider_id="claude_code",
    )
    new_run = db.update_run_state(db_path, new_run["id"], expected_version=new_run["version"], new_state="QUEUED")
    new_run = db.update_run_state(db_path, new_run["id"], expected_version=new_run["version"], new_state="RUNNING")
    new_run = db.update_run_state(
        db_path, new_run["id"], expected_version=new_run["version"], new_state="FAILED",
        fields={"failure_reason": "session_expired", "completed_at": "2026-01-01T00:02:00"},
    )
    new_run = db.update_run_fields(
        db_path, new_run["id"], expected_version=new_run["version"],
        fields={"first_output_at": "2026-01-01T00:01:30"},
    )
    db.mark_run_finalized(db_path, new_run["id"])
    api = runtime_api.ExecutionCenterAPI(db_path=db_path)
    # Task pointer is frozen on the OLD run — the orphaned state.
    task = _make_task(current_run_id=old_run["id"], executor="claude_code")

    mutated = task_sync.reconcile_and_sync(api, [task])

    assert task in mutated
    assert task["current_run_id"] == new_run["id"]
    assert task["launch_status"] == "Ready"
    assert task.get("failed_executors") == ["claude_code"]


def test_sync_task_from_run_records_last_provider_id_on_completed_and_failed(tmp_path):
    """Both terminal outcomes record which provider produced them (AICC-IMP-011),
    so the board and failover history survive the run window's eviction."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    run = _make_run(db_path, state="FAILED", completed_at="2026-01-01T00:01:00")
    task = _make_task()
    task_sync.sync_task_from_run(task, run, db_path=db_path)
    assert task["last_provider_id"] == "claude_code"

    run2 = _make_run(db_path, state="COMPLETED", task_id="task-2", provider_id="codex", completed_at="2026-01-01T00:02:00")
    task2 = _make_task(id="task-2")
    task_sync.sync_task_from_run(task2, run2, db_path=db_path)
    assert task2["last_provider_id"] == "codex"
