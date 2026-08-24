# ruff: noqa: F401, F811
"""review_once / merge_once (BO-S3b 2/3, 3/3) on live PostgreSQL: the store
side is real (READY_TO_REVIEW tasks with pr evidence), gh is faked in-process
by patching the module's _gh, and enqueue is a recording stub."""

from __future__ import annotations

import json

import pytest

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import (
    merge_once,
    publish_review_verdicts,
    review_once,
)
from tests.db.test_backlog_planner import (
    _test_repo_routes,
    rig,
)


def _complete_review(app_factory, worker, task_id, pr_url, head_sha, result_text):
    """Enqueue + claim + complete a review-class work item exactly the way
    review_once/the real daemon would, so publish_review_verdicts reads a
    result shaped like production, not a hand-built row. Keyed via the real
    `_review_key` (task + PR number + head sha + policy version) -- the
    caller's `head_sha` must match whatever the test's faked `gh pr view`
    reports as `headRefOid`, the same way review_once/publish_review_
    verdicts always compute the key from the PR's live head, never a value
    handed in separately."""
    from command_center.db.work_queue_store import WorkQueueStore

    store = WorkQueueStore(app_factory)
    payload = {
        "kind": "agent_run",
        "v": 1,
        "project_id": task_id,
        "repository_path": "",
        "task_type": "review",
        "prompt": "review it",
        "timeout_seconds": 900,
        "untrusted": False,
    }
    key = review_merge._review_key(task_id, pr_url, head_sha)
    store.enqueue("execution", idempotency_key=key, payload=payload, task_id=task_id)
    claimed = worker.claim("execution", visibility_seconds=60)
    assert worker.complete(claimed, {"status": "completed", "result_text": result_text})


def _ready(store, factory, task_id, pr):
    """A task in READY_TO_REVIEW with a pr evidence row — the state part 1
    leaves behind."""
    from tests.db.test_backlog_planner import _task

    assert store.upsert_task(_task(task_id, repo="repo-x", status="OPEN"))[0]
    with factory() as c, c.cursor() as cur:
        # walk OPEN -> IN_PROGRESS -> READY_TO_REVIEW via the real machine;
        # transition's third arg is the bigint revision, re-read each step.
        def _rev():
            cur.execute(
                "SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,)
            )
            return cur.fetchone()[0]

        cur.execute(
            "SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev())
        )
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute(
            "SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)",
            (task_id, _rev()),
        )
        c.commit()


