"""review_once / merge_once (BO-S3b 2/3, 3/3) on live PostgreSQL: the store
side is real (READY_TO_REVIEW tasks with pr evidence), gh is faked in-process
by patching the module's _gh, and enqueue is a recording stub.

The identity gate at the end of the file takes no `rig` fixture on purpose.
Whether the merge tick will merge is decided before it reaches the store, and
those cases are the ones that must not stop running on a machine with no
PostgreSQL — a regression suite that skips is a regression suite that consents.
"""

from __future__ import annotations

import json

import pytest

from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401 — pytest fixtures
from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import (
    merge_once, review_once,
)



def _ready(store, factory, task_id, pr):
    """A task in READY_TO_REVIEW with a pr evidence row — the state part 1
    leaves behind."""
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task(task_id, repo="repo-x", status="OPEN"))[0]
    with factory() as c, c.cursor() as cur:
        # walk OPEN -> IN_PROGRESS -> READY_TO_REVIEW via the real machine;
        # transition's third arg is the bigint revision, re-read each step.
        def _rev():
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev()))
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task_id, _rev()))
        c.commit()


def test_review_enqueues_one_run_per_ready_task(rig):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R1", "https://github.com/x/y/pull/7")
    calls = []
    report = review_once(app_factory, lambda q, k, p: calls.append((q, k, p)))
    assert ("VOYN-W0-R1", "https://github.com/x/y/pull/7") in report.reviewed
    assert len(calls) == 1
    q, key, payload = calls[0]
    assert key == "review:VOYN-W0-R1"  # idempotency key
    assert payload["task_type"] == "review" and "pull/7" in payload["prompt"]


def test_merge_requires_accept_marker_and_green_checks(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1", "https://github.com/x/y/pull/8")
    head = "a" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "author": {"login": "pr-author"},
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}",
                             "user": {"login": "independent-reviewer"},
                             "state": "COMMENTED"}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "merge-operator\n", "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1", head) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1",))
        assert cur.fetchone()[0] == "DONE"


def test_merge_skips_without_marker(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M2", "https://github.com/x/y/pull/9")

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "merge-operator\n", "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": "b" * 40,
            "author": {"login": "pr-author"}, "reviews": [],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(t == "VOYN-W0-M2" and "no acceptance verdict" in reason
               for t, reason in report.skipped)
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M2",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_skips_when_a_check_is_red(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M3", "https://github.com/x/y/pull/10")
    head = "c" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "merge-operator\n", "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "pr-author"},
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}",
                         "user": {"login": "independent-reviewer"},
                         "state": "COMMENTED"}],
            "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(t == "VOYN-W0-M3" and "checks_not_green" in r for t, r in report.skipped)


# -- the merge loop's identity gate -------------------------------------------
#
# These need no database: they exercise `_pr_is_mergeable`, the decision the
# merge tick makes before it touches the store at all. Each names a route by
# which a pull request could once have merged on evidence the merging account
# produced, or on a marker that never bound anyone, and asserts the tick
# refuses. `HEAD` is a real 40-hex sha because the gateway rejects anything
# else outright, which would make every case below pass for the wrong reason.

HEAD = "d4e5f6a7b8c90112233445566778899aabbccdde"  # pragma: allowlist secret
MERGER = "merge-operator"
AUTHOR = "pr-author"
REVIEWER = "independent-reviewer"


def _mergeable(
    monkeypatch,
    *,
    reviews,
    merger=MERGER,
    merger_rc=0,
    checks=({"name": "CI", "conclusion": "SUCCESS"},),
):
    """Run `_pr_is_mergeable` against a faked `gh`. `reviews` is passed through
    verbatim so a case can shape the review record it is actually about."""

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, merger_rc, merger, "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": HEAD,
            "author": {"login": AUTHOR},
            "reviews": list(reviews),
            "statusCheckRollup": list(checks),
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    return review_merge._pr_is_mergeable("/tmp", "https://github.com/x/y/pull/11")


def _review(body, login=REVIEWER, state="COMMENTED"):
    """A review as `gh pr view --json reviews` renders one: the identity is
    `author`, not the REST API's `user`."""
    return {"body": body, "author": {"login": login}, "state": state}


def test_an_independent_verdict_on_the_head_still_merges(monkeypatch):
    """The accepting case, so every refusal below is a refusal of something."""
    ready, detail = _mergeable(
        monkeypatch, reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}")]
    )
    assert ready
    assert detail == HEAD


