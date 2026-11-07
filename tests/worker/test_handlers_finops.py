from __future__ import annotations

import threading

import pytest

from command_center import agent_runner
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
        "cascade": [
            {"executor": "claude", "task_type": "review"},
            {"executor": "codex", "task_type": "review"},
        ],
    }
    payload.update(overrides)
    return payload


def _event() -> threading.Event:
    return threading.Event()


@pytest.fixture
def run_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_runner, "validate_repository", lambda project_id, path: tmp_path
    )
    monkeypatch.setattr(
        agent_runner, "claude_cli_preflight", lambda binary=None: (True, "ok")
    )
    monkeypatch.setattr(
        agent_runner,
        "run_claude_code",
        lambda **kwargs: agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=0.1,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        ),
    )
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)
    return build_handlers()["agent_run"]


def test_owner_cascade_does_not_reintroduce_metered_links_on_redrive(
    run_agent, monkeypatch
):
    calls: list[str] = []

    def fake_run(**kwargs):
        calls.append(kwargs["executor"])
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=0.1,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-cascade")
    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run)
    monkeypatch.setattr(
        agent_runner, "codex_workspace_write_preflight", lambda: (True, "ok")
    )

    payload = _payload(finops_excluded_executors=["cursor", "grok", "openai_http"])
    result3 = run_agent(payload, _event(), 3)
    result4 = run_agent(payload, _event(), 4)

    assert result3.ok and result4.ok
    assert len(calls) == 2
    assert calls[0] in {"claude", "codex"}
    assert calls[1] in {"claude", "codex"}
    assert "cursor" not in calls
    assert "grok" not in calls
    assert "openai_http" not in calls


def test_owner_alternatives_with_finops_exclusions_waits_without_launch(run_agent, monkeypatch):
    runs: list[str] = []

    def fake_run(**kwargs):
        runs.append(kwargs["executor"])
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=0.1,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run)

    outcome = run_agent(
        _payload(
            cascade=[{"executor": "claude", "task_type": "review"}],
            finops_excluded_executors=["cursor", "grok"],
        ),
        _event(),
        1,
    )

    assert not outcome.ok
    assert outcome.infra_wait
    assert outcome.reason == "finops: all profile links excluded (cost_data_unavailable)"
    assert runs == []


def test_owner_override_without_finops_marker_keeps_legacy_behavior(run_agent, monkeypatch):
    """Legacy payload path stays as in main; see also the regression test in
    tests/worker/test_handlers.py::test_owner_alternatives_preserve_incoming_cascade_links.
    """
    runs: list[dict[str, str | None]] = []

    def fake_run(**kwargs):
        runs.append({"executor": kwargs["executor"], "model": kwargs["model"]})
        return agent_runner.RunResult(
            status="completed",
            exit_code=0,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=0.1,
            started_at="2026-08-19T12:00:00+00:00",
            completed_at="2026-08-19T12:00:01+00:00",
        )

    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run)

    outcome = run_agent(
        _payload(cascade=[{"executor": "claude", "task_type": "review"}]),
        _event(),
        1,
    )

    assert outcome.ok
    assert runs == [{"executor": "cursor", "model": "auto"}]


@pytest.mark.parametrize(
    "value",
    ["cursor", {"executor": "cursor"}, 17],
)
def test_parse_finops_excluded_executors_requires_a_list(value):
    error = parse_agent_run(_payload(finops_excluded_executors=value))
    assert isinstance(error, PayloadError)
    assert "finops_excluded_executors must be a list" in error.reason


def test_parse_finops_excluded_executors_requires_strings():
    error = parse_agent_run(_payload(finops_excluded_executors=["cursor", 7]))
    assert isinstance(error, PayloadError)
    assert "finops_excluded_executors[1] must be a string" in error.reason
