"""VOYN-W0-AICC-MERGE-TICK-RERUNS-CANCELLED-CHECKS: a cancelled latest check is
rerun by the merge tick itself (bounded), instead of blocking an accepted PR
until an operator reruns it."""
from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import review_merge

PR = "https://github.com/voyn88/ai-command-center/pull/1"
HEAD = "a" * 40
URL = "https://github.com/voyn88/ai-command-center/actions/runs/{run}/job/{job}"


def _view(rollup):
    return {
        "state": "OPEN",
        "headRefOid": HEAD,
        "author": {"login": "writer"},
        "reviews": [
            {
                "author": {"login": "voyn88-acceptance-gate[bot]"},
                "state": "COMMENTED",
                "commit": {"oid": HEAD},
                "body": f"ACCEPTANCE: ACCEPT {HEAD}",
                "submittedAt": "2026-09-08T00:00:00Z",
            }
        ],
        "statusCheckRollup": rollup,
    }


def _fake_gh(rollup, attempts):
    calls: list[list[str]] = []

    def fake(argv, repo_path):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_view(rollup)), "")
        if argv[:2] == ["run", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"attempt": attempts.get(argv[2], 1)}), ""
            )
        if argv[:2] == ["run", "rerun"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    return fake, calls


def test_cancelled_latest_check_is_rerun_once_and_reported(monkeypatch):
    rollup = [
        {"name": "CI", "conclusion": "CANCELLED", "startedAt": "2026-09-08T01:00:00Z",
         "detailsUrl": URL.format(run=11, job=1)},
        {"name": "Final merge gate", "conclusion": "CANCELLED",
         "startedAt": "2026-09-08T01:00:00Z", "detailsUrl": URL.format(run=11, job=2)},
        {"name": "Acceptance gate", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:05:00Z", "detailsUrl": URL.format(run=12, job=3)},
    ]
    fake, calls = _fake_gh(rollup, {"11": 1})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is False
    assert reason == "checks_cancelled_rerun_requested: ['11']"
    # Two cancelled checks of the same run are one rerun, not two.
    assert [c for c in calls if c[:2] == ["run", "rerun"]] == [["run", "rerun", "11"]]


def test_a_failed_check_is_a_finding_not_a_rerun(monkeypatch):
    rollup = [{"name": "CI", "conclusion": "FAILURE", "startedAt": "2026-09-08T01:00:00Z",
               "detailsUrl": URL.format(run=11, job=1)}]
    fake, calls = _fake_gh(rollup, {})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is False and reason.startswith("checks_not_green")
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_reruns_stop_after_three_attempts(monkeypatch):
    rollup = [{"name": "CI", "conclusion": "CANCELLED", "startedAt": "2026-09-08T01:00:00Z",
               "detailsUrl": URL.format(run=11, job=1)}]
    fake, calls = _fake_gh(rollup, {"11": 3})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is False and reason.startswith("checks_not_green")
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_a_newer_success_after_a_cancelled_run_needs_no_rerun(monkeypatch):
    rollup = [
        {"name": "CI", "conclusion": "CANCELLED", "startedAt": "2026-09-08T01:00:00Z",
         "detailsUrl": URL.format(run=11, job=1)},
        {"name": "CI", "conclusion": "SUCCESS", "startedAt": "2026-09-08T01:10:00Z",
         "detailsUrl": URL.format(run=13, job=1)},
    ]
    fake, calls = _fake_gh(rollup, {})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is True and reason == HEAD
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]
