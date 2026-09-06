"""Chaos suite scenario 7/8 -- previously missing (claim 34): the full
reject -> remediation -> accept -> merge chain, end to end.

This is the deterministic stand-in for a live event that VOYN-W0-AICC-
REVIEW-STUCK-ON-TRANSIENT-FAILURE currently blocks from being confirmed
against real GitHub: an original task rejected by review, its automatically
spawned remediation task pushing a fix, that fix earning an ACCEPT verdict,
and the resulting PR merging to DONE -- driven across the same three
functions (`publish_review_verdicts`, `publish_review_verdicts` again,
`merge_once`) a live daemon tick would call, against one continuous fake
`gh` and a real backlog_task/backlog_task_remediation history, rather than
four isolated facts asserted independently. Until the live path is
unblocked, this is deterministic coverage of the whole chain instead of
none.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once, publish_review_verdicts
from tests.chaos.conftest import _complete_review, _ready


def test_reject_remediation_accept_merge_chain(rig, monkeypatch):  # noqa: F811
    app_factory, store, worker = rig
    task_id = "VOYN-W0-CHAOS7"
    rem_task_id = f"{task_id}-REM"
    original_pr = "https://github.com/x/y/pull/109"
    remediation_pr = "https://github.com/x/y/pull/110"
    original_head = "b" * 40
    remediation_head = "c" * 40
    merge_oid = "d" * 40

    # -- Step 1: the original task's review comes back REJECT. --
    _ready(store, app_factory, task_id, original_pr)
    feedback = "Found a real defect: off-by-one in the retry bound."
    _complete_review(
        app_factory, worker, task_id, original_pr, original_head,
        f"{feedback}\nVERDICT: REJECT\nHEAD_SHA: {original_head}\n",
    )

    def fake_gh_reject(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({"headRefOid": original_head, "reviews": []})
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh_reject)
    reject_report = publish_review_verdicts(app_factory, "/tmp")
    assert (task_id, rem_task_id) in reject_report.remediated

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "REJECTED"
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (rem_task_id,))
        assert cur.fetchone()[0] == "OPEN"

    # -- Step 2: the remediation task pushes its own PR and earns ACCEPT. --
    _ready(store, app_factory, rem_task_id, remediation_pr)
    _complete_review(
        app_factory, worker, rem_task_id, remediation_pr, remediation_head,
        f"Fix addresses the feedback.\nVERDICT: ACCEPT\nHEAD_SHA: {remediation_head}\n",
    )

    def fake_gh_accept(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({"headRefOid": remediation_head, "reviews": []})
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh_accept)
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []

    def fake_post(creds, pr_url_arg, decision, sha):
        posted.append((pr_url_arg, decision, sha))
        return True, ""

    monkeypatch.setattr(review_merge, "_post_marker_as_bot", fake_post)
    accept_report = publish_review_verdicts(app_factory, "/tmp")
    assert (rem_task_id, remediation_pr) in accept_report.reviewed
    assert posted == [(remediation_pr, "ACCEPT", remediation_head)]

    # -- Step 3: green checks + the posted marker let the merge tick land it. --
    merged_state = {"merged": False}

    def fake_gh_merge(argv, repo):
        if argv[:2] == ["pr", "view"]:
            if merged_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": remediation_head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {remediation_head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            else:
                body = json.dumps({
                    "state": "OPEN", "headRefOid": remediation_head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {remediation_head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            merged_state["merged"] = True
            return sp.CompletedProcess(argv, 0, "merged", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh_merge)
    merge_report = merge_once(app_factory, "/tmp")
    assert (rem_task_id, merge_oid) in merge_report.merged

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (rem_task_id,))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='sha'", (rem_task_id,)
        )
        assert cur.fetchone()[0] == merge_oid
        # The original task is untouched by its remediation's success: still
        # the terminal REJECTED leaf its own rejection left it at.
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "REJECTED"
