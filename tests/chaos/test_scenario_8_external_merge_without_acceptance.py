"""Chaos suite scenario 8/8: a concurrent out-of-band merge must never be
silently blessed DONE.

The owner's PR does not only ever merge through this pipeline's own `gh pr
merge` call -- an admin bypass, a hand merge, or a merge queue racing this
tick can land it first. Verification of 53c7b52 (CONFIRMED): when the merge
tick next examines that PR and finds it already MERGED, it must not treat
the merge itself as sufficient evidence. Only a merged head that ALSO
carries the independent ACCEPT marker and a fully green check rollup may
close the task DONE; anything else surfaces loudly as a skip reason for an
operator, exactly the concurrency hazard a chaos suite exists to pin.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess as sp

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import merge_once
from tests.chaos.conftest import _ready


def test_merged_around_the_queue_without_a_marker_never_goes_done(rig, monkeypatch):  # noqa: F811, E501
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS8A"
    pr_url = "https://github.com/x/y/pull/111"
    head, merge_oid = "e1" * 20, "f2" * 20
    _ready(store, app_factory, task_id, pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                "headRefOid": head, "reviews": [],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert (task_id, "merged_without_acceptance_evidence") in report.skipped
    assert not report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merged_with_empty_check_data_is_inconclusive_not_done(rig, monkeypatch):  # noqa: F811, E501
    """The other half of the same race: a merged head whose check rollup
    came back empty (GitHub data unavailable, not "no checks configured")
    must fail closed the same way -- `any()` over an empty list is False,
    so treating that as "nothing failed" would silently wave a merged PR
    through with no evidence its checks ever ran (review of eabe0d3)."""
    app_factory, store, _worker = rig
    task_id = "VOYN-W0-CHAOS8B"
    pr_url = "https://github.com/x/y/pull/112"
    head, merge_oid = "a3" * 20, "b4" * 20
    _ready(store, app_factory, task_id, pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert (task_id, "merged_without_acceptance_evidence") in report.skipped
    assert not report.merged
