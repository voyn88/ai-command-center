from __future__ import annotations

import json
import subprocess

import pytest

from command_center.orchestrator import acceptance_policy, review_merge

HEAD = "a" * 40


def _write(tmp_path, **overrides):
    payload = {
        "schema_version": 1,
        "policy_version": "test-v1",
        "trusted_reviewer_logins": ["acceptance-bot[bot]"],
        "ci_required_check_names": ["Final merge gate"],
        "merge_required_check_names": [
            "Final merge gate",
            "Acceptance gate (independent verdict on exact SHA)",
        ],
    }
    payload.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload))
    return path


def test_policy_separates_pre_acceptance_ci_from_final_merge_checks(tmp_path):
    policy = acceptance_policy.load(_write(tmp_path))

    assert policy.ci_required_check_names == {"Final merge gate"}
    assert policy.merge_required_check_names == {
        "Final merge gate",
        "Acceptance gate (independent verdict on exact SHA)",
    }


def test_policy_refuses_a_merge_set_that_omits_a_pre_acceptance_check(tmp_path):
    path = _write(
        tmp_path,
        merge_required_check_names=[
            "Acceptance gate (independent verdict on exact SHA)"
        ],
    )

    with pytest.raises(ValueError, match="merge checks must include"):
        acceptance_policy.load(path)


def test_only_policy_authorized_exact_head_review_can_supply_marker():
    policy = acceptance_policy.AcceptancePolicy(
        version="test-v1",
        trusted_reviewer_logins=frozenset({"acceptance-bot[bot]"}),
        ci_required_check_names=frozenset({"Final merge gate"}),
        merge_required_check_names=frozenset(
            {
                "Final merge gate",
                "Acceptance gate (independent verdict on exact SHA)",
            }
        ),
    )
    reviews = [
        {
            "body": f"ACCEPTANCE: ACCEPT {HEAD}",
            "state": "COMMENTED",
            "submittedAt": "2026-08-24T12:00:00Z",
            "author": {"login": "acceptance-bot[bot]"},
        },
        {
            "body": "unrelated collaborator review",
            "state": "COMMENTED",
            "submittedAt": "2026-08-24T12:01:00Z",
            "author": {"login": "collaborator"},
        },
    ]

    assert (
        review_merge._accepted_reviewer_on_latest_review(
            reviews, HEAD, "pull-request-author", policy
        )
        == "acceptance-bot[bot]"
    )

    reviews.append(
        {
            "body": f"ACCEPTANCE: REJECT {HEAD}",
            "state": "COMMENTED",
            "submittedAt": "2026-08-24T12:02:00Z",
            "author": {"login": "acceptance-bot[bot]"},
        }
    )
    assert (
        review_merge._accepted_reviewer_on_latest_review(
            reviews, HEAD, "pull-request-author", policy
        )
        is None
    )


def test_pre_acceptance_ci_can_advance_while_acceptance_gate_is_still_red(
    monkeypatch,
):
    payload = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "statusCheckRollup": [
            {"name": "Final merge gate", "conclusion": "SUCCESS"},
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "FAILURE",
            },
        ],
    }
    monkeypatch.setattr(
        review_merge,
        "_gh",
        lambda argv, repo: subprocess.CompletedProcess(
            argv, 0, json.dumps(payload), ""
        ),
    )

    ok, detail, snapshot = review_merge._ci_snapshot("/tmp", "pr", HEAD)

    assert (ok, detail) == (True, "checks_green")
    assert [check["name"] for check in snapshot["checks"]] == ["Final merge gate"]


def test_merge_requires_both_ci_and_independent_acceptance_checks(monkeypatch):
    payload = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "author": {"login": "pull-request-author"},
        "reviews": [
            {
                "body": f"ACCEPTANCE: ACCEPT {HEAD}",
                "state": "COMMENTED",
                "submittedAt": "2026-08-24T12:00:00Z",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }
        ],
        "statusCheckRollup": [
            {"name": "Final merge gate", "conclusion": "SUCCESS"},
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "FAILURE",
            },
        ],
        "mergeCommit": None,
    }
    monkeypatch.setattr(
        review_merge,
        "_gh",
        lambda argv, repo: subprocess.CompletedProcess(
            argv, 0, json.dumps(payload), ""
        ),
    )

    assert review_merge._pr_is_mergeable("/tmp", "pr", HEAD) == (
        False,
        "checks_not_green: ['Acceptance gate (independent verdict on exact SHA)']",
    )