def test_review_enqueues_one_run_per_ready_task(rig, _test_repo_routes, monkeypatch):

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R1", "https://github.com/x/repo-d2/pull/7")
    head = "d" * 40

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": head}), ""
            )
        if argv[:2] == ["pr", "diff"]:
            return subprocess.CompletedProcess(argv, 0, "diff --git a/x b/x\n+hi\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    calls = []
    report = review_once(
        app_factory,
        lambda q, k, p, tid, attempts: calls.append((q, k, p, tid, attempts)),
        "/tmp",
    )
    assert ("VOYN-W0-R1", "https://github.com/x/repo-d2/pull/7") in report.reviewed
    assert len(calls) == 1
    _q, key, payload, task_id, max_attempts = calls[0]
    # task + PR number + exact head sha + review policy version -- not just
    # the task_id -- so a later push to the same PR (remediation, or an
    # ordinary second push while still IN_PROGRESS) gets its own fresh
    # review instead of being permanently deduped against this one.
    assert key == f"review:VOYN-W0-R1:7:{head}:v2"
    assert task_id == "VOYN-W0-R1"
    assert [link["executor"] for link in payload["cascade"]] == ["copilot", "claude"]
    assert max_attempts == len(payload["cascade"]) == 2
    assert payload["task_type"] == "review" and "pull/7" in payload["prompt"]
    # The diff is embedded in the prompt directly -- the orchestrator fetches
    # it, not the review agent -- so the agent needs no Bash/gh access of its
    # own at all. Independent review (2026-08-21) found that granting a
    # review agent even a narrowly-scoped `gh pr view/diff` Bash pattern let
    # a prompt-injected instruction inside the diff pass an unconstrained
    # `--repo` argument and read unrelated private repos, no shell escape
    # needed; embedding is the fix that removes the capability instead of
    # trying to scope it.
    assert "diff --git a/x b/x" in payload["prompt"]
    assert head in payload["prompt"]
    # Resolved through the same repo_route() table implementation dispatch
    # uses, not the raw backlog task_id and an empty path -- the worker's
    # validate_repository rejects both (VOYN-W0-AICC-MISSING-MARKER-
    # PUBLISHER's review-dispatch half, found live 2026-08-21: every review
    # this function ever enqueued had dead-lettered on first attempt).
    assert payload["project_id"] == "AICC"
    assert payload["repository_path"] == "/srv/repo-d2"


def test_review_skips_a_pr_whose_repo_has_no_route(rig):
    app_factory, store, _ = rig
    _ready(
        store, app_factory, "VOYN-W0-R2", "https://github.com/x/unrouted-repo/pull/9"
    )
    calls = []
    report = review_once(
        app_factory,
        lambda q, k, p, tid, attempts: calls.append((q, k, p, tid, attempts)),
        "/tmp",
    )
    assert not calls
    assert any(
        task_id == "VOYN-W0-R2" and reason.startswith("no_repo_route")
        for task_id, reason in report.skipped
    )


def test_review_skips_when_the_diff_fetch_fails(rig, _test_repo_routes, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R3", "https://github.com/x/repo-d2/pull/12")

    def fake_gh(argv, repo):
        import subprocess

        return subprocess.CompletedProcess(argv, 1, "", "not found")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    calls = []
    report = review_once(
        app_factory,
        lambda q, k, p, tid, attempts: calls.append((q, k, p, tid, attempts)),
        "/tmp",
    )
    assert not calls
    assert any(
        task_id == "VOYN-W0-R3" and reason.startswith("pr_diff_fetch_failed")
        for task_id, reason in report.skipped
    )


def test_review_skips_a_diff_over_the_size_cap(rig, _test_repo_routes, monkeypatch):
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R4", "https://github.com/x/repo-d2/pull/13")
    head = "e" * 40
    huge_diff = "x" * (review_merge._MAX_DIFF_CHARS + 1)

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": head}), ""
            )
        if argv[:2] == ["pr", "diff"]:
            return subprocess.CompletedProcess(argv, 0, huge_diff, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    calls = []
    report = review_once(
        app_factory,
        lambda q, k, p, tid, attempts: calls.append((q, k, p, tid, attempts)),
        "/tmp",
    )
    assert not calls
    assert any(
        task_id == "VOYN-W0-R4" and reason.startswith("diff_too_large")
        for task_id, reason in report.skipped
    )


def test_merge_requires_accept_marker_and_green_checks(rig, monkeypatch):

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1", "https://github.com/x/y/pull/8")
    head = "a" * 40
    merge_sha = "b" * 40

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps(
                {
                    "state": "OPEN",
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                }
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge, "_merged_commit_sha", lambda _repo, _pr: merge_sha
    )
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1", merge_sha) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"
        cur.execute(
            "SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='ci'",
            ("VOYN-W0-M1",),
        )
        assert (f"MERGED:{merge_sha}",) in cur.fetchall()


def test_merge_skips_a_self_issued_marker_from_the_pr_author(rig, monkeypatch):
    """VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE: a marker whose reviewer
    login is the SAME as the PR's own author must not authorize merge --
    live-confirmed as a real gap on PRs #354/#355, both merged by the
    account that had posted their own marker."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1B", "https://github.com/x/y/pull/21")
    head = "f" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "author": {"login": "dimastov-lab"},
                    }
                ],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1B", "no_accept_marker_on_head") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1B",)
        )
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merge_accepts_a_marker_from_a_reviewer_login_distinct_from_the_author(
    rig, monkeypatch
):
    """The positive case of the same check: a genuinely independent
    reviewer login (the acceptance bot's, in production) does authorize
    merge."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1C", "https://github.com/x/y/pull/22")
    head = "1" * 40
    merge_sha = "3" * 40

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps(
                {
                    "state": "OPEN",
                    "headRefOid": head,
                    "author": {"login": "dimastov-lab"},
                    "reviews": [
                        {
                            "body": f"ACCEPTANCE: ACCEPT {head}",
                            "author": {"login": "voyn88-acceptance-gate[bot]"},
                        }
                    ],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                }
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge, "_merged_commit_sha", lambda _repo, _pr: merge_sha
    )
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1C", merge_sha) in report.merged


def test_merge_now_requires_the_acceptance_check_itself_green(rig, monkeypatch):
    """The `"cceptance" not in name` exclusion is gone: a red Acceptance
    gate check blocks merge like any other required check, now that the
    bot behind it is reconnected and the check can genuinely reflect an
    independent verdict rather than being permanently, structurally red."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1D", "https://github.com/x/y/pull/23")
    head = "2" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "author": {"login": "voyn88-acceptance-gate[bot]"},
                    }
                ],
                "statusCheckRollup": [
                    {"name": "CI", "conclusion": "SUCCESS"},
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": "FAILURE",
                    },
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(
        task_id == "VOYN-W0-M1D" and reason.startswith("checks_not_green")
        for task_id, reason in report.skipped
    )


def test_merge_skips_without_marker(rig, monkeypatch):

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M2", "https://github.com/x/y/pull/9")

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": "b" * 40,
                "reviews": [],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M2", "no_accept_marker_on_head") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M2",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_skips_a_still_running_check_instead_of_waving_it_through(
    rig, monkeypatch
):
    """VOYN-W0-AICC-DISABLE-UNSAFE-AUTOMERGE: a CheckRun with `conclusion:
    null` because it hasn't finished (`status` QUEUED/IN_PROGRESS) used to
    read identically to one that simply carries no conclusion key at all --
    both passed. A required check still running must block merge, not be
    treated as passing."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M3", "https://github.com/x/y/pull/20")
    head = "c" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "submittedAt": "2026-01-01T00:00:00Z",
                    }
                ],
                "statusCheckRollup": [
                    {"name": "CI", "status": "IN_PROGRESS", "conclusion": None},
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M3", "checks_not_green: ['CI']") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M3",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merge_skips_a_pending_legacy_status_context_too(rig, monkeypatch):
    """The rollup mixes CheckRun and legacy StatusContext shapes; a pending
    StatusContext (`state: PENDING`, no `conclusion`/`status` keys at all)
    must block merge the same as a running CheckRun does."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M4", "https://github.com/x/y/pull/21")
    head = "f" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "submittedAt": "2026-01-01T00:00:00Z",
                    }
                ],
                "statusCheckRollup": [{"name": "legacy-ci", "state": "PENDING"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M4", "checks_not_green: ['legacy-ci']") in report.skipped


def test_merge_only_the_most_recent_review_can_carry_the_marker(rig, monkeypatch):
    """VOYN-W0-AICC-DISABLE-UNSAFE-AUTOMERGE: an ACCEPT marker sitting in an
    OLDER review must not authorize merge once a NEWER review exists on the
    same head -- e.g. a stale ACCEPT from before a dismissed/superseded
    review. Only the latest review by submittedAt counts."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M5", "https://github.com/x/y/pull/22")
    head = "9" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "reviews": [
                    {
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "submittedAt": "2026-01-01T00:00:00Z",
                    },
                    {
                        "body": "Actually, hold on -- this needs another look.",
                        "submittedAt": "2026-01-02T00:00:00Z",
                    },
                ],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M5", "no_accept_marker_on_head") in report.skipped


