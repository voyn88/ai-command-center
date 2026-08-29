"""The agent_run bridge (SRV-05 slice 2): payload contract and outcome
discipline, with the runner faked at its module seam -- no subprocess, no
claude binary, no repository on disk unless a test builds one."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from command_center import agent_runner, workspace_provisioning
from command_center.orchestrator.publish import PublishResult
from command_center.worker.handlers import build_handlers
from command_center.worker.payloads import PayloadError, parse_agent_run


def _payload(**overrides):
    payload = {
        "v": 1,
        "project_id": "proj",
        "repository_path": "/tmp/repo",
        "prompt": "do the thing",
        "task_type": "review",
        "timeout_seconds": 120,
        "untrusted": False,
    }
    payload.update(overrides)
    return payload


def _event() -> threading.Event:
    return threading.Event()


def isolated_path(repository: Path, backlog_task: str = "proj") -> Path:
    """The isolated worktree path `_run_agent` derives for a mutating
    dispatch of `repository`/`backlog_task` -- mirrors
    `handlers._isolated_workspace_path` (`<repo>-worktrees/backlog-<task>`)
    so a test can predict where the lease gate and provisioning will look
    without importing the private helper itself."""
    return workspace_provisioning.task_workspace_path(
        repository, f"backlog/{backlog_task}"
    )


@dataclass(frozen=True)
class _FakeEvidence:
    workspace_path: str
    expected_branch: str
    remote_url: str
    start_sha: str
    base_sha: str
    remote_task_sha: str | None
    workspace_device: int
    workspace_inode: int
    provision_outcome: str


@pytest.fixture
def handler(monkeypatch, tmp_path):
    """The agent_run handler with every external seam faked to succeed.

    Workspace provisioning is faked too, at the same seam level as
    `run_claude_code`: these are generic outcome-discipline tests, not
    isolation tests, and a real `git worktree add` would need an actual
    repository under `tmp_path` for every one of them. The fake hands back
    exactly the path `_run_agent` asked for (`spec.workspace_path`), so the
    dispatch flow -- including which path reaches `run_claude_code` and the
    lease gate -- is unchanged from a real provision; only the git mutation
    itself is skipped. Real provisioning end-to-end (a real repo, a real
    worktree, real cleanup) is exercised separately in
    `test_isolated_workspace.py`.
    """
    monkeypatch.setattr(
        agent_runner, "validate_repository", lambda project_id, path: tmp_path
    )
    monkeypatch.setattr(
        agent_runner, "claude_cli_preflight", lambda binary=None: (True, "ok")
    )
    runs: list[dict] = []

    def fake_run(**kwargs):
        runs.append(kwargs)
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=1.5,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run)
    monkeypatch.setattr(
        workspace_provisioning,
        "provision_and_verify",
        lambda spec: _FakeEvidence(
            workspace_path=spec.workspace_path,
            expected_branch=spec.expected_branch or "",
            remote_url=str(tmp_path),
            start_sha="0" * 40,
            base_sha="0" * 40,
            remote_task_sha=None,
            workspace_device=1,
            workspace_inode=1,
            provision_outcome="cloned",
        ),
    )
    monkeypatch.setattr(
        workspace_provisioning,
        "trusted_publish_clone",
        lambda workspace, **kwargs: nullcontext(Path(workspace)),
    )
    monkeypatch.setattr(
        workspace_provisioning,
        "task_workspace_candidate_sha",
        lambda workspace, **kwargs: "0" * 40,
    )
    monkeypatch.setattr(
        workspace_provisioning,
        "checkpoint_task_workspace",
        lambda workspace, **kwargs: kwargs["previous_start_sha"],
    )
    monkeypatch.setattr(
        workspace_provisioning, "task_workspace_is_unchanged", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        workspace_provisioning, "remove_workspace", lambda *a, **kw: "removed"
    )
    # The writer-lease gate is a real external seam: it shells out to the
    # lease tool whenever VOYN_LEASE_DSN names an authority, and this host
    # has one. Unset it here so the default fixture stays hermetic -- the
    # gate's own tests opt back in explicitly.
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)
    return build_handlers()["agent_run"], runs


@pytest.fixture
def lease_tool(monkeypatch, tmp_path):
    """Install a fake ``voyn-lease`` and point the gate at an authority.

    Mirrors tests/orchestrator/test_publish.py: a shell script on disk, so
    the subprocess boundary itself is exercised rather than stubbed out.
    """

    def install(stdout: str = "[]", exit_code: int = 0):
        binary = tmp_path / "fake-voyn-lease"
        binary.write_text(
            f"#!/bin/sh\ncat <<'JSON'\n{stdout}\nJSON\nexit {exit_code}\n"
        )
        binary.chmod(0o755)
        monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
        monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")
        return binary

    return install


def _lease_row(worktree, **overrides) -> str:
    row = {
        "repository_id": "ai-command-center",
        "owner": "claude-worker",
        "host": socket.gethostname(),
        "session_id": "sess-1",
        "worktree": str(worktree),
        "process_pid": 4242,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    row.update(overrides)
    return json.dumps([row])


# -- payload contract ---------------------------------------------------------


def test_unsupported_version_is_non_retryable() -> None:
    error = parse_agent_run(_payload(v=2))
    assert isinstance(error, PayloadError) and not error.retryable
    assert "v1" in error.reason


def test_missing_fields_are_named() -> None:
    error = parse_agent_run(_payload(prompt="", project_id=None))
    assert isinstance(error, PayloadError)
    assert "project_id" in error.reason and "prompt" in error.reason


def test_timeout_beyond_visibility_ceiling_is_refused() -> None:
    error = parse_agent_run(_payload(timeout_seconds=3601))
    assert isinstance(error, PayloadError) and "3600" in error.reason
    assert isinstance(parse_agent_run(_payload(timeout_seconds=True)), PayloadError)


def test_provenance_defaults_to_untrusted() -> None:
    payload = _payload()
    del payload["untrusted"]
    request = parse_agent_run(payload)
    assert request.untrusted is True


# -- outcome discipline -------------------------------------------------------


def test_a_completed_run_reports_ok_with_bounded_result(handler) -> None:
    run_agent, runs = handler
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    assert outcome.result["status"] == "completed"
    assert outcome.result["result_text"] == "done"
    assert runs[0]["task_type"] == "review"


def test_agent_failure_is_still_ok_not_redelivered(handler, monkeypatch) -> None:
    """The agent failing the task is a *result*; redelivering a mutating run
    that already executed would re-apply its side effects."""
    run_agent, _ = handler

    def failed_run(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=1,
            stdout="partial",
            stderr="boom",
            duration_seconds=2.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:02+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", failed_run)
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok and outcome.result["status"] == "failed"


def test_runner_never_started_is_retryable(handler, monkeypatch) -> None:
    run_agent, _ = handler

    def never_started(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=None,
            stdout="",
            stderr="no binary",
            duration_seconds=0.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", never_started)
    outcome = run_agent(_payload(), _event(), 1)
    assert not outcome.ok and outcome.retryable


def test_principal_isolation_failure_is_retryable_not_a_task_result(
    handler, monkeypatch, tmp_path
) -> None:
    run_agent, _ = handler
    removed: list[tuple] = []
    monkeypatch.setattr(
        workspace_provisioning,
        "remove_workspace",
        lambda *args, **kwargs: removed.append(args),
    )

    def launcher_refused(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=125,
            stdout="",
            stderr="AICC_AGENT_LAUNCH_INFRA_FAILURE: launcher socket unavailable",
            duration_seconds=0.1,
            started_at="2026-08-24T00:00:00+00:00",
            completed_at="2026-08-24T00:00:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", launcher_refused)
    outcome = run_agent(
        _payload(
            task_type="implementation",
            repository_path=str(tmp_path / "repo"),
        ),
        _event(),
        1,
    )
    assert not outcome.ok
    assert outcome.retryable
    assert "agent principal isolation" in outcome.reason
    assert not removed, "ambiguous launcher failure must preserve task-local work"


def test_successful_review_may_quote_principal_failure_marker(
    handler, monkeypatch
) -> None:
    """Reviewing launcher code must not make its literal look like transport failure."""
    run_agent, _ = handler

    def accepted_review(**kwargs):
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout=json.dumps(
                {
                    "result": (
                        "The diff contains AICC_AGENT_LAUNCH_INFRA_FAILURE: "
                        "as data.\nVERDICT: ACCEPT\nHEAD_SHA: " + "a" * 40
                    )
                }
            ),
            stderr="model trace quoted AICC_AGENT_LAUNCH_INFRA_FAILURE: data",
            duration_seconds=0.1,
            started_at="2026-08-29T00:00:00+00:00",
            completed_at="2026-08-29T00:00:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", accepted_review)

    outcome = run_agent(_payload(task_type="independent_review"), _event(), 1)

    assert outcome.ok
    assert outcome.result["status"] == "completed"
    assert outcome.result["head_sha"] == "a" * 40


def test_failed_copilot_independent_review_is_retryable_infrastructure(
    handler, monkeypatch
) -> None:
    run_agent, _ = handler

    def failed_copilot(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=1,
            stdout="",
            stderr="Error: No authentication information found",
            duration_seconds=0.1,
            started_at="2026-08-29T00:00:00+00:00",
            completed_at="2026-08-29T00:00:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", failed_copilot)
    outcome = run_agent(
        _payload(
            task_type="independent_review",
            cascade=[
                {
                    "executor": "copilot",
                    "task_type": "independent_review",
                    "capability": "model_only",
                }
            ],
        ),
        _event(),
        1,
    )

    assert not outcome.ok
    assert outcome.retryable
    assert "provider/auth/quota" in outcome.reason


def test_api_error_in_cli_output_is_retryable_not_a_success(
    handler, monkeypatch
) -> None:
    """Incident 2026-08-21 16:09 UTC (control-01/worker-01): a shared
    Claude-CLI account hit its session/rate limit mid-fleet. The process
    still started and exited non-zero *with* stdout, so the pre-existing
    "process never started" check (`exit_code is None and not stdout`) never
    fired and the run fell through to `ok=True` -- an infrastructure failure
    recorded as a genuine task success, so it was never retried. 142 backlog
    tasks cascade-exhausted their retry budget from this exact payload,
    captured verbatim from the live incident DB."""
    run_agent, _ = handler

    rate_limited_stdout = json.dumps(
        {
            "is_error": True,
            "duration_api_ms": 0,
            "num_turns": 1,
            "stop_reason": "stop_sequence",
            "total_cost_usd": 0,
            "usage": {
                "input_tokens": 4,
                "output_tokens": 12,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "terminal_reason": "api_error",
            "fast_mode_disabled_reason": "sdk_opt_in_required",
            "subtype": "success",
            "api_error_status": 429,
            "result": "You've hit your session limit · resets 4:10pm (UTC)",
            "type": "result",
            "duration_ms": 410,
            "uuid": "6f1c9c2e-6b1a-4b0a-9b7e-1f2c3d4e5f60",
        }
    )

    def rate_limited(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=1,
            stdout=rate_limited_stdout,
            stderr="",
            duration_seconds=0.41,
            started_at="2026-08-21T16:09:00+00:00",
            completed_at="2026-08-21T16:09:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", rate_limited)
    outcome = run_agent(_payload(), _event(), 1)
    assert not outcome.ok
    assert outcome.retryable
    assert "executor infrastructure failure" in outcome.reason
    assert "session limit" in outcome.reason


def test_bwrap_loopback_result_is_retryable_infrastructure_failure(
    handler, monkeypatch
):
    run_agent, _runs = handler
    payload = _cascade_payload()
    payload["cascade"][0]["executor"] = "codex"
    payload["cascade"][0]["task_type"] = "implementation"
    monkeypatch.setattr(
        agent_runner, "_codex_workspace_write_preflight_result", (True, "")
    )

    seen = []

    def bwrap_then_fallback(**kwargs):
        seen.append(kwargs["executor"])
        if kwargs["executor"] == "codex":
            return agent_runner.RunResult(
                status="failed",
                exit_code=0,
                stdout="",
                stderr="bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
                duration_seconds=0.01,
                started_at="2026-08-23T12:00:00+00:00",
                completed_at="2026-08-23T12:00:01+00:00",
            )
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "fallback done"}',
            stderr="",
            duration_seconds=0.01,
            started_at="2026-08-23T12:00:02+00:00",
            completed_at="2026-08-23T12:00:03+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", bwrap_then_fallback)
    outcome = run_agent(payload, _event(), 1)
    assert outcome.ok
    assert outcome.result["cascade_step"] == 2
    assert seen == ["codex", payload["cascade"][1]["executor"]]
    assert agent_runner.codex_workspace_write_preflight()[0] is False


def test_codex_workspace_preflight_skips_to_fallback_without_spending_attempt(
    handler, monkeypatch
):
    run_agent, runs = handler
    payload = _cascade_payload()
    payload["cascade"][0]["executor"] = "codex"
    payload["cascade"][0]["task_type"] = "implementation"
    monkeypatch.setattr(
        agent_runner, "codex_workspace_write_preflight", lambda: (False, "bwrap")
    )
    outcome = run_agent(payload, _event(), 1)
    assert outcome.ok
    assert outcome.result["cascade_step"] == 2
    assert runs[0]["executor"] == payload["cascade"][1]["executor"]


def test_copilot_preflight_checks_the_copilot_binary(handler, monkeypatch):
    run_agent, runs = handler
    checked = []

    def preflight(binary=None):
        checked.append(binary)
        return False, "not logged in"

    monkeypatch.setattr(agent_runner, "claude_cli_preflight", preflight)
    payload = _cascade_payload()
    payload["cascade"][0] = {"executor": "copilot", "task_type": "review"}
    outcome = run_agent(payload, _event(), 1)
    assert not outcome.ok and outcome.retryable
    # The unavailable copilot no longer dead-ends the attempt: the cascade
    # falls through to the remaining links (review finding on b311666), so
    # the copilot binary is probed FIRST and the terminal reason belongs to
    # the last exhausted candidate.
    # Pin the WHOLE probe sequence, not just the first link: copilot is tried
    # first, then the one remaining claude candidate (default binary), and the
    # cascade neither stops early nor probes anything extra.
    assert checked == [agent_runner.COPILOT_BINARY, None]
    # The terminal reason must belong to the LAST exhausted candidate (claude),
    # not the copilot link that merely started the cascade.
    assert "claude cli unavailable" in outcome.reason
    assert runs == []


@pytest.mark.parametrize(
    "diagnostic",
    [
        "AI credit usage limit reached",
        "not logged in; authentication required",
        "service unavailable",
        "network error: connection refused",
    ],
)
def test_copilot_provider_failure_retries_into_the_next_cascade_link(
    handler, monkeypatch, diagnostic
):
    run_agent, runs = handler

    def failed_copilot(**kwargs):
        if kwargs["executor"] == "copilot":
            return agent_runner.RunResult(
                status="failed",
                exit_code=4,
                stdout="",
                stderr=diagnostic,
                duration_seconds=0.1,
                started_at="2026-08-23T12:00:00+00:00",
                completed_at="2026-08-23T12:00:01+00:00",
            )
        runs.append(kwargs)
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=0.1,
            started_at="2026-08-23T12:00:01+00:00",
            completed_at="2026-08-23T12:00:02+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", failed_copilot)
    payload = _cascade_payload()
    payload["cascade"] = [
        {"executor": "copilot", "task_type": "review"},
        {"executor": "claude", "task_type": "review"},
    ]
    first = run_agent(payload, _event(), 1)
    assert not first.ok and first.retryable
    assert "executor infrastructure failure" in first.reason
    second = run_agent(payload, _event(), 2)
    assert second.ok and runs[-1]["executor"] == "claude"


def test_unknown_copilot_failure_is_fail_closed_for_read_only_review(
    handler, monkeypatch
):
    run_agent, _runs = handler

    def failed_copilot(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=9,
            stdout="",
            stderr="unexpected provider-side failure",
            duration_seconds=0.1,
            started_at="2026-08-23T12:00:00+00:00",
            completed_at="2026-08-23T12:00:01+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", failed_copilot)
    payload = _cascade_payload()
    payload["cascade"][0] = {"executor": "copilot", "task_type": "review"}
    outcome = run_agent(payload, _event(), 1)
    assert not outcome.ok and outcome.retryable
    assert "unexpected provider-side failure" in outcome.reason


def test_genuine_task_failure_with_error_flavoured_text_still_ok(
    handler, monkeypatch
) -> None:
    """Regression guard for the fix above: a run that genuinely executed and
    the agent's own report happens to mention "error"/"rate limit" in free
    text -- but the CLI's structured payload carries neither `is_error` nor
    `terminal_reason: "api_error"` -- must still be `ok=True`, never
    reclassified as an infrastructure failure. Detection must key off the
    CLI's own structured signal, not a broad heuristic on `result_text`."""
    run_agent, _ = handler

    genuinely_failed_stdout = json.dumps(
        {
            "is_error": False,
            "subtype": "success",
            "type": "result",
            "result": (
                "Ran the test suite: 3 failures remain in "
                "test_rate_limit_handling.py. Could not resolve the "
                "underlying error in the time available."
            ),
            "num_turns": 14,
            "duration_ms": 240000,
            "total_cost_usd": 0.42,
        }
    )

    def genuinely_failed(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=1,
            stdout=genuinely_failed_stdout,
            stderr="pytest exited 1",
            duration_seconds=240.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:04:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", genuinely_failed)
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    assert outcome.result["status"] == "failed"


def test_non_json_stdout_fails_safe_not_misclassified(handler, monkeypatch) -> None:
    """`run.stdout` isn't always pure JSON (e.g. a stream-json/NDJSON shape,
    or plain text). When it can't be positively parsed as the CLI's
    structured result object, the new detection must never trigger as a
    fallback default -- only a confirmed signal is retryable, per
    `agent_runner._parse_cli_result_payload`'s fail-safe contract."""
    run_agent, _ = handler

    def not_json(**kwargs):
        return agent_runner.RunResult(
            status="failed",
            exit_code=1,
            stdout="Error: rate limit api_error 429\nsomething went wrong",
            stderr="",
            duration_seconds=1.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", not_json)
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    assert outcome.result["status"] == "failed"


def test_untrusted_mutating_task_is_refused_not_downgraded(handler) -> None:
    """Audit D7 at the queue boundary: refusal with the reason named, not a
    silent read-only downgrade that half-executes and looks completed."""
    run_agent, runs = handler
    outcome = run_agent(_payload(task_type="implementation", untrusted=True), _event())
    assert not outcome.ok and not outcome.retryable
    assert "operator elevation" in outcome.reason
    assert runs == []  # nothing executed


def test_unknown_repository_is_retryable_elsewhere(handler, monkeypatch) -> None:
    run_agent, _ = handler

    def refuse(project_id, path):
        raise agent_runner.RunnerError("repository not registered on this host")

    monkeypatch.setattr(agent_runner, "validate_repository", refuse)
    outcome = run_agent(_payload(), _event(), 1)
    assert not outcome.ok and outcome.retryable


def test_lease_lost_before_execution_refuses_to_start(handler) -> None:
    run_agent, runs = handler
    lost = _event()
    lost.set()
    outcome = run_agent(_payload(), lost)
    assert not outcome.ok and outcome.retryable and runs == []


def test_run_claude_code_receives_lease_lost_as_cancel_event(handler) -> None:
    """VOYN-W0-AICC-FORCED-AGENT-CANCELLATION: the same `lease_lost` event
    checked once before the subprocess starts must also reach the runner as
    `cancel_event`, so a lease lost mid-run can actually stop it — not just
    be noticed once it exits on its own."""
    run_agent, runs = handler
    event = _event()
    run_agent(_payload(), event, 1)
    assert len(runs) == 1
    assert runs[0]["cancel_event"] is event


def test_lease_lost_mid_run_discards_outcome_and_never_publishes(
    handler, monkeypatch
) -> None:
    """VOYN-W0-AICC-FORCED-AGENT-CANCELLATION requirement 3b: a lease that
    dies WHILE the agent is running (not just before it starts) must not let
    the same attempt's outcome reach `publish_run` (a real `git push` + PR
    create) or be reported as ok. `run_claude_code` itself does not return
    until the process group is confirmed terminated (see
    `agent_runner._terminate_process_group`); this test fakes that return
    with `status="cancelled"` and the event already set — the shape a real
    mid-run cancellation leaves behind — and asserts the handler treats it as
    unaccountable rather than a completed run worth publishing."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    publish_calls: list = []

    def fake_publish(repository, cfg):
        publish_calls.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    event = _event()

    def cancelled_run(**kwargs):
        # Simulate the daemon's heartbeat thread setting `lease_lost` while
        # the subprocess was still in flight -- by the time `run_claude_code`
        # returns, cancellation has already been confirmed.
        event.set()
        return agent_runner.RunResult(
            status="cancelled",
            exit_code=None,
            stdout="",
            stderr="",
            duration_seconds=3.0,
            started_at="2026-08-21T12:00:00+00:00",
            completed_at="2026-08-21T12:00:03+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", cancelled_run)

    outcome = run_agent(_payload(task_type="implementation"), event, 1)

    assert not outcome.ok
    assert outcome.retryable
    assert publish_calls == []


def test_long_output_travels_as_tails(handler, monkeypatch) -> None:
    run_agent, _ = handler

    def chatty(**kwargs):
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout="x" * 50000,
            stderr="y" * 50000,
            duration_seconds=1.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", chatty)
    outcome = run_agent(_payload(), _event(), 1)
    assert len(outcome.result["stdout_tail"]) == 4000
    assert len(outcome.result["stderr_tail"]) == 4000


def test_timed_out_run_is_ok_not_redelivered(handler, monkeypatch) -> None:
    """A timed-out run may have half-executed its mutations; redelivery
    would re-apply them. It is a *result* (status timed_out), never the
    never-started retryable path — pinned because the mutant
    `if run.exit_code is None:` survived review's mutation pass."""
    run_agent, _ = handler

    def timed_out(**kwargs):
        return agent_runner.RunResult(
            status="timed_out",
            exit_code=None,
            stdout="partial work...",
            stderr="",
            duration_seconds=900.0,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:15:00+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", timed_out)
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok and outcome.result["status"] == "timed_out"


def test_every_payload_defect_is_non_retryable() -> None:
    """Per-site pinning: only the version error's retryability was asserted,
    so a site-local `retryable=True` regression would dead-letter-loop bad
    payloads through max_attempts (review finding 2)."""
    defects = [
        _payload(prompt="", project_id=None),  # missing fields
        _payload(timeout_seconds=7200),  # beyond visibility ceiling
        _payload(model=123),  # wrong type
        _payload(untrusted="yes"),  # wrong type
    ]
    for payload in defects:
        error = parse_agent_run(payload)
        assert isinstance(error, PayloadError), payload
        assert error.retryable is False, error.reason


def test_cli_unavailable_is_retryable_and_runs_nothing(handler, monkeypatch) -> None:
    run_agent, runs = handler
    monkeypatch.setattr(
        agent_runner, "claude_cli_preflight", lambda: (False, "binary missing")
    )
    outcome = run_agent(_payload(), _event(), 1)
    assert not outcome.ok and outcome.retryable
    assert "unavailable" in outcome.reason and runs == []


# -- the executor cascade (BO-S2a) -------------------------------------------


def _cascade_payload(**overrides):
    payload = _payload()
    payload["cascade"] = [
        {"executor": "claude", "task_type": "review"},
        {"executor": "claude", "task_type": "review", "model": "stronger-model"},
    ]
    payload.update(overrides)
    return payload


def test_cascade_selects_the_link_for_this_delivery(handler) -> None:
    run_agent, runs = handler
    outcome = run_agent(_cascade_payload(), _event(), 1)
    assert outcome.ok
    assert runs[-1]["task_type"] == "review"
    assert runs[-1]["model"] is None
    assert outcome.result["cascade_step"] == 1


def test_cascade_second_attempt_takes_the_second_link(handler) -> None:
    run_agent, runs = handler
    outcome = run_agent(_cascade_payload(), _event(), 2)
    assert outcome.ok
    assert runs[-1]["model"] == "stronger-model"
    assert outcome.result["cascade_step"] == 2


def test_cascade_clamps_at_the_tail(handler) -> None:
    """Past the last link the tail keeps serving until the attempt budget
    (the cascade's own length, set by the planner) dead-letters the item."""
    run_agent, runs = handler
    outcome = run_agent(_cascade_payload(), _event(), 7)
    assert outcome.ok
    assert runs[-1]["model"] == "stronger-model"


def test_unavailable_executor_is_a_routing_signal_not_a_task_error(handler) -> None:
    """BO-S2a decision: a link naming an executor this host cannot run is a
    RETRYABLE refusal — the attempt returns to the pool and the next delivery
    selects the next link. Nothing may have executed.

    Uses a deliberately never-real name: `codex` used to stand in for
    "unavailable" here, but it became a genuinely wired executor
    (VOYN-W0-AICC-EXECUTOR-CODEX), so reusing it would silently invert what
    this test asserts -- the very kind of stale-example rot that makes a
    green test meaningless."""
    run_agent, runs = handler
    payload = _cascade_payload()
    payload["cascade"][0]["executor"] = "no-such-executor"
    outcome = run_agent(payload, _event(), 1)
    assert not outcome.ok and outcome.retryable is True
    assert "executor_unavailable" in outcome.reason
    assert "no-such-executor" in outcome.reason
    assert runs == [], "an unavailable executor must not execute anything"
    # The same payload on attempt 2 runs the second (available) link.
    assert run_agent(payload, _event(), 2).ok


def test_a_wired_executor_is_dispatched_under_its_own_name(handler) -> None:
    """VOYN-W0-AICC-EXECUTOR-CODEX: an executor present in
    `agent_runner.COMMAND_BUILDERS` must actually RUN (and be recorded as
    itself in the result), not be refused as unavailable. Without this the
    escalation link is inert and every task still funnels into the one
    account whose quota exhaustion this route exists to escape."""
    run_agent, runs = handler
    payload = _cascade_payload()
    payload["cascade"][0]["executor"] = "codex"
    outcome = run_agent(payload, _event(), 1)
    assert outcome.ok, outcome.reason
    assert len(runs) == 1, "the wired executor must actually execute"
    assert runs[0]["executor"] == "codex", (
        "the executor must be passed through to the runner, not silently "
        "defaulted to claude"
    )
    assert outcome.result["executor"] == "codex"


def test_malformed_cascade_is_a_non_retryable_payload_defect(handler) -> None:
    run_agent, runs = handler
    for bad in (
        {"cascade": "claude"},
        {"cascade": [{"model": "x"}]},
        {"cascade": [42]},
    ):
        outcome = run_agent(_payload(**bad), _event(), 1)
        assert not outcome.ok and outcome.retryable is False, bad
    assert runs == []


def test_absent_cascade_keeps_the_single_executor_behaviour(handler) -> None:
    run_agent, runs = handler
    outcome = run_agent(_payload(), _event(), 3)
    assert outcome.ok
    assert outcome.result["cascade_step"] is None
    assert runs[-1]["task_type"] == _payload()["task_type"]


# -- machine outcome extraction (BO-S3) ---------------------------------------


def test_result_carries_pr_url_and_labelled_head_sha(handler, monkeypatch) -> None:
    from command_center import agent_runner

    run_agent, _runs = handler
    monkeypatch.setattr(
        agent_runner,
        "extract_result_text",
        lambda stdout: (
            "Opened https://github.com/o/r/pull/42 for review.\nHEAD_SHA: deadbeefcafe"
        ),
    )
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    assert outcome.result["pr_url"] == "https://github.com/o/r/pull/42"
    assert outcome.result["head_sha"] == "deadbeefcafe"


def test_publish_branches_on_the_backlog_task_id_not_the_project_id(
    handler, monkeypatch
) -> None:
    """VOYN-W0-AICC-PUBLISH-BRANCH-COLLISION: publish_run branches on
    ``cfg.task`` (``backlog/<task>``, publish.py), so passing the shared
    project_id there put every task for one project on the SAME branch —
    a later force-push silently erased an earlier, still-open task's work.
    The payload's own backlog_task_id must be what reaches PublishConfig."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    captured: list = []

    def fake_publish(repository, cfg):
        captured.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    run_agent(
        _payload(task_type="implementation", backlog_task_id="VOYN-W0-REAL-TASK"),
        _event(),
        1,
    )
    assert captured[0].task == "VOYN-W0-REAL-TASK"


def test_publish_falls_back_to_project_id_without_a_backlog_task_id(
    handler, monkeypatch
) -> None:
    """A payload enqueued before this field existed still parses and still
    publishes -- to the old shared-branch name, not a crash."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    captured: list = []

    def fake_publish(repository, cfg):
        captured.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    run_agent(_payload(task_type="implementation"), _event(), 1)
    assert captured[0].task == "proj"  # _payload()'s project_id


def test_a_bare_hex_string_is_not_a_head_sha(handler, monkeypatch) -> None:
    """Only the labelled trailer counts: a transcript is full of object ids,
    and guessing which one is the head is the substring-matching the rules
    forbid. No trailer, no sha — the DONE gate simply holds."""
    from command_center import agent_runner

    run_agent, _runs = handler
    monkeypatch.setattr(
        agent_runner,
        "extract_result_text",
        lambda stdout: (
            "commit 0123456789abcdef0123456789abcdef01234567 pushed, no trailer"
        ),
    )
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    assert outcome.result["head_sha"] is None
    assert outcome.result["pr_url"] is None


# -- single-writer dispatch gate (VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE)


def test_mutating_dispatch_into_a_leased_worktree_is_refused(
    handler, lease_tool, tmp_path
) -> None:
    """The defect this task names: the worker dispatched into a checkout
    another writer holds, and the collision only surfaced at commit time --
    by which point the foreign edits were already in the tree."""
    run_agent, runs = handler
    worktree = isolated_path(tmp_path)
    lease_tool(_lease_row(worktree))
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert not outcome.ok and outcome.retryable
    assert "claude-worker" in outcome.reason and str(worktree) in outcome.reason
    assert runs == [], "nothing may execute in another writer's checkout"


def test_a_lease_one_level_up_still_covers_the_dispatch(
    handler, lease_tool, tmp_path
) -> None:
    """Routing at a subdirectory of a leased checkout is the same collision,
    so containment -- not path equality -- is what the gate tests."""
    run_agent, runs = handler
    lease_tool(_lease_row(tmp_path.parent))
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert not outcome.ok and outcome.retryable and runs == []


def test_a_read_only_dispatch_is_not_gated(handler, lease_tool, tmp_path) -> None:
    """Scope, stated rather than assumed: a non-mutating run gets the
    read-only sandbox profile and cannot write into the tree at all, so a
    lease it could not violate must not block it."""
    run_agent, runs = handler
    lease_tool(_lease_row(tmp_path))
    outcome = run_agent(_payload(task_type="review"), _event())
    assert outcome.ok and len(runs) == 1


def test_expired_other_host_and_other_path_leases_do_not_block(
    handler, lease_tool, tmp_path
) -> None:
    """Three ways a row can name this path without holding it."""
    run_agent, runs = handler
    stale = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    worktree = isolated_path(tmp_path)
    for row in (
        _lease_row(worktree, expires_at=stale),
        _lease_row(worktree, host="some-other-worker"),
        _lease_row(tmp_path.parent / "a-different-checkout"),
    ):
        runs.clear()
        lease_tool(row)
        outcome = run_agent(_payload(task_type="implementation"), _event())
        assert outcome.ok, f"{row} must not block: {outcome.reason}"
        assert len(runs) == 1


def test_an_unreachable_authority_blocks_rather_than_opens(
    handler, lease_tool, monkeypatch, tmp_path
) -> None:
    """A guard that opens when it cannot see is not a guard. Every failure
    to ask a configured authority -- non-zero exit, unreadable output,
    missing binary -- refuses the dispatch."""
    run_agent, runs = handler

    lease_tool("boom: connection refused", exit_code=1)
    refused = run_agent(_payload(task_type="implementation"), _event())
    assert not refused.ok and refused.retryable and runs == []
    assert "connection refused" in refused.reason

    lease_tool("not json at all")
    unreadable = run_agent(_payload(task_type="implementation"), _event())
    assert not unreadable.ok and unreadable.retryable and runs == []
    assert "unreadable" in unreadable.reason

    lease_tool("[]")
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(tmp_path / "does-not-exist"))
    missing = run_agent(_payload(task_type="implementation"), _event())
    assert not missing.ok and missing.retryable and runs == []
    assert "unreachable" in missing.reason


def test_no_configured_authority_leaves_the_gate_inert(handler, tmp_path) -> None:
    """A host with no lease authority has no lease to violate: VOYN_LEASE_DSN
    is the tool's only DSN source, so without it ``list`` cannot answer and
    blocking every mutating run would strand hosts that never had a lease."""
    run_agent, runs = handler
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert outcome.ok and len(runs) == 1


def _own_start(pid: int) -> str:
    """The identity token the lease authority stores for a pid, read the same
    way the gate reads it -- past the last ``)``, because ``comm`` is
    parenthesised and may contain spaces."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rindex(")") + 2 :].split()[19]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="reads /proc/<pid>/stat directly, the same seamless-fallback shape "
    "worktree_lease._process_start itself uses in production (fails soft to "
    "None off Linux, never crashes there) -- but this test's own fixture "
    "helper, _own_start, has no such fallback, so it must skip rather than "
    "fail where /proc does not exist. Linux CI (the real deployment target) "
    "keeps full coverage; see VOYN-W0-AICC-LEASE-TEST-PROC-MACOS-SKIP.",
)
def test_a_lease_held_by_our_own_supervisor_does_not_block(
    handler, lease_tool, tmp_path
) -> None:
    """The process that launched us owning the tree is the normal shape.

    A supervisor holds the lease and dispatches this worker into the tree it
    owns; refusing there would deadlock the loop the supervisor runs. Our own
    pid stands in for the ancestor -- self is the first entry of the ancestry
    walk, so it exercises the same match.
    """
    run_agent, runs = handler
    pid = os.getpid()
    lease_tool(
        _lease_row(
            isolated_path(tmp_path), process_pid=pid, process_start=_own_start(pid)
        )
    )
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert outcome.ok, outcome.reason
    assert len(runs) == 1, "the dispatch was refused by its own supervisor's lease"


def test_a_recycled_pid_matching_an_ancestor_still_blocks(
    handler, lease_tool, tmp_path
) -> None:
    """Identity is (pid, process_start), not pid alone.

    Same pid as ours, different start token: a foreign writer whose pid the
    kernel happened to reuse. Matching on the number alone would open the gate
    for it.
    """
    run_agent, runs = handler
    lease_tool(
        _lease_row(
            isolated_path(tmp_path),
            process_pid=os.getpid(),
            process_start="not-our-start",
        )
    )
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert not outcome.ok and outcome.retryable
    assert runs == []


def test_a_lease_with_no_identity_token_still_blocks(
    handler, lease_tool, tmp_path
) -> None:
    """An unverifiable match is not a match: the row names our pid, but with
    no ``process_start`` on either side to confirm it, the gate fails closed."""
    run_agent, _runs = handler
    row = json.loads(_lease_row(isolated_path(tmp_path), process_pid=os.getpid()))
    row[0].pop("process_start", None)
    lease_tool(json.dumps(row))
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert not outcome.ok and outcome.retryable


# -- full-lifecycle writer lease (VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE) ----


def test_mutating_dispatch_holds_the_writer_lease_before_provisioning(
    handler, monkeypatch, tmp_path
) -> None:
    """The writer lease must be acquired (and the pre-push hook's identity
    provisioned) BEFORE the workspace is provisioned and the agent runs --
    not only around `publish_run`'s own push -- and released only once the
    whole dispatch is done. `blocking_lease`'s own `list` preflight must run
    first (it is the read-only check for a lease held by someone ELSE);
    `acquire`/`install-hooks` (this task's lease, held by us) follow it."""
    run_agent, runs = handler
    calls = tmp_path / "lease-calls.log"
    binary = tmp_path / "fake-voyn-lease"
    # `blocking_lease` calls plain `[tool, "list"]` (no `--repo`/identity
    # flags -- it is a read-only preflight over every lease, not one this
    # process's own identity); the writer lease's `acquire`/`install-hooks`/
    # `release` go through `lease_client.lease_argv`'s
    # `--repo <path> <verb> --repository ... --owner ...` shape instead. One
    # script answers both: it always emits `[]` (the only thing `list`
    # reads) and exits 0.
    binary.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\necho '[]'\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")

    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert outcome.ok, outcome.reason
    assert len(runs) == 1

    lines = calls.read_text().splitlines()
    assert lines and lines[0] == "list", "blocking_lease's preflight runs first"

    def _first(verb: str) -> int:
        # `--repo <path> <verb> ...` -- the verb is the third token.
        return next(i for i, line in enumerate(lines) if line.split()[2:3] == [verb])

    def _last(verb: str) -> int:
        return max(i for i, line in enumerate(lines) if line.split()[2:3] == [verb])

    assert _first("acquire") >= 1
    assert _first("release") >= 1
    assert 0 < _first("acquire") < _first("release"), (
        "the writer lease is acquired before provisioning/running the agent "
        "and released only once the dispatch is fully done"
    )
    # VOYN-W0-AICC-LEASE-SCOPE-PER-TASK: the hook identity file lives in the
    # clone's COMMON git dir, so a task-scoped lease must not write it --
    # only `publish_run` does, immediately before its push. See
    # `test_hold_never_touches_the_clone_wide_hook_identity_file`.
    assert not any(line.split()[2:3] == ["install-hooks"] for line in lines), (
        "the full-lifecycle lease must not provision the clone-wide hook identity file"
    )


def test_the_full_lifecycle_lease_is_scoped_to_the_task_not_the_repository(
    handler, monkeypatch, tmp_path
) -> None:
    """VOYN-W0-AICC-LEASE-SCOPE-PER-TASK. Measured 2026-08-23: 96 of 115
    returns-to-pool in three hours were `VOYN_LEASE_REFUSED active` -- tasks
    refusing each other because the lease keyed on the repository, even
    though #346 gave each task its own worktree and they share no mutable
    state during the agent run. The scope must carry the task id."""
    run_agent, _runs = handler
    calls = tmp_path / "lease-calls.log"
    binary = tmp_path / "fake-voyn-lease"
    binary.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\necho '[]'\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")

    outcome = run_agent(
        _payload(task_type="implementation", backlog_task_id="VOYN-W0-SCOPED"),
        _event(),
    )
    assert outcome.ok, outcome.reason

    scopes = {
        line.split("--repository ", 1)[1].split()[0]
        for line in calls.read_text().splitlines()
        if "--repository " in line
    }
    assert scopes, "no identity-bearing lease call was made"
    assert all("VOYN-W0-SCOPED" in scope for scope in scopes), (
        f"the lease scope must name the task, got {scopes}"
    )


def test_publish_does_not_release_a_lease_the_caller_still_holds(
    handler, monkeypatch, tmp_path
) -> None:
    """Independent-review finding on VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE:
    the first revision let `publish_run` release the writer lease
    unconditionally in its own `finally`, dropping it mid-dispatch -- before
    this function's own post-publish work and `remove_workspace`'s worktree
    cleanup, both still inside the caller's `stack`. When the full-lifecycle
    lease was actually acquired (`VOYN_LEASE_DSN` configured), `publish_run`
    must be called with `release_lease=False` so the one real release stays
    with the caller's own `stack.close()`."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    binary = tmp_path / "fake-voyn-lease"
    binary.write_text('#!/bin/sh\nif [ "$1" = "list" ]; then echo \'[]\'; fi\nexit 0\n')
    binary.chmod(0o755)
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")

    captured: list = []

    def fake_publish(repository, cfg):
        captured.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    # No `backlog_task_id`, so `_task_lease_scope` falls back to the bare
    # project and the two leases ARE the same row -- the case where the
    # caller's own release is the only correct one.
    outcome = run_agent(_payload(task_type="implementation"), _event(), 1)
    assert outcome.ok, outcome.reason
    assert captured and captured[0].release_lease is False


def test_publish_releases_its_own_lease_when_the_scopes_differ(
    handler, monkeypatch, tmp_path
) -> None:
    """VOYN-W0-AICC-LEASE-SCOPE-PER-TASK, caught in independent review before
    merge: once the full-lifecycle lease became task-scoped
    (`<project>:<task>`) while `publish_run` stayed repository-scoped
    (`<project>`), they are two DIFFERENT lease rows. `release_lease=False`
    -- correct while both keys were the repository -- would then leave
    `<project>` held by nobody's `finally`: `writer_lease._release` only ever
    releases its own `<project>:<task>`. The row would leak on the first
    mutating task and refuse every later push with `VOYN_LEASE_REFUSED
    active` for the worker process's whole lifetime, un-reapable because
    `--auto-takeover` and `ops/lease_reap.sh` both require a DEAD recorded
    holder and this one is alive.

    So the flag must follow whether the two scopes name the same row, not
    whether an outer lease merely exists."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    binary = tmp_path / "fake-voyn-lease"
    binary.write_text('#!/bin/sh\nif [ "$1" = "list" ]; then echo \'[]\'; fi\nexit 0\n')
    binary.chmod(0o755)
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")

    captured: list = []

    def fake_publish(repository, cfg):
        captured.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    outcome = run_agent(
        _payload(task_type="implementation", backlog_task_id="VOYN-W0-SCOPED"),
        _event(),
        1,
    )
    assert outcome.ok, outcome.reason
    assert captured, "publish_run was never called"
    assert captured[0].release_lease is True, (
        "publish holds a different lease row than the caller, so it must "
        "release its own or the repository-scoped row leaks"
    )
    # And the two really are different rows -- otherwise this test would pass
    # for the wrong reason.
    assert captured[0].repository != handlers_module._task_lease_scope(
        parse_agent_run(
            _payload(task_type="implementation", backlog_task_id="VOYN-W0-SCOPED")
        )
    )


def test_publish_releases_its_own_lease_when_no_full_lifecycle_lease_is_held(
    handler, monkeypatch
) -> None:
    """Symmetric case: no `VOYN_LEASE_DSN` means no full-lifecycle lease was
    ever acquired (the `handler` fixture already unsets it), so
    `publish_run` must keep its own default `release_lease=True` -- nothing
    else will ever release that lease if it doesn't."""
    import command_center.worker.handlers as handlers_module

    run_agent, _runs = handler
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")

    captured: list = []

    def fake_publish(repository, cfg):
        captured.append(cfg)
        return PublishResult(ok=True, branch=f"backlog/{cfg.task}")

    monkeypatch.setattr(handlers_module, "publish_run", fake_publish)

    outcome = run_agent(_payload(task_type="implementation"), _event(), 1)
    assert outcome.ok, outcome.reason
    assert captured and captured[0].release_lease is True


def test_writer_lease_unavailable_blocks_dispatch_before_the_agent_runs(
    handler, monkeypatch, tmp_path
) -> None:
    """Another writer already holding the repository-level lease (or the
    authority refusing/unreachable for the acquire specifically, as opposed
    to `list`) must refuse the dispatch retryably before any workspace is
    touched or the agent is launched -- the same fail-closed shape
    `blocking_lease` already has for its own preflight."""
    run_agent, runs = handler
    binary = tmp_path / "fake-voyn-lease"
    binary.write_text(
        "#!/bin/sh\n"
        # `blocking_lease` calls plain `[tool, "list"]` ($1=list, no
        # `--repo`); the writer lease's calls go through `--repo <path>
        # <verb> ...` ($3=verb). Distinguish on whichever positional
        # argument actually carries the verb for that call shape.
        'if [ "$1" = "list" ]; then echo "[]"; exit 0; fi\n'
        'case "$3" in\n'
        "  acquire) echo 'lease already held by another writer' >&2; exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(binary))
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgresql://authority/present")

    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert not outcome.ok
    assert outcome.retryable
    assert "writer lease unavailable" in outcome.reason
    assert runs == [], "the agent must not run without the writer lease held"


def test_no_configured_authority_leaves_the_writer_lease_inert_too(
    handler, tmp_path
) -> None:
    """Symmetric with `test_no_configured_authority_leaves_the_gate_inert`
    for `blocking_lease`: a host with no `VOYN_LEASE_DSN` has no lease
    authority to acquire a lease from either, so the full-lifecycle lease
    must not block it (it already inherits the ambient absence of
    `VOYN_LEASE_TOOL`/`VOYN_LEASE_DSN` from the `handler` fixture)."""
    run_agent, runs = handler
    outcome = run_agent(_payload(task_type="implementation"), _event())
    assert outcome.ok, outcome.reason
    assert len(runs) == 1


# -- VOYN-W0-AICC-REVIEW-AUTO-ACCEPT: head-pinned verification checkout ------


def test_review_head_payload_fields_parse_and_validate() -> None:
    request = parse_agent_run(
        _payload(review_head={"pr_number": "42", "head_sha": "a" * 40})
    )
    assert request.review_head_pr_number == "42"
    assert request.review_head_sha == "a" * 40

    absent = parse_agent_run(_payload())
    assert absent.review_head_sha is None and absent.review_head_pr_number is None

    for bad in (
        "not-a-dict",
        {"pr_number": "42"},
        {"pr_number": "42", "head_sha": "short"},
        {"pr_number": "x", "head_sha": "a" * 40},
        {"pr_number": 42, "head_sha": "a" * 40},
    ):
        error = parse_agent_run(_payload(review_head=bad))
        assert isinstance(error, PayloadError) and not error.retryable, bad


def test_review_head_runs_in_the_pinned_checkout_and_cleans_up(
    handler, monkeypatch, tmp_path
) -> None:
    """The verification agent must execute in the detached checkout at the
    PR's exact head -- not the shared repository -- and the checkout must be
    removed on every exit path (the ExitStack owns the removal)."""
    from command_center.worker import handlers as handlers_module

    run_agent, runs = handler
    pin = tmp_path / "verify-42-pin"
    removed: list[tuple] = []
    monkeypatch.setattr(
        handlers_module,
        "_review_head_checkout",
        lambda repository, pr_number, head_sha: (pin, None),
    )
    monkeypatch.setattr(
        handlers_module,
        "_remove_review_head_checkout",
        lambda repository, target: removed.append((repository, target)),
    )
    outcome = run_agent(
        _payload(
            task_type="verification_review",
            untrusted=True,
            review_head={"pr_number": "42", "head_sha": "b" * 40},
        ),
        _event(),
    )
    assert outcome.ok, outcome.reason
    assert runs[0]["repository_path"] == pin
    assert removed == [(tmp_path, pin)]


def test_review_head_checkout_is_removed_on_failure_paths_too(
    handler, monkeypatch, tmp_path
) -> None:
    """Cleanup is owned by the handler's ExitStack, so it must run when the
    agent run FAILS and when the runner RAISES -- not only after a success
    (independent review of this change, chunk 1 at 32bf893: a success-only
    test would pass an implementation that leaks the worktree on failure)."""
    from command_center.worker import handlers as handlers_module

    run_agent, _runs = handler
    pin = tmp_path / "verify-42-pin"
    removed: list[tuple] = []
    monkeypatch.setattr(
        handlers_module,
        "_review_head_checkout",
        lambda repository, pr_number, head_sha: (pin, None),
    )
    monkeypatch.setattr(
        handlers_module,
        "_remove_review_head_checkout",
        lambda repository, target: removed.append((repository, target)),
    )
    payload = _payload(
        task_type="verification_review",
        untrusted=True,
        review_head={"pr_number": "42", "head_sha": "b" * 40},
    )

    def failed_run(**kwargs):
        return agent_runner.RunResult(
            status="failed", exit_code=1, stdout="", stderr="agent died",
            duration_seconds=0.1,
            started_at="2026-08-26T12:00:00+00:00",
            completed_at="2026-08-26T12:00:01+00:00",
        )

    monkeypatch.setattr(agent_runner, "run_claude_code", failed_run)
    run_agent(payload, _event())
    assert removed == [(tmp_path, pin)]

    def raising_run(**kwargs):
        raise RuntimeError("runner blew up")

    removed.clear()
    monkeypatch.setattr(agent_runner, "run_claude_code", raising_run)
    with pytest.raises(RuntimeError):
        run_agent(payload, _event())
    assert removed == [(tmp_path, pin)]


def test_review_head_pin_is_refused_for_a_mutating_task(handler) -> None:
    """A mutating run pinned to a detached head has no branch to publish --
    the combination is a payload defect, never silently ignored."""
    run_agent, runs = handler
    outcome = run_agent(
        _payload(
            task_type="implementation",
            untrusted=False,
            review_head={"pr_number": "42", "head_sha": "b" * 40},
        ),
        _event(),
    )
    assert not outcome.ok and not outcome.retryable
    assert "review_head" in outcome.reason
    assert runs == []


def test_review_head_checkout_failure_is_retryable(handler, monkeypatch) -> None:
    from command_center.worker import handlers as handlers_module

    run_agent, runs = handler
    monkeypatch.setattr(
        handlers_module,
        "_review_head_checkout",
        lambda repository, pr_number, head_sha: (None, "fetch failed"),
    )
    outcome = run_agent(
        _payload(
            task_type="verification_review",
            untrusted=True,
            review_head={"pr_number": "42", "head_sha": "b" * 40},
        ),
        _event(),
    )
    assert not outcome.ok and outcome.retryable
    assert "fetch failed" in outcome.reason
    assert runs == []


def test_review_head_checkout_builds_a_detached_worktree_at_the_exact_sha(
    tmp_path,
) -> None:
    """Real git end to end: fetch refs/pull/<n>/head from origin, add the
    detached worktree at the exact sha, verify, and remove -- the seam the
    faked tests above stand on."""
    import subprocess

    from command_center.worker.handlers import (
        _remove_review_head_checkout,
        _review_head_checkout,
    )

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    git(tmp_path, "init", "-q", str(origin))
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "--allow-empty", "-q", "-m", "base")
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "--allow-empty", "-q", "-m", "pr head")
    head_sha = git(origin, "rev-parse", "HEAD")
    git(origin, "update-ref", "refs/pull/7/head", head_sha)
    git(origin, "reset", "-q", "--hard", "HEAD~1")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    assert git(clone, "rev-parse", "HEAD") != head_sha

    checkout, failure = _review_head_checkout(clone, "7", head_sha)
    assert failure is None
    assert checkout is not None and checkout.is_dir()
    assert git(checkout, "rev-parse", "HEAD") == head_sha

    _remove_review_head_checkout(clone, checkout)
    assert not checkout.exists()

    missing, failure = _review_head_checkout(clone, "7", "f" * 40)
    assert missing is None and "unreachable" in failure
