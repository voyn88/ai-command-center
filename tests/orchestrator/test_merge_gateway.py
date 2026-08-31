"""merge_gateway.merge_pr: the sole `gh pr merge` call site, fail-closed on
every axis it checks. `gh` is faked in-process by patching the module's
`_gh`, mirroring tests/db/test_review_merge.py's pattern for the pre-gateway
`_pr_is_mergeable`/`merge_once`."""

from __future__ import annotations

import json
import subprocess

import pytest

from command_center.orchestrator import merge_gateway
from command_center.orchestrator.merge_gateway import GatewayConfig, merge_pr

PR_URL = "https://github.com/x/y/pull/42"
HEAD = "a" * 40
AUTHOR = "server-worker"
REVIEWER = "voyn-acceptance[bot]"


def _view_body(**overrides) -> str:
    body = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "author": {"login": AUTHOR},
        "reviews": [{"body": f"ACCEPTANCE: ACCEPT {HEAD}", "user": {"login": REVIEWER}, "state": "COMMENTED"}],
        "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
    }
    body.update(overrides)
    return json.dumps(body)


def _fake_gh(view_body: str, view_rc: int = 0, merge_rc: int = 0):
    calls = []

    def fake(argv, repo_path, token):
        calls.append((argv, repo_path, token))
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, view_rc, view_body, "" if view_rc == 0 else "boom")
        if argv[:1] == ["api"]:
            return subprocess.CompletedProcess(argv, merge_rc, "{}", "" if merge_rc == 0 else "merge refused")
        raise AssertionError(f"unexpected gh call: {argv}")

    return fake, calls


@pytest.fixture(autouse=True)
def _gateway_token(monkeypatch):
    monkeypatch.setenv(merge_gateway.GATEWAY_TOKEN_ENV, "gateway-secret-token")


def test_merges_when_every_check_passes(monkeypatch):
    fake, calls = _fake_gh(_view_body())
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is True
    assert result.head_sha == HEAD
    assert result.reviewer == REVIEWER
    view_call, merge_call = calls
    assert view_call[2] == "gateway-secret-token"
    assert merge_call[0] == [
        "api", "-X", "PUT", "repos/x/y/pulls/42/merge",
        "-f", "merge_method=squash", "-f", f"sha={HEAD}",
    ]
    assert merge_call[2] == "gateway-secret-token"


def test_refuses_without_a_gateway_credential(monkeypatch):
    monkeypatch.delenv(merge_gateway.GATEWAY_TOKEN_ENV, raising=False)
    fake, calls = _fake_gh(_view_body())
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "gateway_credential_missing" in result.reason
    assert calls == []  # never touches the network without its own credential


def test_refuses_on_policy_version_mismatch(monkeypatch):
    fake, calls = _fake_gh(_view_body())
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp", policy_version="stale"))

    assert result.ok is False
    assert "policy_version_mismatch" in result.reason
    assert calls == []


def test_refuses_an_unresolvable_pr_url(monkeypatch):
    fake, calls = _fake_gh(_view_body())
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr("not-a-pr-url", GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "unresolvable_pr_url" in result.reason
    assert calls == []


def test_refuses_when_pr_is_not_open(monkeypatch):
    fake, _ = _fake_gh(_view_body(state="CLOSED"))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "pr_not_open" in result.reason


def test_refuses_when_head_sha_is_unreadable(monkeypatch):
    fake, _ = _fake_gh(_view_body(headRefOid="not-a-sha"))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "head_sha_unreadable" in result.reason


def test_refuses_a_self_issued_verdict(monkeypatch):
    fake, _ = _fake_gh(_view_body(
        reviews=[{"body": f"ACCEPTANCE: ACCEPT {HEAD}", "user": {"login": AUTHOR}, "state": "COMMENTED"}],
    ))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "acceptance_refused" in result.reason
    assert "author" in result.reason


def test_refuses_an_active_reject(monkeypatch):
    fake, _ = _fake_gh(_view_body(
        reviews=[{"body": f"ACCEPTANCE: REJECT {HEAD}", "user": {"login": REVIEWER}, "state": "COMMENTED"}],
    ))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "acceptance_refused" in result.reason
    assert "REJECTED" in result.reason


def test_refuses_with_no_verdict_at_all(monkeypatch):
    fake, _ = _fake_gh(_view_body(reviews=[]))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "acceptance_refused" in result.reason


def test_refuses_when_a_required_check_is_not_terminal_success(monkeypatch):
    fake, _ = _fake_gh(_view_body(
        statusCheckRollup=[{"name": "CI", "conclusion": "FAILURE"}],
    ))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "checks_not_terminal_success" in result.reason


def test_refuses_when_a_check_is_still_pending(monkeypatch):
    """A `None` conclusion (still running) is not a pass — the pre-gateway
    `_pr_is_mergeable` treated it as one, which is exactly the kind of
    logic-level gap a credential-isolated gateway should not repeat."""
    fake, _ = _fake_gh(_view_body(
        statusCheckRollup=[{"name": "CI", "conclusion": None}],
    ))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "checks_not_terminal_success" in result.reason


def test_refuses_when_no_checks_are_reported_at_all(monkeypatch):
    fake, _ = _fake_gh(_view_body(statusCheckRollup=[]))
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "no_status_checks_reported" in result.reason


def test_refuses_when_the_view_call_fails(monkeypatch):
    fake, _ = _fake_gh(_view_body(), view_rc=1)
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "gh_view_failed" in result.reason


def test_refuses_when_the_view_response_is_not_json(monkeypatch):
    fake, _ = _fake_gh("not json")
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "gh_view_unparseable" in result.reason


def test_refuses_when_the_merge_call_itself_fails(monkeypatch):
    """`gh api ... sha=<head>` returning non-zero (e.g. a 409 because the head
    moved between view and merge) is a refusal, not a swallowed error."""
    fake, _ = _fake_gh(_view_body(), merge_rc=1)
    monkeypatch.setattr(merge_gateway, "_gh", fake)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "gh_merge_failed" in result.reason


def test_refuses_when_gh_raises(monkeypatch):
    def raising(argv, repo_path, token):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(merge_gateway, "_gh", raising)

    result = merge_pr(PR_URL, GatewayConfig(repo_path="/tmp"))

    assert result.ok is False
    assert "gh_view_error" in result.reason


def test_gh_wrapper_uses_the_gateway_token_never_an_ambient_one(monkeypatch, tmp_path):
    """Exercises the real `_gh`, not the fake: an ambient GH_TOKEN/GITHUB_TOKEN
    already sitting in the process environment (as a worker process's would
    be, if one were ever misconfigured to carry one) must never reach the
    subprocess — only the gateway's own token may."""
    monkeypatch.setenv("GH_TOKEN", "ambient-should-never-be-used")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-should-never-be-used-either")

    captured = {}

    def fake_run(argv, cwd, capture_output, text, check, timeout, env):
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    merge_gateway._gh(["pr", "view", PR_URL], str(tmp_path), "the-real-gateway-token")

    assert captured["env"]["GH_TOKEN"] == "the-real-gateway-token"
    assert "GITHUB_TOKEN" not in captured["env"]