def test_publish_verdict_posts_the_marker_under_the_acceptance_bot_identity(
    rig, monkeypatch
):
    """VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE: the agent's own ACCEPT
    verdict must reach GitHub as the exact `ACCEPTANCE: ACCEPT <sha>`
    comment-review body, posted under the independent acceptance bot's
    identity -- not the same ambient `gh` credential that authored and will
    merge the PR (the self-issued marker VOYN-W0-AICC-MISSING-MARKER-
    PUBLISHER's own test used to accept, live-confirmed on PRs #354/#355 as
    a real self-approval bypass)."""
    app_factory, store, worker = rig
    head = "d" * 40
    pr_url = "https://github.com/x/y/pull/11"
    _ready(store, app_factory, "VOYN-W0-P1", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P1",
        pr_url,
        head,
        f"Reviewed the diff, found nothing wrong.\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
    )

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps({"headRefOid": head, "reviews": []})
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge,
        "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials(
            "1", "2", "/dev/null"
        ),
    )
    posted = []

    def fake_post(creds, pr_url_arg, decision, sha):
        posted.append((pr_url_arg, decision, sha))
        return True, ""

    monkeypatch.setattr(review_merge, "_post_marker_as_bot", fake_post)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P1", "https://github.com/x/y/pull/11") in report.reviewed
    assert posted == [("https://github.com/x/y/pull/11", "ACCEPT", head)]


