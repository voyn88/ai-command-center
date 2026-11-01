"""VOYN-W0-AICC-MERGE-TICK-RERUNS-CANCELLED-CHECKS: a cancelled latest check is
rerun by the merge tick itself (bounded), instead of blocking an accepted PR
until an operator reruns it."""
from __future__ import annotations

import json
import subprocess

import pytest

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


def _fake_gh(rollup, attempts, runs=None):
    calls: list[list[str]] = []

    def fake(argv, repo_path):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_view(rollup)), "")
        if argv[:2] == ["run", "list"]:
            listing = runs
            if listing is None:
                by_run = {}
                for check in rollup:
                    match = review_merge._RUN_ID_IN_DETAILS_URL.search(
                        check.get("detailsUrl", "")
                    )
                    if match:
                        run_id = match.group(1)
                        by_run[run_id] = {
                            "databaseId": int(run_id), "headSha": HEAD,
                            "workflowName": "CI" if run_id in ("11", "13") else "Acceptance",
                            "createdAt": check["startedAt"], "status": "completed",
                            "conclusion": check["conclusion"].lower(),
                            "attempt": attempts.get(run_id, 1),
                        }
                listing = list(by_run.values())
            return subprocess.CompletedProcess(argv, 0, json.dumps(listing), "")
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
        {"name": "Final merge gate", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:10:00Z", "detailsUrl": URL.format(run=13, job=2)},
        {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:12:00Z", "detailsUrl": URL.format(run=14, job=1)},
    ]
    fake, calls = _fake_gh(rollup, {})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is True and reason == HEAD
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_stale_failed_duplicate_check_is_ignored_after_newer_success(monkeypatch):
    """Latest checks determine eligibility; native merge protection still applies.

    Restarting superseded failures can cancel the replacement CI run.
    """
    rollup = [
        {"name": "Final merge gate", "conclusion": "FAILURE",
         "startedAt": "2026-09-08T01:00:00Z", "detailsUrl": URL.format(run=11, job=1)},
        {"name": "Final merge gate", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:10:00Z", "detailsUrl": URL.format(run=13, job=1)},
        {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:12:00Z", "detailsUrl": URL.format(run=14, job=1)},
    ]
    fake, calls = _fake_gh(rollup, {"11": 1})
    monkeypatch.setattr(review_merge, "_gh", fake)

    ok, reason = review_merge._pr_is_mergeable("/repo", PR)

    assert ok is True and reason == HEAD
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_stale_cancelled_duplicate_check_is_not_restarted(monkeypatch):
    rollup = [
        {"name": "Final merge gate", "conclusion": "CANCELLED",
         "startedAt": "2026-09-08T01:00:00Z", "detailsUrl": URL.format(run=11, job=1)},
        {"name": "Final merge gate", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:10:00Z", "detailsUrl": URL.format(run=13, job=1)},
        {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:12:00Z", "detailsUrl": URL.format(run=14, job=1)},
    ]
    fake, calls = _fake_gh(rollup, {"11": 1})
    monkeypatch.setattr(review_merge, "_gh", fake)

    ok, reason = review_merge._pr_is_mergeable("/repo", PR)

    assert ok is True and reason == HEAD
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_exhausted_stale_duplicate_does_not_block_newer_success(monkeypatch):
    rollup = [
        {"name": "Final merge gate", "conclusion": "FAILURE",
         "startedAt": "2026-09-08T01:00:00Z", "detailsUrl": URL.format(run=11, job=1)},
        {"name": "Final merge gate", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:10:00Z", "detailsUrl": URL.format(run=13, job=1)},
        {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS",
         "startedAt": "2026-09-08T01:12:00Z", "detailsUrl": URL.format(run=14, job=1)},
    ]
    fake, calls = _fake_gh(rollup, {"11": 3})
    monkeypatch.setattr(review_merge, "_gh", fake)

    ok, reason = review_merge._pr_is_mergeable("/repo", PR)

    assert ok is True and reason == HEAD
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def test_a_head_whose_required_gates_never_ran_is_not_mergeable(monkeypatch):
    """Absence of information is not a verdict: an all-green rollup that lacks
    the required contexts (gates skipped or never triggered) must not merge."""
    rollup = [{"name": "Secret scan", "conclusion": "SUCCESS",
               "startedAt": "2026-09-08T01:00:00Z", "detailsUrl": URL.format(run=11, job=1)}]
    fake, calls = _fake_gh(rollup, {})
    monkeypatch.setattr(review_merge, "_gh", fake)
    ok, reason = review_merge._pr_is_mergeable("/repo", PR)
    assert ok is False
    assert reason == (
        "checks_missing: ['Final merge gate', "
        "'Acceptance gate (independent verdict on exact SHA)']"
    )
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


@pytest.mark.parametrize("status,conclusion", [
    ("queued", None), ("in_progress", None), ("completed", "success"),
])
def test_pending_or_green_replacement_suppresses_old_cancelled_job(
    monkeypatch, status, conclusion,
):
    rollup = [{"name": "CI", "conclusion": "CANCELLED",
               "startedAt": "2026-09-08T01:00:00Z",
               "detailsUrl": URL.format(run=11, job=1)}]
    old = _run(11, "CI", "completed", "cancelled")
    new = _run(13, "CI", status, conclusion)
    fake, calls = _fake_gh(rollup, {}, runs=[new, old])
    monkeypatch.setattr(review_merge, "_gh", fake)
    assert review_merge._rerun_cancelled_latest_runs("/repo", rollup, HEAD) == []
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def _run(run_id, workflow, status="completed", conclusion="failure", attempt=1):
    return {
        "databaseId": run_id, "headSha": HEAD, "workflowName": workflow,
        "createdAt": f"2026-09-08T01:{run_id:02d}:00Z",
        "status": status, "conclusion": conclusion, "attempt": attempt,
    }


@pytest.mark.parametrize("status,conclusion", [
    ("queued", None), ("in_progress", None), ("completed", "success"),
])
def test_flake_retry_ignores_superseded_runs(monkeypatch, status, conclusion):
    runs = [_run(11, "CI"), _run(13, "CI", status, conclusion),
            _run(12, "CI", "completed", "cancelled")]
    calls = _flake_fake(monkeypatch, runs)
    assert review_merge._rerun_failed_ci_once("/repo", PR) == ""
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]


def _flake_fake(monkeypatch, runs):
    calls = []

    def fake(argv, repo_path):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            body = {"headRefName": "feature", "headRefOid": HEAD}
        elif argv[:2] == ["run", "list"]:
            body = runs
        elif argv[:2] == ["run", "rerun"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(review_merge, "_gh", fake)
    return calls


def test_flake_retry_selects_only_latest_failure_per_workflow(monkeypatch):
    runs = [_run(13, "CI"), _run(11, "CI"), _run(12, "CI"),
            _run(14, "Lint", attempt=2)]
    calls = _flake_fake(monkeypatch, runs)
    assert review_merge._rerun_failed_ci_once("/repo", PR) == "flaky_rerun_dispatched:1"
    assert [c for c in calls if c[:2] == ["run", "rerun"]] == [
        ["run", "rerun", "13", "--failed"],
    ]
    # After GitHub reflects the retry, a later tick cannot dispatch it again.
    runs[0]["attempt"] = 2
    assert review_merge._rerun_failed_ci_once("/repo", PR) == ""


def test_missing_workflow_metadata_does_not_authorize_retry(monkeypatch):
    run = _run(11, "CI")
    del run["workflowName"]
    calls = _flake_fake(monkeypatch, [run])
    assert review_merge._rerun_failed_ci_once("/repo", PR) == ""
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]



def test_failed_workflow_retry_is_not_blocked_by_another_pending_workflow(monkeypatch):
    runs = [_run(11, "CI"), _run(12, "Lint", "queued", None)]
    calls = _flake_fake(monkeypatch, runs)
    assert review_merge._rerun_failed_ci_once("/repo", PR) == "flaky_rerun_dispatched:1"
    assert [c for c in calls if c[:2] == ["run", "rerun"]] == [
        ["run", "rerun", "11", "--failed"],
    ]


def test_same_timestamp_newer_run_id_and_skipped_label_run_suppress_retry(monkeypatch):
    old, new = _run(11, "CI"), _run(12, "CI", conclusion="skipped")
    new["createdAt"] = old["createdAt"]
    calls = _flake_fake(monkeypatch, [old, new])
    assert review_merge._rerun_failed_ci_once("/repo", PR) == ""
    assert not [c for c in calls if c[:2] == ["run", "rerun"]]
