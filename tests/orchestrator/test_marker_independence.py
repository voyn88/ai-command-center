"""Merger independence for the acceptance marker.

The Actions gate and ``review_merge`` must agree: the ACCEPT line is the
first line, the reviewer is neither the pull-request author nor the login
that will run ``gh pr merge``, and a missing login is not an accept.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from command_center.orchestrator import review_merge

HEAD = "a" * 40
PR = "https://github.com/example/repo/pull/7"
AUTHOR = "dimastov-lab"
REVIEWER = "voyn88-acceptance-gate[bot]"


def _review(body: str, login: str | None = REVIEWER, **extra: object) -> dict:
    review: dict = {"body": body, "state": "COMMENTED", **extra}
    if login is not None:
        review["author"] = {"login": login}
    return review


def _view(reviews: list[dict], author: str | None = AUTHOR) -> dict:
    payload: dict = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "reviews": reviews,
        "statusCheckRollup": [
            {"name": "Final merge gate", "conclusion": "SUCCESS"},
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "SUCCESS",
            },
        ],
    }
    if author is not None:
        payload["author"] = {"login": author}
    return payload


def _patch_gh(monkeypatch, payload: dict) -> None:
    def fake(argv, repo_path):
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        raise AssertionError(argv)

    monkeypatch.setattr(review_merge, "_gh", fake)


def test_merge_refuses_when_the_reviewer_is_the_merger(monkeypatch):
    _patch_gh(monkeypatch, _view([_review(f"ACCEPTANCE: ACCEPT {HEAD}", login=REVIEWER)]))
    monkeypatch.setattr(
        review_merge, "_gh_current_login", lambda repo_path, pr_url: REVIEWER
    )
    ready, reason = review_merge._pr_is_mergeable(PR, PR)
    assert ready is False
    assert reason == "no_accept_marker_on_head"


def test_merge_refuses_when_the_merger_login_cannot_be_read(monkeypatch):
    _patch_gh(monkeypatch, _view([_review(f"ACCEPTANCE: ACCEPT {HEAD}")]))
    monkeypatch.setattr(
        review_merge, "_gh_current_login", lambda repo_path, pr_url: None
    )
    ready, reason = review_merge._pr_is_mergeable(PR, PR)
    assert ready is False
    assert reason == "merger_identity_unresolved"


def test_merge_ignores_a_marker_that_is_not_the_first_line(monkeypatch):
    body = f"quoting an old line\nACCEPTANCE: ACCEPT {HEAD}\n"
    _patch_gh(monkeypatch, _view([_review(body)]))
    monkeypatch.setattr(
        review_merge, "_gh_current_login", lambda repo_path, pr_url: "merge-bot"
    )
    ready, reason = review_merge._pr_is_mergeable(PR, PR)
    assert ready is False
    assert reason == "no_accept_marker_on_head"


def test_merge_refuses_an_unattributable_marker(monkeypatch):
    _patch_gh(monkeypatch, _view([_review(f"ACCEPTANCE: ACCEPT {HEAD}", login=None)]))
    monkeypatch.setattr(
        review_merge, "_gh_current_login", lambda repo_path, pr_url: "merge-bot"
    )
    ready, reason = review_merge._pr_is_mergeable(PR, PR)
    assert ready is False
    assert reason == "no_accept_marker_on_head"


def test_gh_current_login_pins_the_pull_request_host(monkeypatch):
    seen: list[list[str]] = []

    def fake(argv, repo_path):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"login": "merge-bot"}), ""
        )

    monkeypatch.setattr(review_merge, "_gh", fake)
    assert review_merge._gh_current_login("/repo", PR) == "merge-bot"
    assert seen == [["api", "user", "--hostname", "github.com"]]


def test_gh_current_login_fails_closed_without_a_host(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_gh",
        lambda argv, repo_path: pytest.fail("lookup must not run"),
    )
    assert review_merge._gh_current_login("/repo", "not-a-url") is None
