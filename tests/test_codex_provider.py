from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from command_center import (
    launch,
    launch_service,
    models,
    project_config,
    provider_route,
    storage,
)
from command_center.runtime import api as runtime_api
from command_center.runtime import (
    db,
    identity,
    providers,
    reports,
    session_view,
    supervisor,
    task_sync,
)


def _add_worktree(repo: Path, target: Path, branch: str = "feature/codex-test") -> Path:
    subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(target), "HEAD"], cwd=repo, check=True)
    return target


def _start_codex(sup, *, canonical, worktree, branch="feature/codex-test", **kwargs):
    return sup.start_raw(
        project="AIOS",
        repository_path=str(worktree),
        canonical_repository_path=str(canonical),
        expected_branch=branch,
        task_type=kwargs.pop("task_type", "review"),
        prompt=kwargs.pop("prompt", "summarize the recent changes"),
        confirmed=True,
        repository_already_validated=True,
        executor_id="codex",
        **kwargs,
    )


def _persisted_database_text(db_path: Path) -> str:
    """Serialize every user table cell from the actual SQLite database."""
    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        rows = {
            table: conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608 - names come from sqlite_master
            for table in tables
        }
    return json.dumps(rows, ensure_ascii=False, default=str)


def _report_text(db_path: Path, run_id: str) -> str:
    report = db.get_report(db_path, run_id)
    assert report is not None
    return (reports.REPORTS_ROOT.parent / report["path"]).read_text(encoding="utf-8")


def test_codex_discovery_and_version_probe_use_fake_only(fake_codex):
    availability = providers.get_provider("codex").availability()
    assert availability.available is True
    assert availability.code == "usable"
    assert availability.executable == str(fake_codex)
    assert availability.version == "codex-cli 0.145.0-fake"


def test_codex_missing_executable_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setenv("AICC_CODEX_BINARY", str(tmp_path / "missing"))
    availability = providers.get_provider("codex").availability()
    assert availability.available is False
    assert availability.code == "executable_missing"
    assert "install" in availability.message.lower() or "configure" in availability.message.lower()


def test_codex_unsupported_interface_fails_closed(fake_codex, monkeypatch):
    original = providers._probe

    def probe(executable, args, *, provider_id):
        if args == ["exec", "--help"]:
            return True, "old incompatible help"
        return original(executable, args, provider_id=provider_id)

    monkeypatch.setattr(providers, "_probe", probe)
    availability = providers.get_provider("codex").availability()
    assert availability.available is False
    assert availability.code == "unsupported_interface"


def test_codex_version_probe_failure_is_distinct(fake_codex, monkeypatch):
    monkeypatch.setattr(
        providers,
        "_probe",
        lambda executable, args, *, provider_id: (False, "codex version/interface probe failed (exit 9)"),
    )
    availability = providers.get_provider("codex").availability()
    assert availability.available is False
    assert availability.code == "version_probe_failed"


def test_codex_argv_is_fixed_and_prompt_is_stdin_only(fake_codex, git_repo):
    prompt = "secret; $(whoami); rm -rf /"
    spec = providers.get_provider("codex").build_launch(
        repository_path=git_repo,
        session_id="unused",
        prompt=prompt,
        task_type="implementation",
        is_resume=False,
        model=None,
    )
    assert spec.argv == (
        str(fake_codex), "exec", "--json", "--color", "never", "--sandbox",
        "workspace-write", "--cd", str(git_repo), "-",
    )
    assert prompt not in spec.argv
    assert spec.stdin_text == prompt
    assert spec.audit_metadata["prompt_transport"] == "stdin"
    assert "secret" not in json.dumps(spec.audit_metadata)


def test_supervisor_never_uses_shell_true_for_provider_launches():
    source = Path(supervisor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    popen_calls = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"]
    assert popen_calls
    assert all(
        any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            for keyword in call.keywords)
        for call in popen_calls
    )


def test_codex_rejects_canonical_checkout(fake_codex, git_repo):
    sup = supervisor.Supervisor()
    with pytest.raises(supervisor.SupervisorError, match="canonical checkout"):
        _start_codex(sup, canonical=git_repo, worktree=git_repo)
    assert db.list_runs(sup.db_path) == []


