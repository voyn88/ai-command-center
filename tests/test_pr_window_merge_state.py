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


def test_blocked_pr_refreshes_cached_stale_checks_before_staying_blocked(monkeypatch):
    head = "b" * 40
    light_pr = {
        "number": 2,
        "url": "https://github.com/x/repo/pull/2",
        "headRefOid": head,
        "createdAt": "2026-01-01T00:00:00Z",
        "author": {"login": "alice"},
        "labels": [{"name": "review-window:blocked"}],
    }
    labels: list[tuple[int, str]] = []
    cache_payload = {
        "reviews": [],
        "statusCheckRollup": [
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            }
        ],
        "commits": [{"oid": head, "committedDate": "2099-01-01T00:00:00Z"}],
    }

    class Cache:
        enabled = True

        def get(self, _repo: str, _number: int, _head: str) -> dict:
            return cache_payload

        def put(self, _repo: str, _number: int, _head: str, payload: dict) -> None:
            cache_payload.update(payload)

        def prune(self) -> int:
            return 0

    monkeypatch.setattr(
        review_merge, "_list_open_pulls", lambda _repo_path, _cfg: ([light_pr], None)
    )
    monkeypatch.setattr(review_merge.gh_access, "detail_cache", Cache)
    monkeypatch.setattr(
        review_merge, "_rest_reviews", lambda _repo_path, _owner, _repo, _number: []
    )
    monkeypatch.setattr(
        review_merge,
        "_rest_check_rollup",
        lambda _repo_path, _owner, _repo, _head: [
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
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
        "/repo",
        PrWindowConfig(
            max_active=1,
            stale_seconds=10**12,
            required_checks=("Acceptance gate (independent verdict on exact SHA)",),
        ),
    )

    assert report.active == [(2, head)]
    assert report.blocked == []
    assert labels == [(2, "review-window:active")]


def test_old_blocked_pr_does_not_starve_fresh_candidate(monkeypatch):
    stale_blocked = _pr(1, "a" * 40, label="review-window:blocked")
    stale_blocked["statusCheckRollup"] = [
        {"name": "Final merge gate", "status": "COMPLETED", "conclusion": "FAILURE"}
    ]
    fresh = _pr(2, "b" * 40)
    labels: list[tuple[int, str]] = []

    monkeypatch.setattr(
        review_merge,
        "_list_open_pulls",
        lambda _repo_path, _cfg: ([stale_blocked, fresh], None),
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

    assert report.active == [(2, "b" * 40)]
    assert report.blocked == [(1, "checks_stale")]
    assert labels == [(2, "review-window:active"), (1, "review-window:blocked")]


def test_blocked_refresh_budget_leaves_old_blocked_tail_untouched(monkeypatch):
    old_blocked = [
        {
            "number": number,
            "url": f"https://github.com/x/repo/pull/{number}",
            "headRefOid": str(number) * 40,
            "createdAt": f"2026-01-0{number}T00:00:00Z",
            "author": {"login": "alice"},
            "labels": [{"name": "review-window:blocked"}],
        }
        for number in (1, 2, 3)
    ]
    fresh = _pr(4, "d" * 40)
    labels: list[tuple[int, str]] = []
    fetched: list[int] = []

    def details(_repo_path, pr, _cache=None, *, fetch=True, refresh=False, include_reviews=True):
        if not fetch:
            return None
        fetched.append(pr["number"])
        detailed = dict(pr)
        detailed["reviews"] = []
        detailed["statusCheckRollup"] = (
            []
            if pr["number"] == 4
            else [
                {
                    "name": "Final merge gate",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ]
        )
        detailed["commits"] = [
            {
                "oid": detailed["headRefOid"],
                "committedDate": "2099-01-01T00:00:00Z",
            }
        ]
        return detailed

    monkeypatch.setattr(
        review_merge,
        "_list_open_pulls",
        lambda _repo_path, _cfg: ([*old_blocked, fresh], None),
    )
    monkeypatch.setattr(review_merge, "_pr_window_details", details)
    monkeypatch.setattr(
        review_merge,
        "_set_pr_window_labels",
        lambda _repo_path, pr, _cfg, desired: labels.append((pr["number"], desired))
        or True,
    )

    report = reconcile_pr_window(
        "/repo",
        PrWindowConfig(
            max_active=1,
            stale_seconds=10**12,
            detail_budget=10,
            blocked_refresh_budget=1,
        ),
    )

    assert fetched == [4, 1]
    assert report.active == [(4, "d" * 40)]
    assert report.blocked == [(1, "checks_stale")]
    assert report.unchecked == [(2, "2" * 40), (3, "3" * 40)]
    assert labels == [(4, "review-window:active"), (1, "review-window:blocked")]


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