def test_publish_verdict_skips_without_the_acceptance_bot_configured(rig, monkeypatch):
    """A host with no acceptance-bot credentials must not fall back to the
    old same-identity marker -- that marker can never satisfy
    `_pr_is_mergeable`'s different-author check any more, so posting one
    would just be silent, ineffective noise. Skip loudly instead."""
    app_factory, store, worker = rig
    head = "e" * 40
    pr_url = "https://github.com/x/y/pull/12"
    _ready(store, app_factory, "VOYN-W0-P1B", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P1B",
        pr_url,
        head,
        f"Looks fine.\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
    )

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps({"headRefOid": head, "reviews": []})
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: None)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert not report.reviewed
    assert ("VOYN-W0-P1B", "acceptance_bot_not_configured") in report.skipped


def test_publish_verdict_reject_dispatches_a_linked_remediation_task(rig, monkeypatch):
    """VOYN-W0-AICC-REVIEW-REJECT-REMEDIATION-LOOP: a REJECT must not just be
    skipped forever -- it dispatches a new, linked follow-up task (0010's
    design: a new task, not a cycle back into the rejected task's own state
    machine) so the loop actually closes end to end instead of dead-ending
    the moment a real review agent finds a real defect (which is exactly
    what happened live 2026-08-21, the first time this pipeline ever
    reviewed a real diff: both real reviews it produced correctly
    REJECTED)."""
    import subprocess as sp

    app_factory, store, worker = rig
    head = "e" * 40
    pr_url = "https://github.com/x/y/pull/12"
    _ready(store, app_factory, "VOYN-W0-P2", pr_url)
    feedback = "Found a real defect: the retry loop never terminates."
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P2",
        pr_url,
        head,
        f"{feedback}\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": head, "reviews": []}), ""
            )
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P2", "VOYN-W0-P2-REM") in report.remediated
    assert not any(a[:2] == ["pr", "review"] for a in posted)

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-P2",))
        assert cur.fetchone()[0] == "REJECTED"

        cur.execute(
            "SELECT status, title, body FROM backlog_task WHERE task_id=%s",
            ("VOYN-W0-P2-REM",),
        )
        new_status, new_title, new_body = cur.fetchone()
        assert new_status == "OPEN"
        assert "Remediation" in new_title
        assert feedback in new_body
        assert pr_url in new_body
        assert head in new_body

        cur.execute(
            "SELECT parent_task_id, pr_url, rejected_head_sha "
            "FROM backlog_task_remediation WHERE task_id=%s",
            ("VOYN-W0-P2-REM",),
        )
        parent, linked_pr, linked_sha = cur.fetchone()
        assert parent == "VOYN-W0-P2"
        assert linked_pr == pr_url
        assert linked_sha == head


def test_publish_verdict_reject_remediation_is_idempotent(rig, monkeypatch):
    """The parent task leaves READY_TO_REVIEW the instant it is REJECTED, so
    a second publish_review_verdicts tick never even selects it again -- the
    state machine itself is the idempotency boundary, not a special skip
    reason. What must still hold is the DB-level guard inside
    `_remediate_rejection`: called twice directly for the same parent (the
    only way to reach it a second time -- a race between two ticks that both
    read READY_TO_REVIEW before either commits), it must create exactly one
    remediation task, not two."""
    import subprocess as sp

    app_factory, store, worker = rig
    head = "e" * 40
    pr_url = "https://github.com/x/y/pull/12"
    _ready(store, app_factory, "VOYN-W0-P2B", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P2B",
        pr_url,
        head,
        f"A defect.\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": head, "reviews": []}), ""
            )
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    first = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P2B", "VOYN-W0-P2B-REM") in first.remediated

    # The task is now REJECTED -- a second full tick finds nothing to do.
    second = publish_review_verdicts(app_factory, "/tmp")
    assert not second.remediated
    assert not second.skipped

    # Direct second call to the guarded function itself, simulating a race:
    # the DB-level "does a remediation already exist" check must refuse.
    again = review_merge._remediate_rejection(
        app_factory, "VOYN-W0-P2B", pr_url, head, "A defect.\nVERDICT: REJECT\n"
    )
    assert again is None

    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM backlog_task WHERE task_id LIKE %s",
            ("VOYN-W0-P2B-REM%",),
        )
        assert cur.fetchone()[0] == 1