def test_codex_rejects_wrong_or_missing_task_branch(fake_codex, git_repo, tmp_path):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    sup = supervisor.Supervisor()
    with pytest.raises(supervisor.SupervisorError, match="intended task branch"):
        _start_codex(sup, canonical=git_repo, worktree=worktree, branch=None)
    with pytest.raises(supervisor.SupervisorError, match="Unsafe Codex target worktree"):
        _start_codex(sup, canonical=git_repo, worktree=worktree, branch="feature/wrong")


def test_codex_launch_handshake_prompt_transport_and_audit(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    capture = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_CODEX_PROMPT_CAPTURE", str(capture))
    prompt = "sensitive prompt that must not be argv-visible"
    sup = supervisor.Supervisor()
    run = _start_codex(
        sup, canonical=git_repo, worktree=worktree, prompt=prompt, title=prompt
    )
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "COMPLETED", final["failure_reason"]
    assert final["provider_id"] == "codex"
    assert final["prompt"].startswith("[redacted:")
    assert prompt not in final["command_json"]
    assert prompt not in db.get_task(sup.db_path, final["task_id"])["title"]
    assert capture.read_text(encoding="utf-8") == prompt
    metadata = json.loads(final["provider_metadata_json"])
    assert metadata["provider_id"] == "codex"
    assert metadata["prompt_transport"] == "stdin"
    assert final["first_output_at"]
    events = db.list_run_events(sup.db_path, run["id"])
    assert any(event["payload"].get("lifecycle") == "prompt_delivered" for event in events)
    assert any(event["event_type"] == "assistant_message" for event in events)
    assert session_view.derive_status(final) == session_view.STATUS_COMPLETED


def test_codex_duplicate_prevention_and_cancellation(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    monkeypatch.delenv("FAKE_CODEX_TOUCH_FILE")
    sup = supervisor.Supervisor()
    first = _start_codex(sup, canonical=git_repo, worktree=worktree)
    with pytest.raises(supervisor.WorkspaceLockedError):
        _start_codex(sup, canonical=git_repo, worktree=worktree)
    cancelled = sup.cancel(first["id"], confirmed=True, grace_seconds=1)
    assert cancelled["state"] == "CANCELLED"


def test_cancellation_refuses_unverifiable_reused_pid(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    # A reused PID cannot be signalled here, but the mechanism is ownership,
    # not identity comparison: `_terminate_active_process` holds
    # `process_control_lock` while it observes leader exit and signals the
    # process group id captured at launch. While this instance's Popen handle
    # is unreaped the kernel cannot reuse that PID, so there is no window in
    # which the recorded identity could disagree. Identity comparison remains
    # the mechanism for processes this instance did *not* spawn — and those it
    # refuses to signal at all (see `reconcile`, which never re-registers an
    # adopted orphan as an active run).
    assert run["process_start_identity"]
    assert identity.identity_matches(run["pid"], run["process_start_identity"])
    sup.cancel(run["id"], confirmed=True, grace_seconds=1)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "CANCELLED"
    assert final["cancel_requested"] == 1


@pytest.mark.serial  # SIGTERM-grace timing: the 0.1s escalation window misses under xdist CPU load
def test_codex_ignored_termination_escalates(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    monkeypatch.setenv("FAKE_CODEX_IGNORE_SIGTERM", "1")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not db.get_run(sup.db_path, run["id"])["first_output_at"]:
        time.sleep(0.02)
    final = sup.cancel(run["id"], confirmed=True, grace_seconds=0.1)
    assert final["state"] == "CANCELLED"
    events = db.list_run_events(sup.db_path, run["id"])
    assert any(event["payload"].get("lifecycle") == "cancel_sigkill_sent" for event in events)


@pytest.mark.serial
def test_cancel_waits_for_supervision_writer_after_process_exit(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    monkeypatch.setenv("FAKE_CODEX_IGNORE_SIGTERM", "1")
    finalizer_entered = threading.Event()
    release_finalizer = threading.Event()
    cancel_finished = threading.Event()
    original_finish = db.finish_provider_attempt

    def blocked_finish(*args, **kwargs):
        finalizer_entered.set()
        release_finalizer.wait()
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(db, "finish_provider_attempt", blocked_finish)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    with sup._active_lock:
        active = sup._active[run["id"]]
    assert active.handshake_recorded.wait(timeout=3)
    outcome = {}

    def cancel_run():
        try:
            outcome["run"] = sup.cancel(run["id"], confirmed=True, grace_seconds=0.1)
        except Exception as exc:  # noqa: BLE001 - thread outcome is asserted below
            outcome["error"] = exc
        finally:
            cancel_finished.set()

    thread = threading.Thread(target=cancel_run)
    thread.start()
    try:
        assert finalizer_entered.wait(timeout=3)
        assert not cancel_finished.wait(timeout=0.2)
    finally:
        release_finalizer.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["run"]["state"] == "CANCELLED"
    assert outcome["run"]["finalized_at"]
    assert run["id"] not in sup.active_run_ids()


@pytest.mark.serial
def test_cancel_recovers_started_attempt_after_terminal_row_was_written(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    original_finish = db.finish_provider_attempt
    failures_remaining = 1

    def fail_first_finish(*args, **kwargs):
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise sqlite3.OperationalError("injected attempt-finalization failure")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(db, "finish_provider_attempt", fail_first_finish)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    with sup._active_lock:
        active = sup._active[run["id"]]
    assert active.handshake_recorded.wait(timeout=3)

    final = sup.cancel(run["id"], confirmed=True, grace_seconds=1)

    assert final["state"] == "CANCELLED"
    assert final["finalized_at"]
    attempts = db.list_provider_attempts(sup.db_path, run["id"])
    assert attempts[-1]["outcome"] == "cancelled"
    assert attempts[-1]["classification"] == provider_route.CANCELLED


@pytest.mark.serial
def test_cancel_recovers_transient_finalization_marker_failure(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    original_mark = db.mark_run_finalized
    failures_remaining = 1

    def fail_first_mark(*args, **kwargs):
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise sqlite3.OperationalError("injected finalization-marker failure")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(db, "mark_run_finalized", fail_first_mark)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    with sup._active_lock:
        active = sup._active[run["id"]]
    assert active.handshake_recorded.wait(timeout=3)

    final = sup.cancel(run["id"], confirmed=True, grace_seconds=1)

    assert final["state"] == "CANCELLED"
    assert final["finalized_at"]
    assert run["id"] not in sup.active_run_ids()


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [("quota", "quota_limit"), ("auth", "authentication_failed"), ("startup_failure", "provider_exit_nonzero")],
)
def test_codex_failure_classification(fake_codex, git_repo, tmp_path, monkeypatch, scenario, reason):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "FAILED"
    assert final["failure_reason"] == reason
    assert any(event["event_type"] == "stderr_line" for event in db.list_run_events(sup.db_path, run["id"]))


def test_codex_malformed_output_is_persisted_without_crashing(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "malformed")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "COMPLETED"
    assert any(event["event_type"] == "malformed" for event in db.list_run_events(sup.db_path, run["id"]))


def test_codex_stderr_redacts_authentication_material(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "Authorization: Bearer secret-token api_key=sk-abcdefghijk")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    assert sup.wait_for_run(run["id"], timeout=10)["state"] == "COMPLETED"
    serialized = json.dumps(db.list_run_events(sup.db_path, run["id"]))
    assert "secret-token" not in serialized
    assert "sk-abcdefghijk" not in serialized
    assert "[REDACTED]" in serialized


def test_codex_startup_delay_stays_starting_then_runs(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_INITIAL_DELAY", "0.4")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    fresh = db.get_run(sup.db_path, run["id"])
    assert fresh["state"] == "RUNNING"
    assert session_view.is_awaiting_handshake(fresh)
    assert sup.wait_for_run(run["id"], timeout=10)["state"] == "COMPLETED"


def test_codex_fake_executable_is_the_only_launched_binary(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    seen = []
    original = subprocess.Popen

    def guarded(command, *args, **kwargs):
        if command[0] == str(fake_codex):
            seen.append(list(command))
        elif Path(command[0]).name == "codex":
            raise AssertionError("a non-fixture Codex executable was invoked")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "Popen", guarded)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    assert sup.wait_for_run(run["id"], timeout=10)["state"] == "COMPLETED"
    assert seen


def test_launch_service_selects_codex_through_existing_api(
    fake_codex, git_repo, tmp_path, configure_project_repo
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    configure_project_repo("AIOS", git_repo)
    api = runtime_api.ExecutionCenterAPI()
    validation = launch.validate_launch(
        workspace_path=str(worktree), expected_branch="feature/codex-test"
    )
    run = launch_service.execute_agent_launch_v2(
        project="AIOS",
        task_type="review",
        prompt="summarize the recent changes",
        timeout_seconds=30,
        repository_path=worktree,
        execution_center_api=api,
        confirmed=True,
        executor_id="codex",
        validation=validation,
        expected_branch="feature/codex-test",
    )
    final = api.supervisor.wait_for_run(run["id"], timeout=10)
    assert final["provider_id"] == "codex"
    assert final["state"] == "COMPLETED"


def test_codex_restart_reconciliation_never_fabricates_success(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path=str(tmp_path / "worktree")
    )
    run = db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path=str(tmp_path / "worktree"),
        prompt="[redacted]",
        is_resume=False,
        provider_id="codex",
    )
    outcome = supervisor.Supervisor(db_path).reconcile()
    final = db.get_run(db_path, run["id"])
    assert outcome[0]["classification"] == "INTERRUPTED"
    assert final["state"] == "INTERRUPTED"
    assert final["state"] != "COMPLETED"


def test_codex_restart_reconciliation_uses_real_fake_process_identity(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_EXTRA_SLEEP", "5")
    monkeypatch.delenv("FAKE_CODEX_TOUCH_FILE")
    db_path = tmp_path / "restart-runtime.db"
    original = supervisor.Supervisor(db_path)
    run = _start_codex(original, canonical=git_repo, worktree=worktree)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if db.get_run(db_path, run["id"])["first_output_at"]:
            break
        time.sleep(0.02)
    restarted = supervisor.Supervisor(db_path)
    outcomes = restarted.reconcile()
    assert outcomes == [
        {
            "run_id": run["id"],
            "classification": "RUNNING",
            "detail": "pid exists and identity matches; orphaned from this supervisor instance",
        }
    ]
    assert db.get_run(db_path, run["id"])["state"] == "RUNNING"
    assert db.get_run(db_path, run["id"])["state"] != "COMPLETED"
    original.cancel(run["id"], confirmed=True, grace_seconds=1)


def test_provider_fields_are_additive_and_default_claude(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    task = db.create_task(db_path, project="AIOS", title="t", task_type="review")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="review",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )
    assert run["provider_id"] == "claude_code"
    assert run["provider_metadata_json"] is None


def test_unknown_provider_configuration_fails_before_persistence(git_repo):
    sup = supervisor.Supervisor()
    with pytest.raises(ValueError, match="Unknown execution provider"):
        sup.start_raw(
            project="AIOS",
            repository_path=str(git_repo),
            task_type="review",
            prompt="p",
            confirmed=True,
            repository_already_validated=True,
            executor_id="malformed-provider",
        )
    assert db.list_runs(sup.db_path) == []


def test_legacy_project_policy_is_claude_only_and_codex_rejection_precedes_persistence(
    fake_codex, git_repo, tmp_path, configure_project_repo
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    project_config.save_allowed_agents("AIOS", ["claude_code"])
    configure_project_repo("AIOS", git_repo)
    api = runtime_api.ExecutionCenterAPI()

    with pytest.raises(project_config.ProviderAuthorizationError, match="not authorized"):
        api.start_run(
            project="AIOS",
            repository_path=str(worktree),
            task_type="review",
            instruction="private instruction",
            confirmed=True,
            repository_already_validated=True,
            expected_branch="feature/codex-test",
            executor_id="codex",
        )

    assert api.list_tasks() == []
    assert api.list_sessions() == []
    assert api.list_runs() == []


def test_launch_service_enforces_claude_only_policy_before_task_mutation(
    fake_codex, git_repo, tmp_path
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    project_config.save_allowed_agents("AIOS", ["claude_code"])
    task = {"id": "policy-task", "title": "Policy task", "prompt_history": []}
    api = runtime_api.ExecutionCenterAPI()

    with pytest.raises(project_config.ProviderAuthorizationError, match="not authorized"):
        launch_service.execute_agent_launch_v2(
            project="AIOS",
            task_type="review",
            prompt="must not persist",
            timeout_seconds=30,
            repository_path=worktree,
            execution_center_api=api,
            confirmed=True,
            task=task,
            executor_id="codex",
            expected_branch="feature/codex-test",
        )

    assert task == {"id": "policy-task", "title": "Policy task", "prompt_history": []}
    assert db.list_runs(api.db_path) == []
    assert db.list_sessions(api.db_path) == []
    assert db.list_tasks(api.db_path) == []


@pytest.mark.parametrize("policy", [[], "codex", ["codex", "unknown-provider"]])
def test_malformed_or_unknown_project_policy_fails_closed(policy):
    storage.atomic_write_json(project_config.CONFIG_FILE, {"AIOS": {"allowed_agents": policy}})
    with pytest.raises(project_config.ProviderAuthorizationError, match="policy|unknown"):
        project_config.allowed_execution_providers("AIOS")


def test_missing_policy_safely_defaults_to_claude_only():
    storage.atomic_write_json(project_config.CONFIG_FILE, {"AIOS": {"repository_path": "/tmp/example"}})
    assert project_config.allowed_execution_providers("AIOS") == ("claude_code",)


def test_codex_rejects_symlinked_and_dirty_worktrees_before_persistence(
    fake_codex, git_repo, tmp_path
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    symlink = tmp_path / "worktree-link"
    symlink.symlink_to(worktree, target_is_directory=True)

    symlink_sup = supervisor.Supervisor(tmp_path / "symlink-runtime.db")
    with pytest.raises(supervisor.SupervisorError, match="symlinked"):
        _start_codex(symlink_sup, canonical=git_repo, worktree=symlink)
    assert db.list_runs(symlink_sup.db_path) == []

    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    dirty_sup = supervisor.Supervisor(tmp_path / "dirty-runtime.db")
    with pytest.raises(supervisor.SupervisorError, match="Unsafe Codex target worktree"):
        _start_codex(dirty_sup, canonical=git_repo, worktree=worktree)
    assert db.list_runs(dirty_sup.db_path) == []
    assert db.list_sessions(dirty_sup.db_path) == []
    assert db.list_tasks(dirty_sup.db_path) == []


@pytest.mark.parametrize(
    ("lines", "stderr", "expected_reason", "has_handshake"),
    [
        (["not-json{{{"], None, "incomplete:provider_handshake_missing", False),
        ([json.dumps({"type": "future.event", "message": "warning"})], None,
         "incomplete:provider_handshake_missing", False),
        ([json.dumps({"type": "thread.started"})], None,
         "incomplete:provider_result_missing", True),
    ],
)
def test_codex_handshake_and_result_require_recognized_evidence(
    fake_codex, git_repo, tmp_path, monkeypatch, lines, stderr, expected_reason, has_handshake
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    if stderr:
        monkeypatch.setenv("FAKE_CODEX_STDERR", stderr)
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "FAILED"
    assert final["failure_reason"] == expected_reason
    assert bool(final["first_output_at"]) is has_handshake


def test_codex_stderr_cannot_satisfy_handshake(fake_codex, git_repo, tmp_path, monkeypatch):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "startup_failure")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "FAILED"
    assert final["first_output_at"] is None


def test_codex_valid_multi_event_sequence_normalizes_result_and_report(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    first = "First normalized output"
    final_text = "Verdict: APPROVED FOR COMMIT\nSecond normalized output"
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": first}}),
        json.dumps({"type": "future.event", "result": "must not replace"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final_text}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}}),
    ]
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    completed = sup.wait_for_run(run["id"], timeout=10)
    assert completed["state"] == "COMPLETED"

    events = db.list_run_events(sup.db_path, run["id"])
    assert reports.result_text(events) == final_text
    report = _report_text(sup.db_path, run["id"])
    assert report.index(first) < report.index(final_text)
    assert any(
        event["event_type"] == "unknown_type" and event["payload"].get("result") == "must not replace"
        for event in events
    )
    assert reports.result_text(events) != "must not replace"

    task = {
        "id": run["task_id"],
        "title": "Projection",
        "project": "AIOS",
        "progress": 0,
        "current_stage": "Backlog",
        "launch_status": "Not Started",
    }
    models.normalize_task_workflow(task)
    models.normalize_task_execution(task)
    assert task_sync.sync_task_from_run(task, completed, db_path=sup.db_path)
    assert task["latest_verdict"] == models.VERDICT_APPROVED_FOR_COMMIT


def test_codex_structured_output_is_sanitized_before_sqlite_and_report_persistence(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    prompt = "PROMPT-SENTINEL-34981 never persist this instruction"
    uppercase_token = "SK-UPPERCASESECRET123456"
    bearer = "Bearer bearer-secret-987654"
    quoted_assignment = 'API_KEY="quoted-secret-246810"'
    environment_secret = "environment-derived-sentinel-112233"
    monkeypatch.setenv("AICC_TEST_ACCESS_TOKEN", environment_secret)
    result = (
        f"{prompt}\n{uppercase_token}\n{bearer}\n{quoted_assignment}\n{environment_secret}"
    )
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "secret-thread"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": result}}),
        json.dumps({"type": "turn.completed"}),
    ]
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree, prompt=prompt)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["state"] == "COMPLETED"

    persisted = _persisted_database_text(sup.db_path)
    report = _report_text(sup.db_path, run["id"])
    for secret in (
        prompt,
        uppercase_token,
        "bearer-secret-987654",
        "quoted-secret-246810",
        environment_secret,
    ):
        assert secret not in persisted
        assert secret not in report
    assert "[REDACTED]" in persisted
    metadata = json.loads(final["provider_metadata_json"])
    assert prompt not in json.dumps(metadata)
    assert "environment" not in metadata
    assert db.get_task(sup.db_path, final["task_id"])["title"] == "Codex CLI run"


def test_codex_malformed_stdout_and_stderr_are_prompt_aware_before_persistence(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    prompt = "PROMPT-MALFORMED-SENTINEL-135790"
    lines = [
        f"malformed echo: {prompt}",
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "safe result"}}),
        json.dumps({"type": "turn.completed"}),
    ]
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    monkeypatch.setenv("FAKE_CODEX_STDERR", f"stderr prompt echo {prompt}")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree, prompt=prompt)
    assert sup.wait_for_run(run["id"], timeout=10)["state"] == "COMPLETED"

    persisted = _persisted_database_text(sup.db_path)
    report = _report_text(sup.db_path, run["id"])
    assert prompt not in persisted
    assert prompt not in report
    assert any(event["event_type"] == "malformed" for event in db.list_run_events(sup.db_path, run["id"]))


def test_codex_redacts_prompt_and_credentials_split_across_lines(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    prompt = "PROMPT-FRAGMENT-ALPHAOMEGA"
    lines = [
        "PROMPT-FRAGMENT-",
        "ALPHAOMEGA",
        "SK-SPLIT",
        "SECRET123456",
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "safe"}}),
        json.dumps({"type": "turn.completed"}),
    ]
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree, prompt=prompt)
    assert sup.wait_for_run(run["id"], timeout=10)["state"] == "COMPLETED"
    persisted = _persisted_database_text(sup.db_path)
    assert "PROMPT-FRAGMENT-" not in persisted
    assert "ALPHAOMEGA" not in persisted
    assert "SK-SPLIT" not in persisted
    assert "SECRET123456" not in persisted


def test_sanitization_boundary_redacts_chunk_split_and_json_escaped_values():
    prompt = 'PROMPT-CHUNK-SENTINEL\nquoted "value"'
    boundary = providers.SanitizationBoundary(prompt)
    assert boundary.feed_stderr("Bearer chunk-") == []
    assert boundary.feed_stderr("secret-123456\n") == []
    assert boundary.feed_stdout('{"type":"error","message":"PROMPT-CHUNK-') == []
    assert boundary.feed_stdout('SENTINEL\\\\nquoted \\\\\"value\\\\\""}\n') == []
    persisted = "".join(
        boundary.flush_stderr() + boundary.flush_stdout()
    )
    assert "chunk-secret-123456" not in persisted
    assert "PROMPT-CHUNK-SENTINEL" not in persisted
    assert 'quoted \\\\"value\\\\"' not in persisted


def test_codex_prompt_safety_limit_fails_before_runtime_persistence(fake_codex, git_repo, tmp_path):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    sup = supervisor.Supervisor()
    with pytest.raises(supervisor.ProviderUnavailableError, match="safety limit"):
        _start_codex(
            sup,
            canonical=git_repo,
            worktree=worktree,
            prompt="x" * (providers.MAX_CODEX_PROMPT_CHARS + 1),
        )
    assert db.list_tasks(sup.db_path) == []
    assert db.list_sessions(sup.db_path) == []
    assert db.list_runs(sup.db_path) == []


def test_codex_failure_classification_ignores_assistant_and_result_text(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    lines = [
        json.dumps({"type": "thread.started"}),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "The task discussed quota and authentication as ordinary content.",
            },
        }),
        json.dumps({"type": "turn.completed"}),
    ]
    monkeypatch.setenv("FAKE_CODEX_LINES", json.dumps(lines))
    monkeypatch.setenv("FAKE_CODEX_EXIT_CODE", "9")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["failure_reason"] == "provider_exit_nonzero"


