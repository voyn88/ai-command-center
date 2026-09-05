"""merge_once (BO-S3b 3/3, VOYN-W0-AICC-MERGE-GATEWAY) on live PostgreSQL:
the store side is real (READY_TO_REVIEW tasks with pr evidence), gh is faked
in-process by patching the module's _gh with a function taking
`(argv, repo, token)` — one argument more than review_merge's old fake,
because every call here must carry the gateway's own credential rather than
whatever the process is ambiently authenticated as.

Review JSON here is shaped the way `gh pr view --json reviews` actually
nests it (`author: {login}`, not the REST API's `user: {login}`) — the
shape `acceptance_policy.verdicts_from` had to be taught to accept for this
module to work with real `gh` output at all."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401 — pytest fixtures
from tests.db.test_review_merge import _ready
from command_center.orchestrator import merge_gateway
from command_center.orchestrator.merge_gateway import GatewayConfig, merge_once

GATEWAY_LOGIN = "voyn-merge-gateway[bot]"
AUTHOR = "task-author"
REVIEWER = "voyn-acceptance[bot]"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("AICC_MERGE_GATEWAY_TOKEN", "gateway-token")


def _pr_view(head, *, reviews, checks, state="OPEN", author=AUTHOR):
    return json.dumps({
        "state": state,
        "headRefOid": head,
        "author": {"login": author},
        "reviews": reviews,
        "statusCheckRollup": checks,
    })


def _accept(head, login=REVIEWER, state="APPROVED"):
    return {"body": f"ACCEPTANCE: ACCEPT {head}", "author": {"login": login}, "state": state}


def _gh_stub(view_body):
    def fake_gh(argv, repo, token):
        assert token == "gateway-token"
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, f"{GATEWAY_LOGIN}\n", "")
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, view_body, "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected call")
    return fake_gh


def test_merge_requires_an_independent_accept_and_terminal_checks(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG1", "https://github.com/x/y/pull/8")
    head = "a" * 40

    calls = []

    def fake_gh(argv, repo, token):
        calls.append(argv)
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, f"{GATEWAY_LOGIN}\n", "")
        if argv[:2] == ["pr", "view"]:
            body = _pr_view(head, reviews=[_accept(head)], checks=[{"name": "CI", "conclusion": "SUCCESS"}])
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MG1", head) in report.merged
    assert report.errors == []

    merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
    assert "--squash" in merge_call
    assert "--match-head-commit" in merge_call and head in merge_call

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MG1",))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='acceptance'",
            ("VOYN-W0-MG1",),
        )
        recorded = cur.fetchone()[0]
        assert REVIEWER in recorded and GATEWAY_LOGIN in recorded and "acceptance_policy_v1" in recorded


def test_merge_skips_without_marker(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG2", "https://github.com/x/y/pull/9")

    body = _pr_view("b" * 40, reviews=[], checks=[{"name": "CI", "conclusion": "SUCCESS"}])
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG2" for t, _ in report.skipped)
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MG2",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_skips_when_a_check_has_not_finished(rig, monkeypatch):
    """A check still queued (`conclusion` absent) must block exactly like a
    failure — the bug the old review_merge._pr_is_mergeable had, letting a
    `None` conclusion through as green."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG3", "https://github.com/x/y/pull/10")
    head = "c" * 40

    body = _pr_view(head, reviews=[_accept(head)], checks=[{"name": "CI", "conclusion": None}])
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG3" and "checks_not_terminal_success" in r for t, r in report.skipped)


def test_merge_refuses_a_self_issued_verdict(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG4", "https://github.com/x/y/pull/11")
    head = "d" * 40

    body = _pr_view(
        head,
        reviews=[_accept(head, login=AUTHOR)],  # the PR's own author "accepting" itself
        checks=[{"name": "CI", "conclusion": "SUCCESS"}],
    )
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG4" and "authored this" in r for t, r in report.skipped)


def test_merge_refuses_a_verdict_issued_by_the_gateway_itself(rig, monkeypatch):
    """VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE: the identity that would
    spend the merge credential is refused on the same footing as the author
    — publishing the verdict and executing the merge must be two identities,
    not one identity running both steps."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG5", "https://github.com/x/y/pull/12")
    head = "e" * 40

    body = _pr_view(
        head,
        reviews=[_accept(head, login=GATEWAY_LOGIN)],
        checks=[{"name": "CI", "conclusion": "SUCCESS"}],
    )
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG5" and "would merge" in r for t, r in report.skipped)


def test_merge_refuses_an_active_rejection(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG6", "https://github.com/x/y/pull/13")
    head = "f" * 40

    reject = {"body": f"ACCEPTANCE: REJECT {head}", "author": {"login": REVIEWER}, "state": "APPROVED"}
    body = _pr_view(head, reviews=[reject], checks=[{"name": "CI", "conclusion": "SUCCESS"}])
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG6" and "REJECTED" in r for t, r in report.skipped)


def test_merge_refuses_a_closed_pr(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG7", "https://github.com/x/y/pull/14")
    head = "0" * 40

    body = _pr_view(head, reviews=[_accept(head)], checks=[{"name": "CI", "conclusion": "SUCCESS"}], state="CLOSED")
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.errors == []
    assert any(t == "VOYN-W0-MG7" and r == "pr_closed" for t, r in report.skipped)


def test_gh_view_failure_is_an_error_not_a_skip(rig, monkeypatch):
    """Fail-closed on GitHub API unavailability: a `gh pr view` failure must
    not look like an ordinary not-ready-yet skip, or an outage would retry
    forever, silently, with nobody able to tell it apart from a PR that
    simply is not accepted yet."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG8", "https://github.com/x/y/pull/15")

    def fake_gh(argv, repo, token):
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, f"{GATEWAY_LOGIN}\n", "")
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 503: GitHub is down")
        raise AssertionError(f"unexpected call: {argv}")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.skipped == []
    assert any(t == "VOYN-W0-MG8" and "gh_view_failed" in r for t, r in report.errors)
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MG8",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_command_failure_is_an_error_not_a_skip(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MG9", "https://github.com/x/y/pull/16")
    head = "1" * 40

    def fake_gh(argv, repo, token):
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, f"{GATEWAY_LOGIN}\n", "")
        if argv[:2] == ["pr", "view"]:
            body = _pr_view(head, reviews=[_accept(head)], checks=[{"name": "CI", "conclusion": "SUCCESS"}])
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 1, "", "422 not mergeable")
        raise AssertionError(f"unexpected call: {argv}")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert report.merged == [] and report.skipped == []
    assert any(t == "VOYN-W0-MG9" and "merge_failed" in r for t, r in report.errors)
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MG9",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_respects_max_per_tick(rig, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MGA", "https://github.com/x/y/pull/17")
    _ready(store, app_factory, "VOYN-W0-MGB", "https://github.com/x/y/pull/18")
    head = "2" * 40

    body = _pr_view(head, reviews=[_accept(head)], checks=[{"name": "CI", "conclusion": "SUCCESS"}])
    monkeypatch.setattr(merge_gateway, "_gh", _gh_stub(body))
    report = merge_once(app_factory, "/tmp", GatewayConfig(max_per_tick=1))
    assert len(report.merged) == 1