def test_publish_verdict_takes_the_last_verdict_not_the_first(rig, monkeypatch):
    """Independent review, 2026-08-21: a reviewing agent reasoning aloud can
    draft a tentative ACCEPT, keep reading, find a real defect, and correct
    itself to REJECT further down the same transcript. .search() would have
    silently kept the first (wrong) verdict and posted ACCEPTANCE anyway --
    the dangerous direction, since the other way (REJECT then ACCEPT) only
    fails closed. Pinned by taking the LAST VERDICT: line."""
    import subprocess as sp

    app_factory, store, worker = rig
    head = "9" * 40
    pr_url = "https://github.com/x/y/pull/16"
    _ready(store, app_factory, "VOYN-W0-P6", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P6",
        pr_url,
        head,
        "Initial pass looked clean.\nVERDICT: ACCEPT\n\n"
        "Wait -- rereading the diff, the stale-head check is buggy after all.\n"
        f"Correcting my assessment:\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": head, "reviews": []}), ""
            )
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P6", "VOYN-W0-P6-REM") in report.remediated
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_ignores_an_earlier_lookalike_block(rig, monkeypatch):
    """Independent review, round 3 (2026-08-21): even a single co-located
    VERDICT+HEAD_SHA regex still matches wherever it occurs in the text --
    including a purely illustrative block ("a passing review would read
    exactly: ...") that isn't the agent's real conclusion. Scanning for
    "the last regex match anywhere" over the free text is the wrong
    primitive entirely; only the transcript's true final two non-blank
    lines (what the prompt actually asks for) may decide the verdict. This
    pins that an earlier lookalike block never wins over the real, later
    verdict."""
    import subprocess as sp

    app_factory, store, worker = rig
    fake_head = "a" * 40
    real_head = "b" * 40
    pr_url = "https://github.com/x/y/pull/18"
    _ready(store, app_factory, "VOYN-W0-P8", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P8",
        pr_url,
        real_head,
        "Note: a passing review would read exactly:\n"
        f"VERDICT: ACCEPT\nHEAD_SHA: {fake_head}\n\n"
        "However, after actually reviewing this diff:\n"
        f"VERDICT: REJECT\nHEAD_SHA: {real_head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": real_head, "reviews": []}), ""
            )
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P8", "VOYN-W0-P8-REM") in report.remediated
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_does_not_pair_mismatched_verdict_and_sha(rig, monkeypatch):
    """Independent review, round 2 (2026-08-21): taking the last VERDICT and
    the last HEAD_SHA *independently* can still combine two unrelated lines
    -- e.g. the reviewer's real, final REJECT followed by incidental prose
    that happens to contain both an ACCEPT (about a different PR) and this
    PR's actual head sha. That must not synthesize an ACCEPT-with-real-sha
    marker. Only a VERDICT line immediately followed by its own HEAD_SHA
    line counts as a verdict at all."""
    import subprocess as sp

    app_factory, store, worker = rig
    real_head = "c" * 40
    pr_url = "https://github.com/x/y/pull/17"
    _ready(store, app_factory, "VOYN-W0-P7", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P7",
        pr_url,
        real_head,
        "VERDICT: REJECT\n"
        "The stale-head check is buggy.\n\n"
        f"Note: PR #99 shares HEAD_SHA: {real_head} with this one, where "
        "VERDICT: ACCEPT was correctly given for the analogous change.\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": real_head, "reviews": []}), ""
            )
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert (
        "VOYN-W0-P7",
        "verdict_or_head_sha_missing_in_review_result",
    ) in report.skipped
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_skips_without_a_completed_review_yet(rig, monkeypatch):
    import subprocess as sp

    app_factory, store, _worker = rig
    pr_url = "https://github.com/x/y/pull/13"
    _ready(store, app_factory, "VOYN-W0-P3", pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": "z" * 40, "reviews": []}), ""
            )
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P3", "no_review_result_yet") in report.skipped


