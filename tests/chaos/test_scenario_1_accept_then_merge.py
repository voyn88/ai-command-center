"""Chaos suite scenario 1/8: the owner's happy path.

A READY_TO_REVIEW task whose review run comes back ACCEPT gets the
independent marker posted, then merges to DONE with the target-branch merge
commit as evidence -- the baseline every other scenario in this suite
deviates from.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once, publish_review_verdicts
from tests.chaos.conftest import _complete_review, _ready


def test_accept_verdict_posts_marker_then_merges(rig, monkeypatch):  # noqa: F811
    app_factory, store, worker = rig
    task_id = "VOYN-W0-CHAOS1"
    pr_url = "https://github.com/x/y/pull/101"
    head = "1" * 40
    _ready(store, app_factory, task_id, pr_url)
    _complete_review(
        app_factory, worker, task_id, pr_url, head,
        f"Reviewed the diff, found nothing wrong.\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
    )

    def fake_gh_review(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh_review)
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []

    def fake_post(creds, pr_url_arg, decision, sha):
        posted.append((pr_url_arg, decision, sha))
        return True, ""

    monkeypatch.setattr(review_merge, "_post_marker_as_bot", fake_post)
    publish_report = publish_review_verdicts(app_factory, "/tmp")
    assert (task_id, pr_url) in publish_report.reviewed
    assert posted == [(pr_url, "ACCEPT", head)]

    merge_oid = "2" * 40
    merged_state = {"merged": False}

    def fake_gh_merge(argv, repo):
        if argv[:2] == ["pr", "view"]:
            if merged_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            else:
                body = json.dumps({
                    "state": "OPEN", "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            merged_state["merged"] = True
            return sp.CompletedProcess(argv, 0, "merged", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh_merge)
    merge_report = merge_once(app_factory, "/tmp")
    assert (task_id, merge_oid) in merge_report.merged

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='sha'", (task_id,)
        )
        assert cur.fetchone()[0] == merge_oid
