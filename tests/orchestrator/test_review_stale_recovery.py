from __future__ import annotations
import json
from types import SimpleNamespace
import pytest
from command_center.orchestrator import review_merge as review

HEAD = "a" * 40
URL = "https://github.com/owner/repo/pull/1"


def setup_recovery(monkeypatch):
    monkeypatch.setenv("AICC_REVIEW_STALE_RECOVERY", "1")
    pull = {
        "state": "open",
        "head": {"sha": HEAD},
        "labels": [{"name": review.DEFAULT_PR_WINDOW_CONFIG.label_blocked}],
    }
    normal = review._PrWindowReadDecision(False, "review_window_inactive", pull)
    monkeypatch.setattr(
        review, "_pr_window_expensive_read_decision", lambda *a, **kw: normal
    )
    data = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "author": {"login": "writer"},
        "reviews": [],
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"name": "Final merge gate", "conclusion": "SUCCESS"},
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "FAILURE",
            },
        ],
    }
    calls = []

    def gh(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))

    monkeypatch.setattr(review, "_gh", gh)
    return normal, data, calls


def test_recovery_can_request_review_before_its_own_acceptance_and_is_bounded(
    monkeypatch,
):
    normal, data, calls = setup_recovery(monkeypatch)
    budget = [3, 1]
    result = review._review_window_read_decision("/repo", URL, budget)
    assert result.allowed and result.reason == "review_stale_head_recovery"
    assert result.pull is normal.pull
    assert budget == [2, 0]
    assert not review._review_window_read_decision("/repo", URL, budget).allowed
    assert len(calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        "flag_off",
        "no_budget",
        "waiting",
        "head_changed",
        "closed",
        "missing_checks",
        "red_ci",
        "pending_ci",
        "skipped_ci",
        "dirty",
        "human_reject",
        "marker_reject",
        "unknown_author",
    ],
)
def test_recovery_never_weakens_code_head_or_rejection_gates(monkeypatch, case):
    normal, data, calls = setup_recovery(monkeypatch)
    budget = [3, 1]
    if case == "flag_off":
        monkeypatch.delenv("AICC_REVIEW_STALE_RECOVERY")
    elif case == "no_budget":
        budget = [0, 1]
    elif case == "waiting":
        normal.pull["labels"] = [
            {"name": review.DEFAULT_PR_WINDOW_CONFIG.label_waiting}
        ]
    elif case == "head_changed":
        data["headRefOid"] = "b" * 40
    elif case == "closed":
        data["state"] = "MERGED"
    elif case == "missing_checks":
        data["statusCheckRollup"] = []
    elif case == "red_ci":
        data["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif case == "pending_ci":
        data["statusCheckRollup"][0]["conclusion"] = None
    elif case == "skipped_ci":
        data["statusCheckRollup"][0]["conclusion"] = "SKIPPED"
    elif case == "dirty":
        data["mergeStateStatus"] = "DIRTY"
    elif case == "human_reject":
        data["reviews"] = [{"state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}}]
    elif case == "marker_reject":
        data["reviews"] = [
            {
                "state": "COMMENTED",
                "author": {"login": "independent"},
                "body": "ACCEPTANCE: REJECT " + HEAD,
                "submittedAt": "2026-11-04T00:00:00Z",
            }
        ]
    elif case == "unknown_author":
        data["author"] = None
    assert not review._review_window_read_decision("/repo", URL, budget).allowed
    assert len(calls) <= 1


def test_recovery_flag_does_not_open_merge_window(monkeypatch):
    setup_recovery(monkeypatch)
    allowed, reason = review._merge_window_allows_expensive_read("/repo", URL)
    assert not allowed and reason == "review_window_inactive"
