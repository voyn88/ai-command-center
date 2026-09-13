"""Chaos suite scenario 4/8 -- previously missing (claim 34): the full
pending -> failed -> rerun -> green sequence in one deterministic test.

Individually, "a pending check blocks merge" and "a failed check on an
accepted head gets one bounded rerun" were each covered in
tests/db/test_review_merge.py, but no single test walked a PR through the
whole sequence a real flaky CI run actually produces: the check starts
QUEUED/IN_PROGRESS while the workflow is still running, later reports
FAILURE once it finishes red, the merge tick's bounded flake retry
(VOYN-W0-AICC-CI-FLAKE-AUTO-RERUN) reruns the failed jobs, and the rerun
comes back green -- at which point the PR merges. This test drives
`merge_once` across three ticks against one mutable fake `gh`, asserting the
task's status and the merge tick's report at every step, so the three
already-covered pieces are pinned as one connected state machine rather than
three independent facts that happen to be true in isolation.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once
from tests.chaos.conftest import _ready


def test_pending_then_failed_then_rerun_recovers_to_merge(rig, monkeypatch):  # noqa: F811, E501
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS4"
    pr_url = "https://github.com/x/y/pull/105"
    head = "6" * 40
    merge_oid = "7" * 40
    _ready(store, app_factory, task_id, pr_url)

    # ci_state advances the fake CI run through exactly the sequence a live
    # flaky run produces: still running, then completed-and-failed, then
    # completed-and-green after the tick's own rerun.
    ci_state = {"phase": "pending"}
    reruns = []
    merged_state = {"merged": False}

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"] and "headRefName" in argv[-1]:
            return sp.CompletedProcess(argv, 0, json.dumps(
                {"headRefName": "backlog/chaos4", "headRefOid": head}), "")
        if argv[:2] == ["pr", "view"]:
            if merged_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
                return sp.CompletedProcess(argv, 0, body, "")
            check = {
                "pending": {"name": "CI", "status": "IN_PROGRESS", "conclusion": None},
                "failed": {"name": "CI", "conclusion": "FAILURE"},
                "green": {"name": "CI", "conclusion": "SUCCESS"},
            }[ci_state["phase"]]
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [check],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "list"]:
            if ci_state["phase"] != "failed":
                return sp.CompletedProcess(argv, 0, "[]", "")
            return sp.CompletedProcess(argv, 0, json.dumps([
                {"databaseId": 31, "headSha": head, "status": "completed",
                 "conclusion": "failure", "attempt": 1},
            ]), "")
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv)
            return sp.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["pr", "merge"]:
            merged_state["merged"] = True
            return sp.CompletedProcess(argv, 0, "merged", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)

    # Tick 1: the workflow is still running. Blocked, no rerun spent -- a
    # running check is not a failure, so the flake retry has nothing to do.
    pending_report = merge_once(app_factory, "/tmp")
    assert (task_id, "checks_not_green: ['CI']") in pending_report.skipped
    assert reruns == []
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"

    # Tick 2: the workflow finished red. The accepted head's failure gets one
    # bounded flake rerun dispatched, but the task is still not merged --
    # the rerun's own result has not landed yet.
    ci_state["phase"] = "failed"
    failed_report = merge_once(app_factory, "/tmp")
    failed_skip = dict(failed_report.skipped)[task_id]
    assert failed_skip.startswith("checks_not_green") and "flaky_rerun_dispatched:1" in failed_skip
    assert reruns == [["run", "rerun", "31", "--failed"]]
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"

    # Tick 3: the rerun came back green. Merge proceeds normally, no further
    # rerun spent, and the task closes DONE with the target-branch merge
    # commit as evidence.
    ci_state["phase"] = "green"
    merged_report = merge_once(app_factory, "/tmp")
    assert (task_id, merge_oid) in merged_report.merged
    assert reruns == [["run", "rerun", "31", "--failed"]]  # unchanged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='sha'", (task_id,)
        )
        assert cur.fetchone()[0] == merge_oid
