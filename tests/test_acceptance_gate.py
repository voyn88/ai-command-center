"""The acceptance gate refuses everything it cannot positively establish.

Independent acceptance on the exact head commit is a delivery rule that GitHub
cannot enforce for this repository (see
`scripts/assert_independent_acceptance.py` for both closed routes). These tests
pin the machine enforcement that replaces it, and every one of them is a
negative control: each names a way a pull request could reach `main` without a
real verdict, and asserts the gate stops it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.assert_independent_acceptance import (
    AcceptanceError,
    _merge_group_numbers,
    assert_accepted,
    evaluate,
    parse_marker,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/acceptance-gate.yml"

# Two distinct commit ids. `detect-secrets` reads any 40-character hex string as
# a possible credential, and a fixture sha is the one place that heuristic is
# wrong on purpose.
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"  # pragma: allowlist secret
OTHER = "0f1e2d3c4b5a69788796a5b4c3d2e1f098765432"  # pragma: allowlist secret
AUTHOR = "dimastov-lab"
REVIEWER = "voyn-acceptance[bot]"


def review(body: str, login: str = REVIEWER, state: str = "COMMENTED") -> dict:
    return {"body": body, "user": {"login": login}, "state": state}


def accept(sha: str = HEAD, **kwargs) -> dict:
    return review(f"ACCEPTANCE: ACCEPT {sha}", **kwargs)


def reject(sha: str = HEAD, **kwargs) -> dict:
    return review(f"ACCEPTANCE: REJECT {sha}", **kwargs)


def test_a_verdict_from_a_different_identity_on_the_current_head_passes() -> None:
    """The one accepting case, so the negatives below are not vacuous."""
    assert evaluate([accept()], HEAD, AUTHOR) == REVIEWER


def test_a_verdict_may_carry_its_reasoning_below_the_marker() -> None:
    body = (
        f"ACCEPTANCE: ACCEPT {HEAD}\n\nChecked the migration and the rollback path.\n"
    )
    assert evaluate([review(body)], HEAD, AUTHOR) == REVIEWER


# --------------------------------------------------------------------------
# Negative controls
# --------------------------------------------------------------------------


def test_no_review_at_all_is_refused() -> None:
    """The default state of every pull request, and it must not be a pass."""
    with pytest.raises(AcceptanceError, match="no acceptance verdict"):
        evaluate([], HEAD, AUTHOR)


def test_a_review_without_a_marker_is_refused() -> None:
    with pytest.raises(AcceptanceError, match="no acceptance verdict"):
        evaluate([review("Looks good to me, shipping.")], HEAD, AUTHOR)


def test_a_verdict_for_a_different_commit_is_refused() -> None:
    """Acceptance is per commit: a verdict survives no push."""
    with pytest.raises(AcceptanceError, match="no verdict names the current head"):
        evaluate([accept(OTHER)], HEAD, AUTHOR)


def test_a_rejection_on_the_current_head_is_refused() -> None:
    with pytest.raises(AcceptanceError, match="was REJECTED"):
        evaluate([reject()], HEAD, AUTHOR)


def test_a_rejection_outranks_an_acceptance_on_the_same_commit() -> None:
    """An earlier ACCEPT does not overturn a rejection that still stands."""
    with pytest.raises(AcceptanceError, match="was REJECTED"):
        evaluate([accept(), reject()], HEAD, AUTHOR)
    # ...in either order: the rejection is not merely "the last word".
    with pytest.raises(AcceptanceError, match="was REJECTED"):
        evaluate([reject(), accept()], HEAD, AUTHOR)


def test_a_verdict_published_by_the_pull_requests_author_is_refused() -> None:
    """The whole point. Independence is compared on `login`.

    `authorAssociation` is what makes the built-in route unusable — the
    acceptance app reports `NONE` — while `login` distinguishes the identities
    exactly, so the check is written against `login` and would silently pass
    everything if it were written against association.
    """
    with pytest.raises(AcceptanceError, match="who authored this"):
        evaluate([accept(login=AUTHOR)], HEAD, AUTHOR)


def test_author_comparison_ignores_login_case() -> None:
    """GitHub logins are case-insensitive; the bypass would be `DiMaStOv-Lab`."""
    with pytest.raises(AcceptanceError, match="who authored this"):
        evaluate([accept(login="DiMaStOv-Lab")], HEAD, AUTHOR)


def test_a_marker_that_is_not_the_first_line_is_refused() -> None:
    """A body that merely *contains* a verdict is prose, not a verdict.

    Without this, quoting the required line while recommending against merge —
    or an unrelated comment pasting a previous verdict — would accept the
    commit.
    """
    body = f"I would be inclined to write:\n\nACCEPTANCE: ACCEPT {HEAD}\n\nbut the tests are red."
    assert parse_marker(body) is None
    with pytest.raises(AcceptanceError, match="no acceptance verdict"):
        evaluate([review(body)], HEAD, AUTHOR)


def test_a_marker_indented_or_reworded_is_not_a_verdict() -> None:
    for body in (
        f"  ACCEPTANCE: ACCEPT {HEAD}",
        f"ACCEPTANCE:ACCEPT {HEAD}",
        f"ACCEPTANCE: ACCEPTED {HEAD}",
        f"acceptance: accept {HEAD}",
        f"> ACCEPTANCE: ACCEPT {HEAD}",
        "ACCEPTANCE: ACCEPT " + HEAD[:39],
        f"ACCEPTANCE: ACCEPT {HEAD} (with reservations)",
    ):
        assert parse_marker(body) is None, body


def test_a_dismissed_acceptance_no_longer_accepts() -> None:
    with pytest.raises(AcceptanceError, match="dismissed"):
        evaluate([accept(state="DISMISSED")], HEAD, AUTHOR)


def test_an_unsubmitted_draft_verdict_does_not_accept() -> None:
    with pytest.raises(AcceptanceError, match="no acceptance verdict"):
        evaluate([accept(state="PENDING")], HEAD, AUTHOR)


def test_an_unattributable_verdict_cannot_establish_independence() -> None:
    with pytest.raises(AcceptanceError, match="no acceptance verdict"):
        evaluate(
            [{"body": f"ACCEPTANCE: ACCEPT {HEAD}", "state": "COMMENTED"}], HEAD, AUTHOR
        )


@pytest.mark.parametrize("head", [None, "", "not-a-sha", HEAD[:39], 12345])
def test_an_unusable_head_sha_is_refused(head: object) -> None:
    with pytest.raises(AcceptanceError, match="head sha is not"):
        evaluate([accept()], head, AUTHOR)


def test_an_unresolvable_pull_request_author_is_refused() -> None:
    with pytest.raises(AcceptanceError, match="no resolvable author"):
        evaluate([accept()], HEAD, None)


def test_a_malformed_review_list_is_refused_rather_than_read_as_empty() -> None:
    for reviews in ({"body": "..."}, "ACCEPT", None):
        with pytest.raises(AcceptanceError):
            evaluate(reviews, HEAD, AUTHOR)


def test_a_missing_token_refuses_instead_of_passing_unverified(tmp_path: Path) -> None:
    """No token means the verdict is unreadable, which is not the same as fine."""
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"number": 1}}', encoding="utf-8")
    env = {
        "GITHUB_REPOSITORY": "dimastov-lab/ai-command-center",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_EVENT_NAME": "pull_request",
    }
    with pytest.raises(AcceptanceError, match="GITHUB_TOKEN is required"):
        assert_accepted(env)


def test_an_event_without_a_pull_request_is_refused(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text('{"ref": "refs/heads/main"}', encoding="utf-8")
    with pytest.raises(AcceptanceError, match="unsupported event"):
        assert_accepted(
            {
                "GITHUB_REPOSITORY": "dimastov-lab/ai-command-center",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_TOKEN": "x",
            }
        )


def test_an_unreadable_event_payload_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AcceptanceError, match="event payload is unreadable"):
        assert_accepted(
            {
                "GITHUB_REPOSITORY": "dimastov-lab/ai-command-center",
                "GITHUB_EVENT_PATH": str(tmp_path / "absent.json"),
                "GITHUB_TOKEN": "x",
            }
        )


def test_a_missing_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AcceptanceError, match="GITHUB_REPOSITORY is not set"):
        assert_accepted({"GITHUB_EVENT_PATH": str(tmp_path / "e.json")})


# --------------------------------------------------------------------------
# The workflow that carries the check
# --------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_the_gate_re_runs_when_the_verdict_arrives_after_ci() -> None:
    """Without `pull_request_review` the gate is a permanent red.

    A verdict is published after CI has already reported. Subscribed to
    `pull_request` alone, the gate would have run once, found nothing, failed,
    and had no further event to re-evaluate the accepted commit — so the only
    way to clear it would be a push, which invalidates the verdict that was
    just given.
    """
    workflow = _workflow()
    assert set(workflow["on"]) == {
        "pull_request",
        "pull_request_review",
        "merge_group",
    }
    assert set(workflow["on"]["pull_request_review"]["types"]) == {
        "submitted",
        "edited",
        "dismissed",
    }
    assert workflow["on"]["merge_group"]["types"] == ["checks_requested"]
    # `edited` and `dismissed` matter as much as `submitted`: a verdict body
    # edited into a rejection, or an acceptance dismissed, must re-judge.


def test_the_check_name_is_the_one_branch_protection_can_require() -> None:
    (job,) = _workflow()["jobs"].values()
    assert job["name"] == "Acceptance gate (independent verdict on exact SHA)"


def test_the_gate_asks_for_no_more_access_than_it_reads() -> None:
    """It reads a pull request and its reviews. It writes nothing, ever."""
    assert _workflow()["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_the_gate_runs_under_a_shell_that_aborts_and_says_so_explicitly() -> None:
    """Declared, not inherited — the reason `ci.yml` pins the same thing.

    An inherited shell depends on `runs-on`; moving the job to `windows-latest`
    would silently take the `pwsh` default, where a non-zero exit from a native
    command is not a terminating error and the refusal would go green.
    """
    for step in _workflow()["jobs"]["acceptance-gate"]["steps"]:
        if "run" in step:
            assert step.get("shell") in {"bash", "sh"}, step.get("name")


def test_the_gate_has_a_deliberate_failure_canary() -> None:
    steps = _workflow()["jobs"]["acceptance-gate"]["steps"]
    (canary,) = [
        step for step in steps if step.get("name") == "Deliberate failure canary"
    ]
    assert "release-gate-canary-acceptance" in canary["if"]
    assert canary["run"].strip() == "exit 1"


def test_the_gates_actions_are_immutable_sha_pinned() -> None:
    for step in _workflow()["jobs"]["acceptance-gate"]["steps"]:
        if uses := step.get("uses"):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), uses


def test_the_gate_runs_the_verifier_that_exists() -> None:
    assert (ROOT / "scripts/assert_independent_acceptance.py").is_file()
    commands = " ".join(
        step.get("run", "") for step in _workflow()["jobs"]["acceptance-gate"]["steps"]
    )
    assert "scripts/assert_independent_acceptance.py" in commands


# --------------------------------------------------------------------------
# Merge-group resolution
# --------------------------------------------------------------------------


BASE = "1111111111111111111111111111111111111111"  # pragma: allowlist secret
SYNTHETIC_ONE = "2222222222222222222222222222222222222222"  # pragma: allowlist secret
SYNTHETIC_TWO = "3333333333333333333333333333333333333333"  # pragma: allowlist secret
HEAD_ELEVEN = "4444444444444444444444444444444444444444"  # pragma: allowlist secret
HEAD_TWELVE = "5555555555555555555555555555555555555555"  # pragma: allowlist secret


def merge_group_payload(number: int = 12) -> dict:
    return {
        "merge_group": {
            "base_sha": BASE,
            "head_sha": SYNTHETIC_TWO,
            "base_ref": "refs/heads/main",
            "head_ref": (f"refs/heads/gh-readonly-queue/main/pr-{number}-{BASE}"),
        }
    }


def comparison(commits: list[dict]) -> dict:
    return {
        "ahead_by": len(commits),
        "total_commits": len(commits),
        "merge_base_commit": {"sha": BASE},
        "commits": commits,
    }


def synthetic(sha: str, parent: str, number: int) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": parent}],
        "commit": {"message": f"Queued change (#{number})\n\nDetails"},
    }


def queue_entry(position: int, number: int, head: str, base: str = BASE) -> dict:
    return {
        "position": position,
        "state": "AWAITING_CHECKS",
        "baseCommit": {"oid": base},
        "headCommit": {"oid": head},
        "pullRequest": {
            "number": number,
            "headRefOid": head,
            "baseRefName": "main",
            "state": "OPEN",
        },
    }


def queue_response(entries: list[dict]) -> dict:
    return {
        "data": {
            "repository": {
                "mergeQueue": {
                    "entries": {
                        "nodes": entries,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    }


def mock_merge_group_apis(monkeypatch, entries: list[dict]) -> None:
    response = comparison(
        [
            synthetic(SYNTHETIC_ONE, BASE, 11),
            synthetic(SYNTHETIC_TWO, SYNTHETIC_ONE, 12),
        ]
    )
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._api", lambda _path, _env: response
    )
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._graphql",
        lambda _query, _variables, _env: queue_response(entries),
    )


def test_a_batch_merge_group_resolves_every_pull_request(monkeypatch) -> None:
    mock_merge_group_apis(
        monkeypatch,
        [
            queue_entry(1, 11, HEAD_ELEVEN),
            queue_entry(2, 12, HEAD_TWELVE),
        ],
    )

    queued_pulls, base = _merge_group_numbers(
        merge_group_payload(), "dimastov-lab/ai-command-center", {"GITHUB_TOKEN": "x"}
    )

    assert queued_pulls == [(11, HEAD_ELEVEN), (12, HEAD_TWELVE)]
    assert base == "main"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(total_commits=3), "incomplete"),
        (
            lambda value: value["commits"][1].update(parents=[{"sha": BASE}]),
            "discontinuous",
        ),
        (
            lambda value: value["commits"][1]["commit"].update(message="No PR suffix"),
            "no unambiguous pull request number",
        ),
    ],
)
def test_an_ambiguous_merge_group_is_refused(monkeypatch, mutate, message) -> None:
    response = comparison(
        [
            synthetic(SYNTHETIC_ONE, BASE, 11),
            synthetic(SYNTHETIC_TWO, SYNTHETIC_ONE, 12),
        ]
    )
    mutate(response)
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._api", lambda _path, _env: response
    )
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._graphql",
        lambda _query, _variables, _env: queue_response(
            [queue_entry(1, 11, HEAD_ELEVEN), queue_entry(2, 12, HEAD_TWELVE)]
        ),
    )

    with pytest.raises(AcceptanceError, match=message):
        _merge_group_numbers(
            merge_group_payload(),
            "dimastov-lab/ai-command-center",
            {"GITHUB_TOKEN": "x"},
        )


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([queue_entry(1, 12, HEAD_TWELVE)], "#11 is no longer"),
        (
            [queue_entry(2, 11, HEAD_ELEVEN), queue_entry(1, 12, HEAD_TWELVE)],
            "order disagrees",
        ),
        (
            [queue_entry(1, 11, HEAD_ELEVEN), queue_entry(3, 12, HEAD_TWELVE)],
            "not contiguous",
        ),
    ],
)
def test_a_group_that_disagrees_with_live_queue_is_refused(
    monkeypatch, entries, message
) -> None:
    mock_merge_group_apis(monkeypatch, entries)

    with pytest.raises(AcceptanceError, match=message):
        _merge_group_numbers(
            merge_group_payload(),
            "dimastov-lab/ai-command-center",
            {"GITHUB_TOKEN": "x"},
        )


def test_every_group_member_is_bound_to_its_exact_queued_head(monkeypatch) -> None:
    stale = queue_entry(1, 11, HEAD_ELEVEN)
    stale["headCommit"]["oid"] = HEAD_TWELVE
    mock_merge_group_apis(
        monkeypatch,
        [
            stale,
            queue_entry(2, 12, HEAD_TWELVE),
        ],
    )

    with pytest.raises(AcceptanceError, match="stale relative"):
        _merge_group_numbers(
            merge_group_payload(),
            "dimastov-lab/ai-command-center",
            {"GITHUB_TOKEN": "x"},
        )


def test_a_group_with_the_wrong_final_queue_ref_number_is_refused(monkeypatch) -> None:
    mock_merge_group_apis(
        monkeypatch,
        [queue_entry(1, 11, HEAD_ELEVEN), queue_entry(2, 12, HEAD_TWELVE)],
    )

    with pytest.raises(AcceptanceError, match="disagrees with its queue ref"):
        _merge_group_numbers(
            merge_group_payload(number=13),
            "dimastov-lab/ai-command-center",
            {"GITHUB_TOKEN": "x"},
        )


def test_batch_acceptance_checks_each_queue_bound_exact_head(
    monkeypatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(merge_group_payload()), encoding="utf-8")
    compare_response = comparison(
        [
            synthetic(SYNTHETIC_ONE, BASE, 11),
            synthetic(SYNTHETIC_TWO, SYNTHETIC_ONE, 12),
        ]
    )

    def api(path: str, _env: dict[str, str]) -> object:
        if "/compare/" in path:
            return compare_response
        if path.endswith("/pulls/11"):
            return {
                "number": 11,
                "state": "open",
                "base": {"ref": "main"},
                "head": {"sha": HEAD_ELEVEN},
                "user": {"login": AUTHOR},
            }
        if path.endswith("/pulls/12"):
            return {
                "number": 12,
                "state": "open",
                "base": {"ref": "main"},
                "head": {"sha": HEAD_TWELVE},
                "user": {"login": AUTHOR},
            }
        if "/pulls/11/reviews" in path:
            return [review(f"ACCEPTANCE: ACCEPT {HEAD_ELEVEN}")]
        if "/pulls/12/reviews" in path:
            return [review(f"ACCEPTANCE: ACCEPT {HEAD_TWELVE}")]
        raise AssertionError(path)

    monkeypatch.setattr("scripts.assert_independent_acceptance._api", api)
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._graphql",
        lambda _query, _variables, _env: queue_response(
            [queue_entry(1, 11, HEAD_ELEVEN), queue_entry(2, 12, HEAD_TWELVE)]
        ),
    )

    evidence = assert_accepted(
        {
            "GITHUB_REPOSITORY": "dimastov-lab/ai-command-center",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_EVENT_NAME": "merge_group",
            "GITHUB_TOKEN": "x",
        }
    )

    assert evidence == f"#11:{REVIEWER}, #12:{REVIEWER}"


def test_a_pr_moving_after_the_group_was_built_is_refused(
    monkeypatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(merge_group_payload()), encoding="utf-8")
    compare_response = comparison(
        [
            synthetic(SYNTHETIC_ONE, BASE, 11),
            synthetic(SYNTHETIC_TWO, SYNTHETIC_ONE, 12),
        ]
    )

    def api(path: str, _env: dict[str, str]) -> object:
        if "/compare/" in path:
            return compare_response
        if path.endswith("/pulls/11"):
            return {
                "number": 11,
                "state": "open",
                "base": {"ref": "main"},
                "head": {"sha": OTHER},
                "user": {"login": AUTHOR},
            }
        raise AssertionError(path)

    monkeypatch.setattr("scripts.assert_independent_acceptance._api", api)
    monkeypatch.setattr(
        "scripts.assert_independent_acceptance._graphql",
        lambda _query, _variables, _env: queue_response(
            [queue_entry(1, 11, HEAD_ELEVEN), queue_entry(2, 12, HEAD_TWELVE)]
        ),
    )

    with pytest.raises(AcceptanceError, match="moved after the group was built"):
        assert_accepted(
            {
                "GITHUB_REPOSITORY": "dimastov-lab/ai-command-center",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "merge_group",
                "GITHUB_TOKEN": "x",
            }
        )