@pytest.mark.parametrize(
    ("provider_error", "expected"),
    [
        ("usage quota exhausted", "quota_limit"),
        ("authentication required", "authentication_failed"),
    ],
)
def test_codex_structured_provider_errors_are_classified(
    fake_codex, git_repo, tmp_path, monkeypatch, provider_error, expected
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    monkeypatch.setenv(
        "FAKE_CODEX_LINES",
        json.dumps([json.dumps({"type": "error", "message": provider_error})]),
    )
    monkeypatch.setenv("FAKE_CODEX_EXIT_CODE", "9")
    sup = supervisor.Supervisor()
    run = _start_codex(sup, canonical=git_repo, worktree=worktree)
    final = sup.wait_for_run(run["id"], timeout=10)
    assert final["failure_reason"] == expected


class _StubProcess:
    """Minimal stand-in for a `Popen`. `_ActiveRun` pins the launch-time pid as
    the process group id in its constructor (resolving it later would open a
    PID-reuse window), so a bare `object()` is no longer sufficient."""

    pid = -1

    @staticmethod
    def poll():
        return None


def test_provider_diagnostic_evidence_is_explicitly_bounded():
    active = supervisor._ActiveRun(
        process=_StubProcess(),
        run_id="bounded",
        provider=providers.get_provider("codex"),
        provider_runtime=providers.CodexRuntime("safe prompt"),
    )
    for _ in range(supervisor.MAX_PROVIDER_DIAGNOSTIC_EVENTS + 50):
        active.add_diagnostic("x" * 4096)
    evidence = active.diagnostic_lines()
    assert len(evidence) <= supervisor.MAX_PROVIDER_DIAGNOSTIC_EVENTS
    assert sum(len(line.encode("utf-8")) for line in evidence) <= supervisor.MAX_PROVIDER_DIAGNOSTIC_BYTES


def test_identity_capture_failure_cleans_spawn_and_never_enters_running(
    fake_codex, git_repo, tmp_path, monkeypatch
):
    worktree = _add_worktree(git_repo, tmp_path / "worktree")
    # Observe the spawned child directly rather than a particular cleanup
    # helper: the property that matters is that the process is gone, not which
    # ownership-aware path terminated it.
    spawned = []
    real_popen = supervisor.subprocess.Popen

    def capturing_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(supervisor, "_capture_stable_process_identity", lambda _process: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", capturing_popen)
    sup = supervisor.Supervisor()
    # Signalled by exception, not by returning a FAILED run: `start_raw`'s
    # callers (`execution_queue.launch_ready`, the autopilot dispatcher) treat a
    # returned run as a started attempt and would record the queue entry as
    # LAUNCHED. Every safety property below is unchanged — the spawn is
    # terminated, nothing reaches RUNNING, and the row is terminal.
    with pytest.raises(supervisor.SupervisorError, match="capture process identity"):
        _start_codex(sup, canonical=git_repo, worktree=worktree)
    persisted = db.list_runs(sup.db_path)[0]
    assert persisted["state"] == "FAILED"
    assert persisted["failure_reason"] == "launch_setup_failed"
    assert persisted["started_at"] is None
    assert spawned and spawned[0].poll() is not None
    assert db.list_runs(sup.db_path, state="RUNNING") == []