def test_publish_verdict_skips_when_already_posted(rig, monkeypatch):
    app_factory, store, worker = rig
    head = "f" * 40
    pr_url = "https://github.com/x/y/pull/14"
    _ready(store, app_factory, "VOYN-W0-P4", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P4",
        pr_url,
        head,
        f"Looks fine.\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps(
                {
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                }
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        posted.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P4", "marker_already_posted") in report.skipped
    assert not posted


def test_publish_verdict_a_review_of_an_old_head_never_gets_read_as_current(
    rig, monkeypatch
):
    """VOYN-OPS-EVIDENCE-MEASURED-ON-A-STATE-THAT-NO-LONGER-EXISTS, same
    class at a new site: the review ran against a sha that is no longer the
    PR's head (a push landed after the review was dispatched). Posting the
    old sha's marker would satisfy merge_once's string match against a
    branch state nobody re-reviewed. Was a dedicated post-hoc "stale_review"
    check before the review-cycle key existed; now it is structurally
    impossible instead of separately guarded -- the lookup itself is keyed
    to the CURRENT head, so a review recorded for a different (old) sha is
    simply not found at all, indistinguishable from "never reviewed" (which
    is the correct, safe reading: from the current head's perspective, it
    hasn't been)."""
    app_factory, store, worker = rig
    reviewed_sha = "1" * 40
    new_head = "2" * 40
    pr_url = "https://github.com/x/y/pull/15"
    _ready(store, app_factory, "VOYN-W0-P5", pr_url)
    _complete_review(
        app_factory,
        worker,
        "VOYN-W0-P5",
        pr_url,
        reviewed_sha,
        f"Looks fine.\nVERDICT: ACCEPT\nHEAD_SHA: {reviewed_sha}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        import subprocess

        if argv[:2] == ["pr", "view"]:
            body = json.dumps({"headRefOid": new_head, "reviews": []})
            return subprocess.CompletedProcess(argv, 0, body, "")
        posted.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P5", "no_review_result_yet") in report.skipped
    assert not posted


def test_merge_skips_when_a_check_is_red(rig, monkeypatch):

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M3", "https://github.com/x/y/pull/10")
    head = "c" * 40

    def fake_gh(argv, repo):
        import subprocess

        body = json.dumps(
            {
                "state": "OPEN",
                "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(t == "VOYN-W0-M3" and "checks_not_green" in r for t, r in report.skipped)


def test_mergeability_uses_latest_check_rerun(monkeypatch):
    import subprocess

    head = "d" * 40

    def fake_gh(argv, repo):
        body = json.dumps(
            {
                "state": "OPEN",
                "author": {"login": "dimastov-lab"},
                "headRefOid": head,
                "reviews": [{"author": {"login": "voyn88-acceptance-gate[bot]"}, "submittedAt": "2026-08-23T06:00:00Z", "body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": "FAILURE",
                        "startedAt": "2026-08-23T04:29:29Z",
                    },
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-08-23T05:25:43Z",
                    },
                    {
                        "name": "Final merge gate",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-08-23T04:29:27Z",
                    },
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    assert review_merge._pr_is_mergeable("/tmp", "https://github.com/x/y/pull/10") == (
        True,
        f"ready_to_enqueue:{head}",
    )


def test_mergeability_rejects_latest_failed_check_rerun(monkeypatch):
    import subprocess

    head = "e" * 40

    def fake_gh(argv, repo):
        body = json.dumps(
            {
                "state": "OPEN",
                "author": {"login": "dimastov-lab"},
                "headRefOid": head,
                "reviews": [{"author": {"login": "voyn88-acceptance-gate[bot]"}, "submittedAt": "2026-08-23T06:00:00Z", "body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [
                    {
                        "name": "Final merge gate",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-08-23T04:00:00Z",
                    },
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-08-23T04:29:29Z",
                    },
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": "FAILURE",
                        "startedAt": "2026-08-23T05:25:43Z",
                    },
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, reason = review_merge._pr_is_mergeable(
        "/tmp", "https://github.com/x/y/pull/10"
    )
    assert not ready
    assert reason == "checks_not_green: ['Acceptance gate (independent verdict on exact SHA)']"


@pytest.mark.parametrize(
    ("newer_conclusion", "expected_ready"),
    [("SUCCESS", True), ("FAILURE", False)],
)
def test_mergeability_uses_timestamps_not_rollup_array_order(
    monkeypatch, newer_conclusion, expected_ready
):
    """GitHub does not promise chronological rollup order.  Put the newer
    run first so taking the last array element would produce the wrong result."""
    import subprocess

    head = "1" * 40
    older_conclusion = "FAILURE" if newer_conclusion == "SUCCESS" else "SUCCESS"

    def fake_gh(argv, repo):
        body = json.dumps(
            {
                "state": "OPEN",
                "author": {"login": "dimastov-lab"},
                "headRefOid": head,
                "reviews": [{"author": {"login": "voyn88-acceptance-gate[bot]"}, "submittedAt": "2026-08-23T06:00:00Z", "body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [
                    {
                        "name": "Final merge gate",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-08-23T04:00:00Z",
                    },
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": newer_conclusion,
                        "startedAt": "2026-08-23T05:25:43Z",
                    },
                    {
                        "name": "Acceptance gate (independent verdict on exact SHA)",
                        "conclusion": older_conclusion,
                        "startedAt": "2026-08-23T04:29:29Z",
                    },
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, _ = review_merge._pr_is_mergeable("/tmp", "https://github.com/x/y/pull/10")
    assert ready is expected_ready


@pytest.mark.parametrize(
    "checks",
    [
        [
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-23T05:25:43Z",
            },
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-23T05:25:43Z",
            },
        ],
        [
            {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "FAILURE"},
            {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS"},
        ],
    ],
)
def test_mergeability_fails_closed_when_rerun_order_is_ambiguous(monkeypatch, checks):
    import subprocess

    head = "f" * 40

    def fake_gh(argv, repo):
        body = json.dumps(
            {
                "state": "OPEN",
                "author": {"login": "dimastov-lab"},
                "headRefOid": head,
                "reviews": [{"author": {"login": "voyn88-acceptance-gate[bot]"}, "submittedAt": "2026-08-23T06:00:00Z", "body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": checks + [{"name": "Final merge gate", "conclusion": "SUCCESS", "startedAt": "2026-08-23T04:00:00Z"}],
            }
        )
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, reason = review_merge._pr_is_mergeable(
        "/tmp", "https://github.com/x/y/pull/10"
    )
    assert not ready
    assert reason == "checks_not_green: ['Acceptance gate (independent verdict on exact SHA)']"


def test_repo_from_pr_url():
    assert (
        review_merge._repo_from_pr_url("https://github.com/voyn88/aios/pull/273")
        == "aios"
    )
    assert (
        review_merge._repo_from_pr_url(
            "https://github.com/voyn88/ai-command-center/pull/1"
        )
        == "ai-command-center"
    )
    assert review_merge._repo_from_pr_url("not a url") is None
    assert review_merge._repo_from_pr_url("https://github.com/voyn88/aios") is None


def test_review_key_scopes_by_pr_number_head_sha_and_policy_version():
    base = "https://github.com/voyn88/aios/pull/273"
    sha_a = "a" * 40
    sha_b = "b" * 40
    key_a = review_merge._review_key("VOYN-W0-X", base, sha_a)
    key_b = review_merge._review_key("VOYN-W0-X", base, sha_b)
    assert key_a != key_b  # a new commit is a new review, automatically
    assert (
        key_a == f"review:VOYN-W0-X:273:{sha_a}:{review_merge._REVIEW_POLICY_VERSION}"
    )
    assert review_merge._review_key("VOYN-W0-X", "not a url", sha_a) is None


def test_review_once_gives_a_second_push_to_the_same_task_its_own_fresh_review(
    rig, _test_repo_routes, monkeypatch
):
    """The general case the review-cycle key fixes, not just remediation: an
    ordinary task still IN_PROGRESS/READY_TO_REVIEW that gets a second push
    to its PR must be reviewable again, not permanently deduped against
    whatever the first push's review concluded."""
    import subprocess as sp

    app_factory, store, _ = rig
    pr_url = "https://github.com/x/repo-d2/pull/20"
    _ready(store, app_factory, "VOYN-W0-R5", pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(
                argv, 0, json.dumps({"headRefOid": next(current_head)}), ""
            )
        if argv[:2] == ["pr", "diff"]:
            return sp.CompletedProcess(argv, 0, "diff --git a/x b/x\n+hi\n", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    calls = []

    def enqueue(q, k, p, tid, attempts):
        calls.append((q, k, p, tid, attempts))

    current_head = iter(["a" * 40])
    review_once(app_factory, enqueue, "/tmp")
    current_head = iter(["b" * 40])
    review_once(app_factory, enqueue, "/tmp")

    assert len(calls) == 2
    keys = {key for _q, key, _p, _tid, _attempts in calls}
    assert len(keys) == 2  # two distinct review-cycle identities, not deduped
    assert all(k.startswith("review:VOYN-W0-R5:20:") for k in keys)
