"""review_once / merge_once (BO-S3b 2/3, 3/3) on live PostgreSQL: the store
side is real (READY_TO_REVIEW tasks with pr evidence), enqueue is a recording
stub, and merge_once's gh calls are faked in-process by patching
merge_gateway's _gh — merge_once itself no longer talks to gh at all, it only
calls into the gateway (see tests/orchestrator/test_merge_gateway.py for the
gateway's own fail-closed coverage)."""

from __future__ import annotations

import json


from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401 — pytest fixtures
from command_center.orchestrator import merge_gateway
from command_center.orchestrator.review_merge import (
    merge_once, review_once,
)

_TOKEN_ENV = merge_gateway.GATEWAY_TOKEN_ENV



def _ready(store, factory, task_id, pr):
    """A task in READY_TO_REVIEW with a pr evidence row — the state part 1
    leaves behind."""
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task(task_id, repo="repo-x", status="OPEN"))[0]
    with factory() as c, c.cursor() as cur:
        # walk OPEN -> IN_PROGRESS -> READY_TO_REVIEW via the real machine;
        # transition's third arg is the bigint revision, re-read each step.
        def _rev():
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev()))
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task_id, _rev()))
        c.commit()


def test_review_enqueues_one_run_per_ready_task(rig):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R1", "https://github.com/x/y/pull/7")
    calls = []
    report = review_once(app_factory, lambda q, k, p: calls.append((q, k, p)))
    assert ("VOYN-W0-R1", "https://github.com/x/y/pull/7") in report.reviewed
    assert len(calls) == 1
    q, key, payload = calls[0]
    assert key == "review:VOYN-W0-R1"  # idempotency key
    assert payload["task_type"] == "review" and "pull/7" in payload["prompt"]


def test_merge_requires_accept_marker_and_green_checks(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1", "https://github.com/x/y/pull/8")
    head = "a" * 40
    monkeypatch.setenv(_TOKEN_ENV, "test-gateway-token")

    def fake_gh(argv, repo, token):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "author": {"login": "server-worker"},
                "reviews": [{
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "user": {"login": "voyn-acceptance[bot]"}, "state": "COMMENTED",
                }],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:1] == ["api"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1", head) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1",))
        assert cur.fetchone()[0] == "DONE"


def test_merge_skips_without_marker(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M2", "https://github.com/x/y/pull/9")
    monkeypatch.setenv(_TOKEN_ENV, "test-gateway-token")

    def fake_gh(argv, repo, token):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": "b" * 40,
            "author": {"login": "server-worker"}, "reviews": [],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(
        t == "VOYN-W0-M2" and r.startswith("acceptance_refused") for t, r in report.skipped
    )
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M2",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_skips_when_a_check_is_red(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M3", "https://github.com/x/y/pull/10")
    head = "c" * 40
    monkeypatch.setenv(_TOKEN_ENV, "test-gateway-token")

    def fake_gh(argv, repo, token):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "server-worker"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "user": {"login": "voyn-acceptance[bot]"}, "state": "COMMENTED",
            }],
            "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(merge_gateway, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(t == "VOYN-W0-M3" and "checks_not_terminal_success" in r for t, r in report.skipped)


def test_merge_refuses_without_a_gateway_credential_even_with_a_perfect_pr(rig, monkeypatch):  # noqa: F811
    """A process running merge_once with no VOYN_MERGE_GATEWAY_TOKEN in its
    environment (a worker or planner host, by construction) cannot merge a
    single PR no matter how clean that PR is — the credential check inside
    the gateway runs before any network call, and this test never even wires
    up a fake gh to make sure of it."""

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M4", "https://github.com/x/y/pull/11")
    monkeypatch.delenv(_TOKEN_ENV, raising=False)

    def unreachable(argv, repo, token):
        raise AssertionError("merge_once must not touch gh without a gateway credential")

    monkeypatch.setattr(merge_gateway, "_gh", unreachable)
    report = merge_once(app_factory, "/tmp")
    assert any(
        t == "VOYN-W0-M4" and "gateway_credential_missing" in r for t, r in report.skipped
    )
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M4",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched