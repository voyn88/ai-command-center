from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from command_center.orchestrator import review_merge

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
HEAD = "a" * 40


def _pr(
    number: int,
    *,
    labels: tuple[str, ...] = (),
    created: str = "2026-09-06T01:00:00Z",
    conclusion: str = "SUCCESS",
    accepted: bool = True,
    merge_state: str = "CLEAN",
) -> dict:
    reviews = []
    if accepted:
        reviews = [{
            "body": f"ACCEPTANCE: ACCEPT {HEAD}",
            "submittedAt": "2026-09-06T02:00:00Z",
            "author": {"login": "acceptance-bot"},
        }]
    return {
        "number": number,
        "url": f"https://github.com/voyn88/ai-command-center/pull/{number}",
        "createdAt": created,
        "updatedAt": created,
        "isDraft": False,
        "mergeStateStatus": merge_state,
        "labels": [{"name": label} for label in labels],
        "statusCheckRollup": [{"name": "CI", "conclusion": conclusion}],
        "reviews": reviews,
        "headRefOid": HEAD,
        "author": {"login": "publisher"},
        "state": "OPEN",
    }


def _fake_github(monkeypatch, prs):
    edits: list[list[str]] = []

    def fake(argv, _repo):
        if argv[:2] == ["label", "create"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["pr", "list", "--state"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(prs), "")
        if argv[:2] == ["pr", "edit"]:
            edits.append(list(argv))
            target = next(pr for pr in prs if pr["url"] == argv[2])
            names = {label["name"] for label in target["labels"]}
            index = 3
            while index < len(argv):
                verb, label = argv[index], argv[index + 1]
                if verb == "--add-label":
                    names.add(label)
                elif verb == "--remove-label":
                    names.discard(label)
                index += 2
            target["labels"] = [{"name": name} for name in sorted(names)]
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(review_merge, "_gh", fake)
    return edits


def test_blocked_active_rotates_out_and_oldest_eligible_waiting_promotes(monkeypatch):
    blocked = _pr(1, labels=("queue-active",), conclusion="FAILURE")
    oldest = _pr(2, labels=("queue-waiting-review",), created="2026-09-06T00:00:00Z")
    newer = _pr(3, labels=("queue-waiting-review",), created="2026-09-06T00:30:00Z")
    edits = _fake_github(monkeypatch, [blocked, newer, oldest])

    report = review_merge.reconcile_pr_window(
        "/repo",
        review_merge.PrWindowConfig(target_active=1, max_active=2),
        now=NOW,
    )

    assert report.promoted == [("2", oldest["url"])]
    assert ("1", "checks_failed:CI") in report.blocked
    assert report.demoted == [("1", "checks_failed:CI")]
    assert {label["name"] for label in blocked["labels"]} == {
        "queue-blocked", "queue-waiting-review"
    }
    assert {label["name"] for label in oldest["labels"]} == {"queue-active"}
    assert len(edits) == 2


def test_maximum_is_hard_and_repeated_tick_is_idempotent(monkeypatch):
    prs = [
        _pr(1, labels=("queue-active",), created="2026-09-06T00:00:00Z"),
        _pr(2, labels=("queue-active",), created="2026-09-06T00:10:00Z"),
        _pr(3, labels=("queue-active",), created="2026-09-06T00:20:00Z"),
    ]
    edits = _fake_github(monkeypatch, prs)
    cfg = review_merge.PrWindowConfig(target_active=1, max_active=2)

    first = review_merge.reconcile_pr_window("/repo", cfg, now=NOW)
    edit_count = len(edits)
    second = review_merge.reconcile_pr_window("/repo", cfg, now=NOW)

    assert first.demoted == [("3", "active_window_cap")]
    assert len(edits) == edit_count
    assert set(second.unchanged) == {"1", "2", "3"}
    assert sum(
        "queue-active" in {label["name"] for label in pr["labels"]}
        for pr in prs
    ) == 2


def test_stale_or_self_authored_acceptance_never_enters_active_window(monkeypatch):
    missing = _pr(1, labels=("queue-active",), accepted=False)
    self_review = _pr(2, labels=("queue-active",))
    self_review["reviews"][0]["author"] = {"login": "publisher"}
    _fake_github(monkeypatch, [missing, self_review])

    report = review_merge.reconcile_pr_window(
        "/repo",
        review_merge.PrWindowConfig(target_active=1, max_active=2, stale_seconds=60),
        now=NOW,
    )

    assert report.promoted == []
    assert report.demoted == [
        ("1", "stale_exact_head_acceptance"),
        ("2", "stale_exact_head_acceptance"),
    ]


def test_explicit_rejection_rotates_out_without_waiting_for_stale_timeout(monkeypatch):
    rejected = _pr(1, labels=("queue-active",), created="2026-09-06T02:59:30Z")
    rejected["reviews"][0]["body"] = f"ACCEPTANCE: REJECT {HEAD}"
    _fake_github(monkeypatch, [rejected])

    report = review_merge.reconcile_pr_window(
        "/repo",
        review_merge.PrWindowConfig(target_active=1, max_active=2),
        now=NOW,
    )

    assert report.demoted == [("1", "acceptance_rejected")]


def test_pending_checks_are_active_work_not_a_false_failure(monkeypatch):
    pending = _pr(
        1,
        labels=("queue-waiting-review",),
        conclusion="",
        created="2026-09-06T02:59:30Z",
    )
    pending["statusCheckRollup"][0] = {
        "name": "CI", "status": "IN_PROGRESS", "conclusion": None
    }
    _fake_github(monkeypatch, [pending])

    report = review_merge.reconcile_pr_window(
        "/repo",
        review_merge.PrWindowConfig(target_active=1, max_active=2),
        now=NOW,
    )

    assert report.promoted == [("1", pending["url"])]
    assert report.blocked == []


def test_rfc3339_z_timestamp_keeps_fresh_pr_inside_grace_period():
    fresh = _pr(1, accepted=False, created="2026-09-06T02:59:30Z")

    assert review_merge._pr_age_seconds(fresh, NOW) == 30
    assert review_merge._window_block_reason(
        fresh,
        review_merge.PrWindowConfig(stale_seconds=60),
        NOW,
    ) is None
