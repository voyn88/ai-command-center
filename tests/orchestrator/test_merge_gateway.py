"""merge_gateway's fail-closed paths that need no database: a missing
credential or an unresolvable identity must refuse before ever looking at a
task, and never merges anything either commits or rolls back — they can be
distinguished from an ordinary "not ready yet" skip by inspecting
``LoopReport.errors`` rather than ``.skipped``, and neither path may touch
the database at all (proven here with a factory that raises if called)."""

from __future__ import annotations

import subprocess

import pytest

from command_center.orchestrator import merge_gateway
from command_center.orchestrator.merge_gateway import (
    GatewayConfig,
    _checks_terminal_success,
    merge_once,
)


def _factory_must_not_be_called():
    def _factory():
        raise AssertionError("merge_once touched the database before resolving its credential")
    return _factory


def test_missing_token_is_an_error_not_a_skip(monkeypatch):
    monkeypatch.delenv("AICC_MERGE_GATEWAY_TOKEN", raising=False)

    def fake_gh(argv, repo, token):
        raise AssertionError("gh must never be invoked with no credential configured")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(_factory_must_not_be_called(), "/tmp")
    assert report.merged == [] and report.skipped == []
    assert len(report.errors) == 1
    assert "AICC_MERGE_GATEWAY_TOKEN" in report.errors[0][1]


def test_unresolvable_identity_is_an_error_not_a_skip(monkeypatch):
    monkeypatch.setenv("AICC_MERGE_GATEWAY_TOKEN", "gateway-token")

    def fake_gh(argv, repo, token):
        assert token == "gateway-token"
        return subprocess.CompletedProcess(argv, 1, "", "bad credentials")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(_factory_must_not_be_called(), "/tmp")
    assert report.merged == [] and report.skipped == []
    assert len(report.errors) == 1
    assert "cannot resolve gateway identity" in report.errors[0][1]


def test_empty_identity_is_an_error_not_a_skip(monkeypatch):
    monkeypatch.setenv("AICC_MERGE_GATEWAY_TOKEN", "gateway-token")

    def fake_gh(argv, repo, token):
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(_factory_must_not_be_called(), "/tmp")
    assert report.merged == [] and report.skipped == []
    assert len(report.errors) == 1
    assert "empty login" in report.errors[0][1]


@pytest.mark.parametrize(
    "rollup,ok",
    [
        ([{"name": "CI", "conclusion": "SUCCESS"}], True),
        ([{"name": "CI", "conclusion": "NEUTRAL"}], True),
        ([{"name": "CI", "conclusion": "SKIPPED"}], True),
        ([{"name": "CI", "conclusion": "FAILURE"}], False),
        # A check still queued or running reports no conclusion at all — not
        # yet terminal, and must block exactly like a failure, never pass by
        # default (the bug this module fixes relative to the old
        # review_merge._pr_is_mergeable, which let `None` through as green).
        ([{"name": "CI", "conclusion": None}], False),
        ([{"name": "CI"}], False),
        ([], True),
        (None, False),
        ("not-a-list", False),
        ([{"name": "not-a-dict-later"}, "bad"], False),
        # The acceptance gate re-evaluates the identical policy this gateway
        # enforces directly and is excluded, whatever its conclusion.
        ([{"name": "Acceptance gate", "conclusion": "FAILURE"}], True),
    ],
)
def test_checks_terminal_success(rollup, ok):
    result, _ = _checks_terminal_success(rollup)
    assert result is ok


def test_a_clean_tick_with_no_ready_tasks_resolves_identity_and_does_nothing(monkeypatch):
    """`--match-head-commit` on the actual `gh pr merge` call is exercised by
    the live-database tests in tests/db/test_merge_gateway.py, which can
    carry a real READY_TO_REVIEW task through to a merge; this only pins that
    an otherwise-empty tick still resolves the gateway's own identity (fail
    closed even with nothing to do) and touches `gh` no further."""
    monkeypatch.setenv("AICC_MERGE_GATEWAY_TOKEN", "gateway-token")
    calls = []

    def fake_gh(argv, repo, token):
        calls.append(argv)
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "gateway-bot\n", "")
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)

    def factory():
        raise AssertionError("no tasks queued in this test")

    # merge_once with no READY_TO_REVIEW rows still resolves identity via gh,
    # proving the token/identity plumbing without needing a live queue.
    class _Empty:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def execute(self, *a, **k):
            return None

        def fetchall(self):
            return []

        description = None

    report = merge_once(lambda: _Empty(), "/tmp", GatewayConfig())
    assert report.errors == [] and report.merged == [] and report.skipped == []
    assert calls == [["api", "user", "--jq", ".login"]]