def test_the_merging_account_cannot_accept_its_own_pull_request(monkeypatch):
    """The gap this closes: the loop merged on a marker it had published."""
    ready, reason = _mergeable(
        monkeypatch, reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}", login=MERGER)]
    )
    assert not ready
    assert "who would merge this" in reason


def test_the_merging_account_is_matched_case_insensitively(monkeypatch):
    """GitHub logins are case-insensitive, so `Merge-Operator` is the merger."""
    ready, reason = _mergeable(
        monkeypatch,
        reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}", login="Merge-Operator")],
    )
    assert not ready
    assert "who would merge this" in reason


def test_the_pull_requests_author_cannot_accept_it(monkeypatch):
    ready, reason = _mergeable(
        monkeypatch, reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}", login=AUTHOR)]
    )
    assert not ready
    assert "who authored this" in reason


def test_a_dismissed_acceptance_no_longer_merges(monkeypatch):
    ready, reason = _mergeable(
        monkeypatch,
        reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}", state="DISMISSED")],
    )
    assert not ready
    assert "was dismissed" in reason


@pytest.mark.parametrize("state", ["PENDING", "", "SOMETHING_NEW"])
def test_only_a_submitted_review_state_can_accept(monkeypatch, state):
    """An unsubmitted draft is not a verdict, and an unrecognised state is not
    evidence of one — the allowlist fails closed on both."""
    ready, reason = _mergeable(
        monkeypatch, reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}", state=state)]
    )
    assert not ready
    assert "no acceptance verdict" in reason


def test_a_marker_below_the_first_line_is_prose_not_a_verdict(monkeypatch):
    """The old check accepted the marker text anywhere in any review body, so
    a reviewer quoting the line they were asked to publish merged the PR."""
    body = f"Looks fine to me. I would say:\n\nACCEPTANCE: ACCEPT {HEAD}\n"
    ready, reason = _mergeable(monkeypatch, reviews=[_review(body)])
    assert not ready
    assert "no acceptance verdict" in reason


def test_a_verdict_for_another_commit_does_not_carry_to_this_head(monkeypatch):
    other = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    ready, reason = _mergeable(
        monkeypatch, reviews=[_review(f"ACCEPTANCE: ACCEPT {other}")]
    )
    assert not ready
    assert "no verdict names the current head" in reason


def test_a_rejection_on_the_head_blocks_even_beside_an_acceptance(monkeypatch):
    ready, reason = _mergeable(monkeypatch, reviews=[
        _review(f"ACCEPTANCE: ACCEPT {HEAD}"),
        _review(f"ACCEPTANCE: REJECT {HEAD}", login="second-reviewer"),
    ])
    assert not ready
    assert "REJECTED" in reason


def test_an_unattributable_verdict_cannot_prove_independence(monkeypatch):
    ready, reason = _mergeable(
        monkeypatch, reviews=[{"body": f"ACCEPTANCE: ACCEPT {HEAD}", "state": "COMMENTED"}]
    )
    assert not ready
    assert "no acceptance verdict" in reason


def test_an_unreadable_merger_identity_refuses_rather_than_merges(monkeypatch):
    """Independence from an account the tick cannot name is unprovable, and an
    unprovable premise is a refusal, not a pass."""
    for merger, rc in (("", 0), ("merge-operator\n", 1)):
        ready, reason = _mergeable(
            monkeypatch,
            reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}")],
            merger=merger,
            merger_rc=rc,
        )
        assert not ready
        assert reason == "merger_identity_unavailable"


def test_a_red_job_merely_named_for_acceptance_still_blocks(monkeypatch):
    """The loop disregards `acceptance-gate.yml` because it re-derives that
    verdict itself. It must not also disregard a failing test job whose name
    happens to contain the word."""
    ready, reason = _mergeable(
        monkeypatch,
        reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}")],
        checks=[{"name": "pytest / acceptance-criteria", "conclusion": "FAILURE"}],
    )
    assert not ready
    assert "checks_not_green" in reason


def test_the_acceptance_gates_own_check_does_not_block(monkeypatch):
    """It is red for the whole window before the verdict lands, and it re-runs
    only on a review event — waiting for it would deadlock the tick."""
    ready, detail = _mergeable(
        monkeypatch,
        reviews=[_review(f"ACCEPTANCE: ACCEPT {HEAD}")],
        checks=[{"name": "Acceptance gate (independent verdict on exact SHA)",
                 "conclusion": "FAILURE"}],
    )
    assert ready
    assert detail == HEAD
