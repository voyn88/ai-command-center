"""Crash-window recovery for the control-plane merge action."""

from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once
from tests.db.test_backlog_planner import rig  # noqa: F401
from tests.db.test_review_merge import _ready


def test_merge_reconciles_github_success_after_crash_before_database_commit(
    rig,  # noqa: F811
    monkeypatch,
):
    app_factory, store, _ = rig
    task_id = "VOYN-W0-MERGE-RECOVERY"
    pr_url = "https://github.com/x/y/pull/90"
    head = "9" * 40
    merge_sha = "8" * 40
    _ready(store, app_factory, task_id, pr_url)
    calls: list[list[str]] = []

    def fake_gh(argv, repo):
        calls.append(argv)
        body = json.dumps(
            {
                "state": "MERGED",
                "headRefOid": head,
                "mergeCommit": {"oid": merge_sha},
                "author": {"login": "author"},
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "author": {"login": "independent-reviewer"},
                    }
                ],
                "statusCheckRollup": [
                    {"name": "CI", "conclusion": "SUCCESS"}
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp", task_id=task_id)

    assert (task_id, merge_sha) in report.merged
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id = %s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"
        cur.execute(
            "SELECT count(*) FROM backlog_evidence WHERE task_id=%s "
            "AND kind='ci' AND value=%s",
            (task_id, f"MERGED:{merge_sha}"),
        )
        assert cur.fetchone()[0] == 1


def test_merge_queue_enqueue_is_not_reported_or_recorded_as_merged(
    rig,  # noqa: F811
    monkeypatch,
):
    app_factory, store, _ = rig
    task_id = "VOYN-W0-MERGE-QUEUED"
    pr_url = "https://github.com/x/y/pull/93"
    head = "7" * 40
    _ready(store, app_factory, task_id, pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "queued", "")
        if argv[:2] == ["pr", "view"] and "state,mergeCommit" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"state": "OPEN", "mergeCommit": None}), ""
            )
        body = {
            "state": "OPEN",
            "headRefOid": head,
            "author": {"login": "author"},
            "reviews": [
                {
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "author": {"login": "independent-reviewer"},
                }
            ],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp", task_id=task_id)

    assert report.merged == []
    assert (task_id, "merge_queued_awaiting_merge") in report.skipped
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"
        cur.execute(
            "SELECT count(*) FROM backlog_evidence WHERE task_id=%s "
            "AND kind='ci' AND value LIKE 'MERGED:%'",
            (task_id,),
        )
        assert cur.fetchone()[0] == 0
