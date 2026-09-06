"""Chaos suite scenario 2/8: CI still pending must never be waved through.

An accepted PR whose required check is still QUEUED/IN_PROGRESS (a CheckRun
with `conclusion: null`) or a legacy StatusContext in `PENDING` state is not
mergeable -- absence of a verdict is not a passing verdict. The task stays
in READY_TO_REVIEW, and the merge tick spends no rerun budget on a check
that has not finished yet (VOYN-W0-AICC-DISABLE-UNSAFE-AUTOMERGE).
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once
from tests.chaos.conftest import _ready


def test_check_run_still_in_progress_blocks_merge(rig, monkeypatch):  # noqa: F811
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS2A"
    pr_url = "https://github.com/x/y/pull/102"
    head = "3" * 40
    _ready(store, app_factory, task_id, pr_url)

    def fake_gh(argv, repo):
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [{"name": "CI", "status": "IN_PROGRESS", "conclusion": None}],
        })
        return sp.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert (task_id, "checks_not_green: ['CI']") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_legacy_pending_status_context_blocks_merge(rig, monkeypatch):  # noqa: F811
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS2B"
    pr_url = "https://github.com/x/y/pull/103"
    head = "4" * 40
    _ready(store, app_factory, task_id, pr_url)

    def fake_gh(argv, repo):
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [{"name": "legacy-ci", "state": "PENDING"}],
        })
        return sp.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert (task_id, "checks_not_green: ['legacy-ci']") in report.skipped
