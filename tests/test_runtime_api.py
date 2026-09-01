"""Minimal Execution Center backend API: list sessions/runs, inspect status,
read events, request cancellation, reconcile — the exact surface the Sprint 1
brief calls for, plus the confirmation/context gating it must enforce.
"""

from __future__ import annotations

import inspect
import time

import pytest

from command_center.runtime import context_service, db
from command_center.runtime.api import ExecutionCenterAPI


def test_start_run_requires_confirmation(git_repo, configure_project_repo):
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    with pytest.raises(context_service.ConfirmationRequiredError):
        api.start_run(
            project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p",
            confirmed=False,
        )


def test_full_lifecycle_through_the_api(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()

    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p", confirmed=True
    )
    assert run["state"] == "RUNNING"

    final = api.supervisor.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "COMPLETED"

    # list sessions
    sessions = api.list_sessions()
    assert any(s["id"] == run["session_id"] for s in sessions)
    sessions_for_task = api.list_sessions(task_id=run["task_id"])
    assert len(sessions_for_task) == 1

    # list runs
    runs = api.list_runs()
    assert any(r["id"] == run["id"] for r in runs)
    runs_for_session = api.list_runs(session_id=run["session_id"])
    assert len(runs_for_session) == 1
    runs_by_state = api.list_runs(state="COMPLETED")
    assert any(r["id"] == run["id"] for r in runs_by_state)

    # inspect run status
    status = api.get_run(run["id"])
    assert status["state"] == "COMPLETED"

    # read incremental events
    events = api.get_events(run["id"])
    assert len(events) > 0
    assert all("payload" in e for e in events)

    # cursor-based reading
    first_seq = events[0]["seq"]
    later_events = api.get_events(run["id"], after_seq=first_seq)
    assert all(e["seq"] > first_seq for e in later_events)

    # report
    report = api.get_report(run["id"])
    assert report is not None


def test_list_runs_states_and_limit_forward_to_db(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p", confirmed=True
    )
    api.supervisor.wait_for_run(run["id"], timeout=10)

    active = api.list_runs(states=db.EXECUTION_CENTER_ACTIVE_STATES)
    assert run["id"] not in {r["id"] for r in active}

    terminal = api.list_runs(states=db.TERMINAL_STATES)
    assert run["id"] in {r["id"] for r in terminal}

    limited = api.list_runs(limit=1)
    assert len(limited) == 1


