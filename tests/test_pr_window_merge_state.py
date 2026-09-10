from __future__ import annotations

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import PrWindowConfig, reconcile_pr_window


def _pr(number: int, head: str, *, label: str | None = None, merge_state: str = "CLEAN"):
    return {
        "number": number,
        "url": f"https://github.com/x/repo/pull/{number}",
        "headRefOid": head,
        "createdAt": f"2026-01-0{number}T00:00:00Z",
        "author": {"login": "alice"},
        "labels": ([{"name": label}] if label else []),
        "reviews": [],
        "statusCheckRollup": [],
        "commits": [{"oid": head, "committedDate": "2099-01-01T00:00:00Z"}],
        "mergeStateStatus": merge_state,
    }


def test_conflicted_active_pr_does_not_hold_the_window(monkeypatch):
    conflicted = _pr(1, "a" * 40, label="review-window:active", merge_state="DIRTY")
    clean = _pr(2, "b" * 40)
    labels: list[tuple[int, str]] = []

    monkeypatch.setattr(
        review_merge,
        "_list_open_pulls",
        lambda _repo_path, _cfg: ([conflicted, clean], None),
    )
    monkeypatch.setattr(
        review_merge,
        "_set_pr_window_labels",
        lambda _repo_path, pr, _cfg, desired: labels.append((pr["number"], desired))
        or True,
    )

    report = reconcile_pr_window(
        "/repo", PrWindowConfig(max_active=1, stale_seconds=10**12)
    )

    assert report.blocked == [(1, "merge_conflict")]
    assert report.active == [(2, "b" * 40)]
    assert labels == [(1, "review-window:blocked"), (2, "review-window:active")]


def test_conflicted_candidate_is_not_promoted_into_an_empty_slot(monkeypatch):
    conflicted = _pr(1, "a" * 40, merge_state="DIRTY")
    clean = _pr(2, "b" * 40)
    labels: list[tuple[int, str]] = []

    monkeypatch.setattr(
        review_merge,
        "_list_open_pulls",
        lambda _repo_path, _cfg: ([conflicted, clean], None),
    )
    monkeypatch.setattr(
        review_merge,
        "_set_pr_window_labels",
        lambda _repo_path, pr, _cfg, desired: labels.append((pr["number"], desired))
        or True,
    )

    report = reconcile_pr_window(
        "/repo", PrWindowConfig(max_active=1, stale_seconds=10**12)
    )

    assert report.blocked == [(1, "merge_conflict")]
    assert report.active == [(2, "b" * 40)]
    assert labels == [(1, "review-window:blocked"), (2, "review-window:active")]


def test_active_pr_refreshes_cached_stale_checks_before_demoting(monkeypatch):
    head = "a" * 40
    light_pr = {
        "number": 1,
        "url": "https://github.com/x/repo/pull/1",
        "headRefOid": head,
        "createdAt": "2026-01-01T00:00:00Z",
        "author": {"login": "alice"},
        "labels": [{"name": "review-window:active"}],
    }
    labels: list[tuple[int, str]] = []
    cache_payload = {
        "reviews": [],
        "statusCheckRollup": [
            {"name": "Final merge gate", "status": "COMPLETED", "conclusion": "FAILURE"}
        ],
        "commits": [{"oid": head, "committedDate": "2099-01-01T00:00:00Z"}],
    }

    class Cache:
        enabled = True

        def __init__(self) -> None:
            self.writes: list[dict] = []

        def get(self, _repo: str, _number: int, _head: str) -> dict:
            return cache_payload

        def put(self, _repo: str, _number: int, _head: str, payload: dict) -> None:
            self.writes.append(payload)

        def prune(self) -> int:
            return 0

    cache = Cache()

    monkeypatch.setattr(
        review_merge, "_list_open_pulls", lambda _repo_path, _cfg: ([light_pr], None)
    )
    monkeypatch.setattr(review_merge.gh_access, "detail_cache", lambda: cache)
    monkeypatch.setattr(
        review_merge, "_rest_reviews", lambda _repo_path, _owner, _repo, _number: []
    )
    monkeypatch.setattr(
        review_merge,
        "_rest_check_rollup",
        lambda _repo_path, _owner, _repo, _head: [
            {"name": "Final merge gate", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    )
    monkeypatch.setattr(
        review_merge,
        "_head_commit_committed_date",
        lambda _repo_path, _pr: "2099-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        review_merge, "_rest_merge_state", lambda _repo_path, _owner, _repo, _number: "CLEAN"
    )
    monkeypatch.setattr(
        review_merge,
        "_set_pr_window_labels",
        lambda _repo_path, pr, _cfg, desired: labels.append((pr["number"], desired))
        or True,
    )

    report = reconcile_pr_window(
        "/repo", PrWindowConfig(max_active=1, stale_seconds=10**12)
    )

    assert report.active == [(1, head)]
    assert report.blocked == []
    assert labels == [(1, "review-window:active")]
    assert cache.writes[-1]["statusCheckRollup"][0]["conclusion"] == "SUCCESS"


def test_in_progress_checks_keep_the_active_window_label(monkeypatch):
    head = "a" * 40
    pr = _pr(1, head, label="review-window:active")
    pr["statusCheckRollup"] = [
        {"name": "Linux quality shard 1 of 4", "status": "IN_PROGRESS", "conclusion": None}
    ]
    labels: list[tuple[int, str]] = []

    monkeypatch.setattr(
        review_merge, "_list_open_pulls", lambda _repo_path, _cfg: ([pr], None)
    )
    monkeypatch.setattr(
        review_merge,
        "_set_pr_window_labels",
        lambda _repo_path, item, _cfg, desired: labels.append((item["number"], desired))
        or True,
    )

    report = reconcile_pr_window(
        "/repo", PrWindowConfig(max_active=1, stale_seconds=10**12)
    )

    assert report.active == [(1, head)]
    assert report.blocked == []
    assert labels == [(1, "review-window:active")]
