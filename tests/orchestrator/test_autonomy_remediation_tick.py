from __future__ import annotations

from contextlib import contextmanager

from command_center.orchestrator import review_merge

PR = "https://github.com/voyn88/ai-command-center/pull/123"
HEAD = "a" * 40


@contextmanager
def _tick(*_args, **_kwargs):
    yield None


def _install_common(monkeypatch, *, accepted: bool, window_reason: str | None):
    pull = {
        "number": 123,
        "html_url": PR,
        "head": {"sha": HEAD},
        "created_at": "2026-09-01T00:00:00Z",
        "user": {"login": "writer"},
        "labels": [{"name": "review-window:blocked"}],
    }
    detailed = {
        "number": 123,
        "url": PR,
        "headRefOid": HEAD,
        "createdAt": "2026-09-01T00:00:00Z",
        "author": {"login": "writer"},
        "labels": [{"name": "review-window:blocked"}],
        "reviews": [],
        "statusCheckRollup": [],
        "commits": [{"oid": HEAD, "committedDate": "2026-09-01T00:00:00Z"}],
    }
    monkeypatch.setattr(review_merge.gh_access, "tick", _tick)
    monkeypatch.setattr(review_merge.gh_access, "detail_cache", lambda: None)
    monkeypatch.setattr(review_merge, "_rows", lambda *_args, **_kwargs: [("TASK", PR)])
    monkeypatch.setattr(review_merge, "_pull_for_pr_url", lambda *_args: pull)
    monkeypatch.setattr(review_merge, "_pr_window_details", lambda *_args, **_kwargs: detailed)
    monkeypatch.setattr(review_merge, "_rest_merge_state", lambda *_args: "CLEAN")
    monkeypatch.setattr(review_merge, "_pr_age_seconds", lambda *_args, **_kwargs: (999999.0, False))
    monkeypatch.setattr(review_merge, "_window_block_reason", lambda *_args, **_kwargs: window_reason)
    monkeypatch.setattr(
        review_merge,
        "_has_accept_marker_with_pull",
        lambda *_args, **_kwargs: (accepted, HEAD),
    )


def test_stale_exact_head_acceptance_enqueues_a_review_refresh(monkeypatch):
    _install_common(monkeypatch, accepted=False, window_reason="stale_exact_head_acceptance")
    refreshed = []
    monkeypatch.setattr(
        review_merge,
        "_enqueue_review_refresh",
        lambda _enqueue, _repo, task_id, pr_url, _pull, _cfg: refreshed.append(
            (task_id, pr_url)
        )
        or None,
    )

    report = review_merge.autonomy_remediate_once(
        object(), object(), "/repo", task_id="TASK"
    )

    assert report.refreshed == [("TASK", PR)]
    assert refreshed == [("TASK", PR)]
    assert not report.remediated


def test_accepted_red_checks_remediate_after_bounded_rerun_is_exhausted(monkeypatch):
    _install_common(monkeypatch, accepted=True, window_reason="checks_stale")
    monkeypatch.setattr(
        review_merge,
        "_pr_is_mergeable",
        lambda *_args, **_kwargs: (False, "checks_not_green: ['CI']"),
    )
    monkeypatch.setattr(review_merge, "_rerun_failed_ci_once", lambda *_args: "")
    monkeypatch.setattr(
        review_merge,
        "_remediate_merge_blocker",
        lambda _factory, task_id, _pr_url, _head, reason: f"{task_id}-REM"
        if reason.startswith("checks_not_green")
        else None,
    )

    report = review_merge.autonomy_remediate_once(
        object(), object(), "/repo", task_id="TASK"
    )

    assert report.remediated == [("TASK", "TASK-REM")]
    assert not report.rerun


def test_accepted_pending_checks_wait_without_rerun_or_remediation(monkeypatch):
    """VOYN-W0-AICC-MERGE-TICK-REJECTS-ON-PENDING-CHECKS: checks still running
    on an accepted head are a timing fact. No flake rerun (nothing failed),
    no remediation task (nothing to fix), no transition -- the tick reports
    `checks_pending` and looks again next tick."""
    _install_common(monkeypatch, accepted=True, window_reason="checks_stale")
    monkeypatch.setattr(
        review_merge,
        "_pr_is_mergeable",
        lambda *_args, **_kwargs: (False, "checks_pending: ['Linux quality shard 1 of 4']"),
    )
    reruns, remediations = [], []
    monkeypatch.setattr(
        review_merge, "_rerun_failed_ci_once", lambda *_args: reruns.append(_args) or ""
    )
    monkeypatch.setattr(
        review_merge,
        "_remediate_merge_blocker",
        lambda *_args: remediations.append(_args) or "TASK-REM",
    )

    report = review_merge.autonomy_remediate_once(
        object(), object(), "/repo", task_id="TASK"
    )

    assert report.remediated == []
    assert reruns == [] and remediations == []
    assert ("TASK", "checks_pending: ['Linux quality shard 1 of 4']") in report.skipped


def test_accepted_red_checks_get_one_bounded_rerun_before_remediation(monkeypatch):
    _install_common(monkeypatch, accepted=True, window_reason="checks_stale")
    monkeypatch.setattr(
        review_merge,
        "_pr_is_mergeable",
        lambda *_args, **_kwargs: (False, "checks_not_green: ['CI']"),
    )
    monkeypatch.setattr(
        review_merge,
        "_rerun_failed_ci_once",
        lambda *_args: "flaky_rerun_dispatched:1",
    )
    remediated = []
    monkeypatch.setattr(
        review_merge,
        "_remediate_merge_blocker",
        lambda *_args: remediated.append(True) or "TASK-REM",
    )

    report = review_merge.autonomy_remediate_once(
        object(), object(), "/repo", task_id="TASK"
    )

    assert report.rerun == [("TASK", "flaky_rerun_dispatched:1")]
    assert not report.remediated
    assert not remediated
