"""Chaos suite scenario 3/8: a genuinely red change costs exactly one extra
rerun, never a retry loop.

VOYN-W0-AICC-CI-FLAKE-AUTO-RERUN bounds the flake retry by GitHub's own run
`attempt` counter: only a completed-and-failed run still at attempt 1 gets
rerun. Two ticks against a change that is really broken (the rerun fails
again, bumping GitHub's attempt counter to 2) must dispatch a rerun exactly
once and then stop -- the second tick sees the same failing check and
declines to rerun it again, leaving the task in READY_TO_REVIEW for a human
rather than looping.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once
from tests.chaos.conftest import _ready


def test_a_genuinely_broken_head_is_rerun_once_then_left_blocked(rig, monkeypatch):  # noqa: F811, E501
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS3"
    pr_url = "https://github.com/x/y/pull/104"
    head = "5" * 40
    _ready(store, app_factory, task_id, pr_url)

    reruns = []
    # Mutated between ticks to model GitHub's own attempt counter advancing
    # once the dispatched rerun itself completes red again.
    run_state = {"attempt": 1}

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"] and "headRefName" in argv[-1]:
            return sp.CompletedProcess(argv, 0, json.dumps(
                {"headRefName": "backlog/x", "headRefOid": head}), "")
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "list"]:
            return sp.CompletedProcess(argv, 0, json.dumps([
                {"databaseId": 21, "headSha": head, "status": "completed",
                 "conclusion": "failure", "attempt": run_state["attempt"]},
            ]), "")
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv)
            return sp.CompletedProcess(argv, 0, "", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)

    first = merge_once(app_factory, "/tmp")
    first_skip = dict(first.skipped)[task_id]
    assert first_skip.startswith("checks_not_green") and "flaky_rerun_dispatched:1" in first_skip
    assert reruns == [["run", "rerun", "21", "--failed"]]

    # The dispatched rerun completed and is still red -- GitHub bumps the
    # attempt counter, exactly the signal `_rerun_failed_ci_once` uses to
    # never touch this run again.
    run_state["attempt"] = 2
    second = merge_once(app_factory, "/tmp")
    second_skip = dict(second.skipped)[task_id]
    assert second_skip == "checks_not_green: ['CI']"
    assert reruns == [["run", "rerun", "21", "--failed"]]  # no second dispatch

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"