def test_api_list_and_get_run_share_the_canonical_provenance_view(
    git_repo, configure_project_repo, fake_claude
):
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        instruction="p",
        confirmed=True,
        expected_branch="main",
    )
    api.supervisor.wait_for_run(run["id"], timeout=10)

    listed = next(item for item in api.list_runs() if item["id"] == run["id"])
    fetched = api.get_run(run["id"])

    assert listed["provenance"] == fetched["provenance"]
    assert listed["provenance"]["repository"] == str(git_repo.resolve())
    assert listed["provenance"]["worktree"] == str(git_repo.resolve())
    assert listed["provenance"]["branch"] == "main"
    assert len(listed["provenance"]["base_sha"]) == 40
    assert len(listed["provenance"]["head_sha"]) == 40
    assert listed["provenance"]["provider_route"]["providers"][0] == "claude_code"
    assert listed["provenance"]["provider_route"]["selection_reason"] == "policy_filtered_preference"
    assert listed["provenance"]["provider_route"]["policy_version"] == "project_allowed_agents_v1"
    attempts = listed["provenance"]["provider_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["run_id"] == run["id"]
    assert attempts[0]["provider_id"] == "claude_code"
    assert attempts[0]["outcome"] == "succeeded"
    assert attempts[0]["classification"] == "success"
    assert attempts[0]["disposition"] == "succeeded"
    assert attempts[0]["error_code"] is None


def test_list_runs_state_and_states_together_raises_value_error(tmp_path):
    api = ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    with pytest.raises(ValueError):
        api.list_runs(state="RUNNING", states=["RUNNING"])


def test_list_and_count_runs_empty_states_return_empty_without_error(tmp_path):
    api = ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    task = db.create_task(api.db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(api.db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    db.create_run(
        api.db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )

    assert api.list_runs(states=[]) == []
    assert api.count_runs(states=[]) == 0


def test_request_cancel_requires_confirmation(git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p", confirmed=True
    )
    try:
        with pytest.raises(context_service.ConfirmationRequiredError):
            api.request_cancel(run["id"], confirmed=False)
    finally:
        api.request_cancel(run["id"], confirmed=True, grace_seconds=2)


def test_request_cancel_end_to_end(git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p", confirmed=True
    )
    time.sleep(0.3)
    result = api.request_cancel(run["id"], confirmed=True, grace_seconds=2)
    assert result["state"] == "CANCELLED"


def test_reconcile_stale_runs_through_the_api(tmp_path):
    api = ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    task = db.create_task(api.db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(api.db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        api.db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="dead-owner", finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(api.db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    db.update_run_state(
        api.db_path, run["id"], expected_version=run["version"], new_state="RUNNING",
        fields={"pid": None, "process_start_identity": None},
    )

    outcomes = api.reconcile()
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert api.get_run(run["id"])["state"] == "INTERRUPTED"


# --------------------------------------------------------------------------
# F2 — sensitive-project context boundary, enforced structurally by
# ExecutionCenterAPI.start_run itself, not merely by context_service in
# isolation. Adversarial coverage per the remediation brief.
# --------------------------------------------------------------------------


def test_public_api_has_no_raw_prompt_parameter_at_all():
    """A caller cannot bypass context assembly by supplying a prebuilt
    prompt: `start_run` has no `prompt` parameter to supply one to."""
    params = inspect.signature(ExecutionCenterAPI.start_run).parameters
    assert "prompt" not in params
    assert "instruction" in params


def test_start_run_rejects_prompt_kwarg(git_repo, configure_project_repo):
    """Even a caller who read the old (pre-remediation) signature and tries
    `prompt=` gets a hard `TypeError`, not a silently-accepted bypass."""
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    with pytest.raises(TypeError):
        api.start_run(
            project="AIOS", repository_path=str(git_repo), task_type="implementation",
            prompt="attempted raw-prompt bypass", confirmed=True,
        )


@pytest.mark.parametrize("project_id", ["BANK", "LEGAL"])
def test_sensitive_project_raw_content_excluded_from_final_prompt_without_confirmation(
    project_id, git_repo, configure_project_repo, fake_claude
):
    configure_project_repo(project_id, git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project=project_id, repository_path=str(git_repo), task_type="implementation",
        instruction="Summarize this.", confirmed=True,
        candidate_content={"file:secret.md": "TOP SECRET FINANCIAL DATA"},
        confirmed_items=None,
    )
    stored_run = db.get_run(api.db_path, run["id"])
    assert "TOP SECRET FINANCIAL DATA" not in stored_run["prompt"], (
        "unconfirmed sensitive content must never reach the outbound prompt"
    )
    assert run["context_manifest"]["excluded_content_keys"] == ["file:secret.md"]
    assert run["context_manifest"]["included_content_keys"] == []
    api.supervisor.wait_for_run(run["id"], timeout=10)


@pytest.mark.parametrize("project_id", ["BANK", "LEGAL"])
def test_sensitive_project_confirmed_content_is_included(project_id, git_repo, configure_project_repo, fake_claude):
    configure_project_repo(project_id, git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project=project_id, repository_path=str(git_repo), task_type="implementation",
        instruction="Summarize this.", confirmed=True,
        candidate_content={"file:secret.md": "TOP SECRET FINANCIAL DATA"},
        confirmed_items=["file:secret.md"],
    )
    stored_run = db.get_run(api.db_path, run["id"])
    assert stored_run["prompt"] == "[redacted: prompt transported via stdin]"
    assert run["context_manifest"]["included_content_keys"] == ["file:secret.md"]
    assert run["context_manifest"]["excluded_content_keys"] == []
    api.supervisor.wait_for_run(run["id"], timeout=10)


def test_non_sensitive_project_retains_automatic_inclusion(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation",
        instruction="Summarize this.", confirmed=True,
        candidate_content={"file:notes.md": "ordinary project notes"},
        confirmed_items=None,
    )
    stored_run = db.get_run(api.db_path, run["id"])
    assert stored_run["prompt"] == "[redacted: prompt transported via stdin]"
    assert run["context_manifest"]["included_content_keys"] == ["file:notes.md"]
    api.supervisor.wait_for_run(run["id"], timeout=10)


def test_caller_cannot_mark_a_sensitive_project_non_sensitive():
    """There is no parameter anywhere on the public API to override
    sensitivity — it is derived solely from `project_config` inside
    `context_service`."""
    params = inspect.signature(ExecutionCenterAPI.start_run).parameters
    assert "sensitive" not in params
    assert "is_sensitive" not in params
    bundle = context_service.assemble_context(project_id="BANK", candidate_content={"k": "v"})
    assert bundle["sensitive"] is True


def test_outbound_manifest_exactly_identifies_what_left_the_machine(git_repo, configure_project_repo, fake_claude):
    configure_project_repo("BANK", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="BANK", repository_path=str(git_repo), task_type="implementation",
        instruction="Summarize.", confirmed=True,
        candidate_content={
            "file:confirmed.md": "confirmed content",
            "file:unconfirmed.md": "unconfirmed content",
        },
        confirmed_items=["file:confirmed.md"],
    )
    manifest = run["context_manifest"]
    assert manifest["included_content_keys"] == ["file:confirmed.md"]
    assert manifest["excluded_content_keys"] == ["file:unconfirmed.md"]
    assert manifest["included_content_sizes"] == {"file:confirmed.md": len("confirmed content")}
    assert manifest["sensitive"] is True

    # The manifest is independently auditable via the events API, not only
    # via the return value of start_run.
    events = api.get_events(run["id"])
    manifest_events = [e for e in events if e["event_type"] == "context_manifest"]
    assert len(manifest_events) == 1
    assert manifest_events[0]["payload"] == manifest

    stored_run = db.get_run(api.db_path, run["id"])
    assert stored_run["prompt"] == "[redacted: prompt transported via stdin]"
    api.supervisor.wait_for_run(run["id"], timeout=10)


# --------------------------------------------------------------------------
# context_service.assemble_context (dry-run preview, launches nothing)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("project_id", ["BANK", "LEGAL"])
def test_assemble_context_excludes_unconfirmed_sensitive_content(project_id, tmp_path):
    api = ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    bundle = api.assemble_context(
        project_id=project_id, candidate_content={"report:r1": "secret"}, confirmed_items=None
    )
    assert bundle["content"] == {}
    assert bundle["sensitive"] is True


def test_assemble_context_for_non_sensitive_project_includes_content(tmp_path):
    api = ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    bundle = api.assemble_context(project_id="AIOS", candidate_content={"file:a.md": "hello"}, confirmed_items=None)
    assert bundle["content"] == {"file:a.md": "hello"}


def test_plan_schedule_reads_live_load_and_defers_busy_workspace(git_repo, configure_project_repo, fake_claude):
    """`plan_schedule` is a read-only decision: it projects the live in-flight
    load from the API's own db, so a workspace with an active run is deferred,
    and it never launches anything itself."""
    from command_center.runtime import scheduler

    # Keep the fake process alive long enough for `plan_schedule` (which probes
    # every registered executor's binary) to return while the run is still
    # RUNNING — without this the ~0.2 s default output window can close first.
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)
    api = ExecutionCenterAPI()
    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", instruction="p", confirmed=True,
        timeout_seconds=None,
    )
    assert run["state"] == "RUNNING"
    try:
        plan = api.plan_schedule([scheduler.WorkItem(task_id="t-new", workspace=str(git_repo))], now="2026-07-23T12:00:00")
        assert len(plan.assignments()) == 0
        assert plan.deferrals()[0].reason_code == scheduler.REASON_WORKSPACE_BUSY
    finally:
        api.request_cancel(run["id"], confirmed=True)
        api.supervisor.wait_for_run(run["id"], timeout=10)
