from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("block", ["merge_conflict", "checks_stale", "acceptance_rejected"])
def test_queue_active_block_cannot_hold_slot_ahead_of_eligible_pr(monkeypatch, block):
    selected = _pr(1, "a" * 40, label="queue-active")
    if block == "merge_conflict":
        selected["mergeStateStatus"] = "DIRTY"
    elif block == "checks_stale":
        selected["statusCheckRollup"] = [{"name": "Final merge gate", "conclusion": "FAILURE"}]
    else:
        selected["reviews"] = [{"author": {"login": "reviewer"}, "state": "COMMENTED",
                                "body": "ACCEPTANCE: REJECT " + "a" * 40}]
    clean = _pr(2, "b" * 40)
    writes = []
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: ([selected, clean], None))
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels",
                        lambda _, pr, cfg, remove: writes.append((pr["number"], remove)) or True)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels",
                        lambda _, pr, cfg, label: writes.append((pr["number"], label)) or True)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, stale_seconds=10**12))
    assert report.blocked == [(1, block)]
    assert report.active == [(2, "b" * 40)]
    assert writes[:2] == [(1, {"queue-active"}), (1, "review-window:blocked")]


def test_failed_queue_label_removal_reserves_slot_and_is_not_reported_blocked(monkeypatch):
    selected = _pr(1, "a" * 40, label="queue-active", merge_state="DIRTY")
    clean = _pr(2, "b" * 40)
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: ([selected, clean], None))
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels", lambda *_: False)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels", lambda *_: True)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, stale_seconds=10**12))
    assert report.unreadable == [(1, "a" * 40)]
    assert report.active == []
    assert report.blocked == []
    assert report.waiting == [(2, "b" * 40)]


def test_two_eligible_queue_active_labels_cannot_exceed_window_capacity(monkeypatch):
    prs = [_pr(1, "a" * 40, label="queue-active"), _pr(2, "b" * 40, label="queue-active")]
    removed = []
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: (prs, None))
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels",
                        lambda _, pr, cfg, remove: removed.append((pr["number"], remove)) or True)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels", lambda *_: True)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, stale_seconds=10**12))
    assert report.active == [(1, "a" * 40)]
    assert report.waiting == [(2, "b" * 40)]
    assert (2, {"queue-active"}) in removed


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


@pytest.mark.parametrize("label", [None, "review-window:active", "queue-active"])
def test_draft_from_rest_listing_frees_slot_without_detail_reads(monkeypatch, label):
    draft = {"number": 1, "html_url": "https://github.com/x/repo/pull/1",
             "head": {"sha": "a" * 40}, "draft": True,
             "labels": [{"name": label}] if label else []}
    clean = _pr(2, "b" * 40)
    writes = []
    monkeypatch.setattr(review_merge, "_origin_owner_repo", lambda _: ("x", "repo"))
    monkeypatch.setattr(review_merge, "_rest_json", lambda *_: (review_merge._REST_OK, [draft]))
    listed, failure = review_merge._list_open_pulls("/repo", PrWindowConfig())
    assert failure is None and listed[0]["isDraft"] is True
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: (listed + [clean], None))
    original_details = review_merge._pr_window_details

    def details(repo, pr, *args, **kwargs):
        assert pr["number"] != 1, "draft must be excluded before any detail/cache lookup"
        return original_details(repo, pr, *args, **kwargs)

    monkeypatch.setattr(review_merge, "_pr_window_details", details)
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels", lambda _, pr, cfg, labels: writes.append((pr["number"], labels)) or True)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels", lambda _, pr, cfg, label: writes.append((pr["number"], label)) or True)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, detail_budget=0, stale_seconds=10**12))
    assert report.blocked == [(1, "draft")]
    assert report.active == [(2, "b" * 40)]
    assert writes[:2] == [(1, {"queue-active", "review-window:active"}), (1, "review-window:blocked")]


@pytest.mark.parametrize("cached_draft", [None, False, True])
@pytest.mark.parametrize("listed_draft", [False, True])
def test_fresh_draft_state_overrides_cached_missing_or_stale_flag(monkeypatch, cached_draft, listed_draft):
    cached = _pr(1, "a" * 40)
    if cached_draft is not None:
        cached["isDraft"] = cached_draft
    light = review_merge._light_pr_from_rest({
        "number": 1, "html_url": "https://github.com/x/repo/pull/1",
        "head": {"sha": "a" * 40}, "draft": listed_draft,
        "labels": [{"name": "review-window:blocked"}],
    })

    class Cache:
        enabled = True

        def get(self, *_):
            return cached

    cache = Cache()
    detailed = review_merge._pr_window_details("/repo", light, cache, fetch=False)
    assert detailed["isDraft"] is listed_draft
    cfg = PrWindowConfig(max_active=1, detail_budget=0, stale_seconds=10**12)
    assert review_merge._window_block_reason(detailed, cfg, age_seconds=0) == ("draft" if listed_draft else None)
    # A readied PR with the same head is admitted despite an old blocked label
    # and a cache that still says draft; no cache flush/detail request required.
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: ([light], None))
    monkeypatch.setattr(review_merge.gh_access, "detail_cache", lambda: cache)
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels", lambda *_: True)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels", lambda *_: True)
    report = reconcile_pr_window("/repo", cfg)
    assert report.active == ([] if listed_draft else [(1, "a" * 40)])
    assert report.blocked == ([(1, "draft")] if listed_draft else [])


def test_draft_failed_active_label_removal_stays_fail_closed(monkeypatch):
    draft = _pr(1, "a" * 40, label="queue-active")
    draft["isDraft"] = True
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: ([draft, _pr(2, "b" * 40)], None))
    monkeypatch.setattr(review_merge, "_remove_pr_window_labels", lambda *_: False)
    monkeypatch.setattr(review_merge, "_set_pr_window_labels", lambda *_: True)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, stale_seconds=10**12))
    assert report.unreadable == [(1, "a" * 40)]
    assert not report.active and not report.blocked
    assert report.waiting == [(2, "b" * 40)]


@pytest.mark.parametrize("active_labels", [
    {"review-window:active"}, {"queue-active"},
    {"review-window:active", "queue-active"},
])
def test_draft_demotion_mutates_each_active_label_once(monkeypatch, active_labels):
    import subprocess
    from urllib.parse import unquote

    draft = _pr(1, "a" * 40)
    draft["isDraft"] = True
    live_labels = active_labels | {"P1", "maintainer-note"}
    draft["labels"] = [{"name": name} for name in sorted(live_labels)]
    calls = []
    monkeypatch.setattr(review_merge, "_list_open_pulls", lambda *_: ([draft], None))

    def transport(args, _repo):
        calls.append(args)
        method = args[2]
        if method == "DELETE":
            label = unquote(args[3].rsplit("/", 1)[1])
            assert label in live_labels, "duplicate DELETE would fail with GitHub 404"
            live_labels.remove(label)
        elif method == "POST":
            assert args[4] == "-f"
            live_labels.add(args[5].removeprefix("labels[]="))
        else:
            raise AssertionError(f"unexpected mutation: {args}")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", transport)
    report = reconcile_pr_window("/repo", PrWindowConfig(max_active=1, detail_budget=0))
    assert report.blocked == [(1, "draft")] and not report.unreadable
    assert live_labels == {"P1", "maintainer-note", "review-window:blocked"}
    assert len([call for call in calls if call[2] == "DELETE"]) == len(active_labels)
    assert len([call for call in calls if call[2] == "POST"]) == 1
