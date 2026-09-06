"""Chaos suite scenario 5/8: a REJECT verdict must not dead-end.

VOYN-W0-AICC-REVIEW-REJECT-REMEDIATION-LOOP: a rejection dispatches a new,
linked follow-up task carrying the review's own feedback (0010's design --
a new task, never a cycle back into the rejected task's own state machine),
and the original task lands on the terminal REJECTED leaf. This is the
first half of the reject -> remediation -> accept -> merge chain; scenario
7 in this suite drives the rest of it in one continuous test.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import publish_review_verdicts
from tests.chaos.conftest import _complete_review, _ready


def test_reject_dispatches_a_linked_remediation_task(rig, monkeypatch):  # noqa: F811
    app_factory, store, worker = rig
    task_id = "VOYN-W0-CHAOS5"
    pr_url = "https://github.com/x/y/pull/106"
    head = "8" * 40
    _ready(store, app_factory, task_id, pr_url)
    feedback = "Found a real defect: the retry loop never terminates."
    _complete_review(
        app_factory, worker, task_id, pr_url, head,
        f"{feedback}\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    rem_task_id = f"{task_id}-REM"
    assert (task_id, rem_task_id) in report.remediated

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "REJECTED"
        cur.execute("SELECT status, body FROM backlog_task WHERE task_id=%s", (rem_task_id,))
        new_status, new_body = cur.fetchone()
        assert new_status == "OPEN"
        assert feedback in new_body
        cur.execute(
            "SELECT parent_task_id, pr_url, rejected_head_sha "
            "FROM backlog_task_remediation WHERE task_id=%s",
            (rem_task_id,),
        )
        parent, linked_pr, linked_sha = cur.fetchone()
        assert (parent, linked_pr, linked_sha) == (task_id, pr_url, head)
