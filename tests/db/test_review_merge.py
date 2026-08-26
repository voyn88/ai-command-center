"""review_once / merge_once (BO-S3b 2/3, 3/3) on live PostgreSQL: the store
side is real (READY_TO_REVIEW tasks with pr evidence), gh is faked in-process
by patching the module's _gh, and enqueue is a recording stub."""
# ruff: noqa: RUF100

from __future__ import annotations

import json

import pytest

from command_center.orchestrator import review_merge
from command_center.orchestrator.review_merge import (
    merge_once,
    publish_review_verdicts,
    reconcile_merge_evidence,
    review_once,
)
from tests.db.test_backlog_planner import (  # noqa: F401 — pytest fixtures
    _test_repo_routes,
    rig,
)

BASE = "c" * 40
DIFF = "diff --git a/x b/x\n+hi\n"
SNAPSHOTS = {}
ORIGINAL_PR_SNAPSHOT = review_merge._pr_diff_and_head


def _snapshot(head, diff=DIFF):
    return review_merge._PRSnapshot.create(diff, BASE, head)


@pytest.fixture(autouse=True)
def _snapshots(monkeypatch):
    SNAPSHOTS.clear()
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda _repo, pr: SNAPSHOTS.get(pr))


def _complete_review(app_factory, worker, task_id, pr_url, head_sha, result_text):
    SNAPSHOTS[pr_url] = _snapshot(head_sha)
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
        "kind": "agent_run", "v": 1, "project_id": task_id,
        "repository_path": "", "task_type": "review",
        "prompt": "review it", "timeout_seconds": 900, "untrusted": False,
    }
    key = review_merge._review_key(task_id, pr_url, _snapshot(head_sha))
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
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev()))
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task_id, _rev()))
        c.commit()


def _done(store, factory, task_id, pr, sha):
    """A DONE task carrying pr + sha evidence, as merge_once leaves it --
    built directly against the store rather than through merge_once, so
    reconcile tests can plant a pre-fix sha value (a PR head, never a merge
    commit) that merge_once itself would no longer produce."""
    _ready(store, factory, task_id, pr)
    with factory() as c, c.cursor() as cur:
        def _rev():
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT backlog_record_evidence(%s,'sha',%s)", (task_id, sha))
        cur.execute("SELECT ok FROM backlog_transition(%s,'DONE',%s)", (task_id, _rev()))
        c.commit()


def test_review_enqueues_one_run_per_ready_task(rig, _test_repo_routes, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R1", "https://github.com/x/repo-d2/pull/7")
    head = "d" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[0] == "api" and "/pulls/7" in argv[1]:
            body = {"base": {"sha": BASE, "repo": {"full_name": "x/repo-d2"}},
                    "head": {"sha": head}, "changed_files": 1,
                    "additions": 1, "deletions": 0}
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        if argv[0] == "api":
            return subprocess.CompletedProcess(argv, 0, DIFF, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", ORIGINAL_PR_SNAPSHOT)
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
    assert key.startswith(
        f"review:VOYN-W0-R1:7:{head}:{review_merge._REVIEW_POLICY_VERSION}:base:{BASE}:diff:"
    )
    assert task_id == "VOYN-W0-R1"
    assert [link["executor"] for link in payload["cascade"]] == ["codex", "copilot", "claude"]
    assert max_attempts == len(payload["cascade"]) == 3
    assert payload["task_type"] == "independent_review"
    assert payload["untrusted"] is True
    assert "pull/7" in payload["prompt"]
    # The orchestrator embeds the diff in a collision-safe JSON string -- the
    # review agent still needs no Bash/gh access of its own. Independent review
    # (2026-08-21) found that granting a
    # review agent even a narrowly-scoped `gh pr view/diff` Bash pattern let
    # a prompt-injected instruction inside the diff pass an unconstrained
    # `--repo` argument and read unrelated private repos, no shell escape
    # needed; embedding is the fix that removes the capability instead of
    # trying to scope it.
    envelope = review_merge._review_envelope_from_prompt(payload["prompt"])
    assert envelope is not None
    assert envelope["content"]["text"] == "diff --git a/x b/x\n+hi\n"
    assert head in payload["prompt"]
    # Resolved through the same repo_route() table implementation dispatch
    # uses, not the raw backlog task_id and an empty path -- the worker's
    # validate_repository rejects both (VOYN-W0-AICC-MISSING-MARKER-
    # PUBLISHER's review-dispatch half, found live 2026-08-21: every review
    # this function ever enqueued had dead-lettered on first attempt).
    assert payload["project_id"] == "AICC"
    assert payload["repository_path"] == "/srv/repo-d2"


def test_review_skips_a_pr_whose_repo_has_no_route(rig):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R2", "https://github.com/x/unrouted-repo/pull/9")
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


def test_review_skips_when_the_diff_fetch_fails(rig, _test_repo_routes, monkeypatch):  # noqa: F811
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


def test_review_chunks_a_diff_over_the_single_prompt_cap(rig, _test_repo_routes, monkeypatch):  # noqa: F811, E501
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R4", "https://github.com/x/repo-d2/pull/13")
    head = "e" * 40
    huge_diff = "diff --git a/x b/x\n+" + "я" * 60_000

    def fake_gh(argv, repo):
        import subprocess
        if argv[0] == "api" and "/pulls/13" in argv[1]:
            body = {"base": {"sha": BASE, "repo": {"full_name": "x/repo-d2"}},
                    "head": {"sha": head}, "changed_files": 1,
                    "additions": 1, "deletions": 0}
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        if argv[0] == "api":
            return subprocess.CompletedProcess(argv, 0, huge_diff, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", ORIGINAL_PR_SNAPSHOT)
    calls = []
    report = review_once(
        app_factory,
        lambda q, k, p, tid, attempts: calls.append((q, k, p, tid, attempts)),
        "/tmp",
    )
    chunks = review_merge._review_chunks(
        _snapshot(head, huge_diff), "VOYN-W0-R4",
        "https://github.com/x/repo-d2/pull/13"
    )
    chunk_calls = [c for c in calls if "review_chunk" in c[2]]
    assert len(chunk_calls) == len(chunks) > 1
    # Chunks only: the eager full-context adjudication is retired
    # (VOYN-W0-AICC-REVIEW-AUTO-ACCEPT) -- verification is enqueued lazily
    # by publish_review_verdicts on REJECT, never here.
    assert len(calls) == len(chunks)
    assert all(len(call[2]["review_chunk"]["content_hash"]) == 64 for call in chunk_calls)
    assert all(
        len(call[2]["prompt"].encode("utf-8"))
        <= review_merge._MAX_REVIEW_PROMPT_BYTES
        for call in calls
    )
    assert ("VOYN-W0-R4", "https://github.com/x/repo-d2/pull/13") in report.reviewed


def test_merge_requires_accept_marker_and_green_checks(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1", "https://github.com/x/y/pull/8")
    head = "a" * 40

    merge_oid = "b" * 40
    merged_state = {"merged": False}

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            if merged_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            else:
                body = json.dumps({
                    "state": "OPEN", "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            merged_state["merged"] = True
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    # Evidence is the TARGET-BRANCH merge commit, never the PR head
    # (VOYN-W0-AICC-MERGE-DONE-BEFORE-TARGET-VERIFY).
    assert ("VOYN-W0-M1", merge_oid) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1",))
        assert cur.fetchone()[0] == "DONE"
        cur.execute("SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='sha'", ("VOYN-W0-M1",))
        assert cur.fetchone()[0] == merge_oid


def test_merge_skips_a_self_issued_marker_from_the_pr_author(rig, monkeypatch):  # noqa: F811
    """VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE: a marker whose reviewer
    login is the SAME as the PR's own author must not authorize merge --
    live-confirmed as a real gap on PRs #354/#355, both merged by the
    account that had posted their own marker."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1B", "https://github.com/x/y/pull/21")
    head = "f" * 40

    def fake_gh(argv, repo):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}", "author": {"login": "dimastov-lab"}}],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1B", "no_accept_marker_on_head") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1B",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merge_accepts_a_marker_from_a_reviewer_login_distinct_from_the_author(rig, monkeypatch):  # noqa: F811, E501
    """The positive case of the same check: a genuinely independent
    reviewer login (the acceptance bot's, in production) does authorize
    merge."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1C", "https://github.com/x/y/pull/22")
    head = "1" * 40

    merge_oid = "2" * 40
    merged_state = {"merged": False}

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            if merged_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": head,
                    "author": {"login": "dimastov-lab"},
                    "reviews": [{
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "author": {"login": "voyn88-acceptance-gate[bot]"},
                    }],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            else:
                body = json.dumps({
                    "state": "OPEN", "headRefOid": head,
                    "author": {"login": "dimastov-lab"},
                    "reviews": [{
                        "body": f"ACCEPTANCE: ACCEPT {head}",
                        "author": {"login": "voyn88-acceptance-gate[bot]"},
                    }],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            merged_state["merged"] = True
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1C", merge_oid) in report.merged


def test_merge_now_requires_the_acceptance_check_itself_green(rig, monkeypatch):  # noqa: F811
    """The `"cceptance" not in name` exclusion is gone: a red Acceptance
    gate check blocks merge like any other required check, now that the
    bot behind it is reconnected and the check can genuinely reflect an
    independent verdict rather than being permanently, structurally red.

    Since VOYN-W0-AICC-ACCEPTANCE-TAIL-LATENCY the block is active rather
    than passive: with a standing independent marker and the gate as the
    only red check, the merge tick re-drives the stale gate run instead of
    skipping for hours -- but it still NEVER merges around the red check."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1D", "https://github.com/x/y/pull/23")
    head = "2" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"] and "headRefName" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefName": "backlog/task-d"}), ""
            )
        if argv[:2] == ["run", "list"]:
            runs = [{"databaseId": 55, "headSha": head, "event": "pull_request",
                     "status": "completed", "conclusion": "failure"}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(runs), "")
        if argv[:2] == ["run", "rerun"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }],
            "statusCheckRollup": [
                {"name": "CI", "conclusion": "SUCCESS"},
                {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "FAILURE"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert not report.merged
    assert any(
        task_id == "VOYN-W0-M1D" and reason == "acceptance_gate_rerun_requeued"
        for task_id, reason in report.skipped
    )
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1D",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merge_skips_without_marker(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M2", "https://github.com/x/y/pull/9")

    def fake_gh(argv, repo):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": "b" * 40, "reviews": [],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M2", "no_accept_marker_on_head") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M2",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"  # untouched


def test_merge_skips_a_still_running_check_instead_of_waving_it_through(rig, monkeypatch):  # noqa: F811, E501
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
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}", "submittedAt": "2026-01-01T00:00:00Z"}],
            "statusCheckRollup": [
                {"name": "CI", "status": "IN_PROGRESS", "conclusion": None},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M3", "checks_not_green: ['CI']") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M3",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_merge_skips_a_pending_legacy_status_context_too(rig, monkeypatch):  # noqa: F811
    """The rollup mixes CheckRun and legacy StatusContext shapes; a pending
    StatusContext (`state: PENDING`, no `conclusion`/`status` keys at all)
    must block merge the same as a running CheckRun does."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M4", "https://github.com/x/y/pull/21")
    head = "f" * 40

    def fake_gh(argv, repo):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}", "submittedAt": "2026-01-01T00:00:00Z"}],
            "statusCheckRollup": [{"name": "legacy-ci", "state": "PENDING"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M4", "checks_not_green: ['legacy-ci']") in report.skipped


def test_merge_only_the_most_recent_review_can_carry_the_marker(rig, monkeypatch):  # noqa: F811, E501
    """VOYN-W0-AICC-DISABLE-UNSAFE-AUTOMERGE: an ACCEPT marker sitting in an
    OLDER review must not authorize merge once a NEWER review exists on the
    same head -- e.g. a stale ACCEPT from before a dismissed/superseded
    review. Only the latest review by submittedAt counts."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M5", "https://github.com/x/y/pull/22")
    head = "9" * 40

    def fake_gh(argv, repo):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [
                {"body": f"ACCEPTANCE: ACCEPT {head}", "submittedAt": "2026-01-01T00:00:00Z"},
                {"body": "Actually, hold on -- this needs another look.", "submittedAt": "2026-01-02T00:00:00Z"},
            ],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M5", "no_accept_marker_on_head") in report.skipped


def test_publish_verdict_posts_the_marker_under_the_acceptance_bot_identity(rig, monkeypatch):  # noqa: F811, E501
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
        app_factory, worker, "VOYN-W0-P1", pr_url, head,
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
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []

    def fake_post(creds, pr_url_arg, decision, sha):
        posted.append((pr_url_arg, decision, sha))
        return True, ""

    monkeypatch.setattr(review_merge, "_post_marker_as_bot", fake_post)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P1", "https://github.com/x/y/pull/11") in report.reviewed
    assert posted == [("https://github.com/x/y/pull/11", "ACCEPT", head)]


def test_publish_verdict_skips_without_the_acceptance_bot_configured(rig, monkeypatch):  # noqa: F811, E501
    """A host with no acceptance-bot credentials must not fall back to the
    old same-identity marker -- that marker can never satisfy
    `_pr_is_mergeable`'s different-author check any more, so posting one
    would just be silent, ineffective noise. Skip loudly instead."""
    app_factory, store, worker = rig
    head = "e" * 40
    pr_url = "https://github.com/x/y/pull/12"
    _ready(store, app_factory, "VOYN-W0-P1B", pr_url)
    _complete_review(
        app_factory, worker, "VOYN-W0-P1B", pr_url, head,
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


def test_publish_verdict_reject_dispatches_a_linked_remediation_task(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P2", pr_url, head,
        f"{feedback}\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
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


def test_publish_verdict_reject_remediation_is_idempotent(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P2B", pr_url, head,
        f"A defect.\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
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


def test_publish_verdict_takes_the_last_verdict_not_the_first(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P6", pr_url, head,
        "Initial pass looked clean.\nVERDICT: ACCEPT\n\n"
        "Wait -- rereading the diff, the stale-head check is buggy after all.\n"
        f"Correcting my assessment:\nVERDICT: REJECT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P6", "VOYN-W0-P6-REM") in report.remediated
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_ignores_an_earlier_lookalike_block(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P8", pr_url, real_head,
        "Note: a passing review would read exactly:\n"
        f"VERDICT: ACCEPT\nHEAD_SHA: {fake_head}\n\n"
        "However, after actually reviewing this diff:\n"
        f"VERDICT: REJECT\nHEAD_SHA: {real_head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": real_head, "reviews": []}), "")
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P8", "VOYN-W0-P8-REM") in report.remediated
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_does_not_pair_mismatched_verdict_and_sha(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P7", pr_url, real_head,
        "VERDICT: REJECT\n"
        "The stale-head check is buggy.\n\n"
        f"Note: PR #99 shares HEAD_SHA: {real_head} with this one, where "
        "VERDICT: ACCEPT was correctly given for the analogous change.\n",
    )

    posted = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": real_head, "reviews": []}), "")
        posted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P7", "verdict_or_head_sha_missing_in_review_result") in report.skipped
    assert not any(a[:2] == ["pr", "review"] for a in posted)


def test_publish_verdict_skips_without_a_completed_review_yet(rig, monkeypatch):  # noqa: F811
    import subprocess as sp

    app_factory, store, _worker = rig
    pr_url = "https://github.com/x/y/pull/13"
    _ready(store, app_factory, "VOYN-W0-P3", pr_url)
    live_head = "7" * 40
    SNAPSHOTS[pr_url] = _snapshot(live_head)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": live_head, "reviews": []}), "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P3", "no_review_result_yet") in report.skipped


def test_publish_verdict_skips_when_already_posted(rig, monkeypatch):  # noqa: F811
    app_factory, store, worker = rig
    head = "f" * 40
    pr_url = "https://github.com/x/y/pull/14"
    _ready(store, app_factory, "VOYN-W0-P4", pr_url)
    _complete_review(
        app_factory, worker, "VOYN-W0-P4", pr_url, head,
        f"Looks fine.\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
    )

    posted = []

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        posted.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-P4", "marker_already_posted") in report.skipped
    assert not posted


def test_publish_verdict_a_review_of_an_old_head_never_gets_read_as_current(rig, monkeypatch):  # noqa: F811
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
        app_factory, worker, "VOYN-W0-P5", pr_url, reviewed_sha,
        f"Looks fine.\nVERDICT: ACCEPT\nHEAD_SHA: {reviewed_sha}\n",
    )
    SNAPSHOTS[pr_url] = _snapshot(new_head)

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


def test_merge_skips_when_a_check_is_red(rig, monkeypatch):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M3", "https://github.com/x/y/pull/10")
    head = "c" * 40

    def fake_gh(argv, repo):
        import subprocess
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(t == "VOYN-W0-M3" and "checks_not_green" in r for t, r in report.skipped)


def test_mergeability_uses_latest_check_rerun(monkeypatch):
    import subprocess

    head = "d" * 40

    def fake_gh(argv, repo):
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [
                {"name": "Acceptance gate", "conclusion": "FAILURE", "startedAt": "2026-08-23T04:29:29Z"},
                {"name": "Acceptance gate", "conclusion": "SUCCESS", "startedAt": "2026-08-23T05:25:43Z"},
                {"name": "CI", "conclusion": "SUCCESS", "startedAt": "2026-08-23T04:29:27Z"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    assert review_merge._pr_is_mergeable("/tmp", "https://github.com/x/y/pull/10") == (True, head)


def test_mergeability_rejects_latest_failed_check_rerun(monkeypatch):
    import subprocess

    head = "e" * 40

    def fake_gh(argv, repo):
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [
                {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "SUCCESS", "startedAt": "2026-08-23T04:29:29Z"},
                {"name": "Acceptance gate (independent verdict on exact SHA)", "conclusion": "FAILURE", "startedAt": "2026-08-23T05:25:43Z"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, reason = review_merge._pr_is_mergeable("/tmp", "https://github.com/x/y/pull/10")
    assert not ready
    # Since VOYN-W0-AICC-ACCEPTANCE-TAIL-LATENCY a definitively-red latest
    # gate run under a standing marker surfaces as the reconcile reason (the
    # merge tick re-drives it, bounded) -- but it still never merges.
    assert reason == f"acceptance_gate_stale_red:{head}"


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
        body = json.dumps({
            "state": "OPEN",
            "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": [
                {
                    "name": "Acceptance gate",
                    "conclusion": newer_conclusion,
                    "startedAt": "2026-08-23T05:25:43Z",
                },
                {
                    "name": "Acceptance gate",
                    "conclusion": older_conclusion,
                    "startedAt": "2026-08-23T04:29:29Z",
                },
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, _ = review_merge._pr_is_mergeable(
        "/tmp", "https://github.com/x/y/pull/10"
    )
    assert ready is expected_ready


@pytest.mark.parametrize(
    "checks",
    [
        [
            {"name": "Acceptance gate", "conclusion": "FAILURE", "startedAt": "2026-08-23T05:25:43Z"},
            {"name": "Acceptance gate", "conclusion": "SUCCESS", "startedAt": "2026-08-23T05:25:43Z"},
        ],
        [
            {"name": "Acceptance gate", "conclusion": "FAILURE"},
            {"name": "Acceptance gate", "conclusion": "SUCCESS"},
        ],
    ],
)
def test_mergeability_fails_closed_when_rerun_order_is_ambiguous(monkeypatch, checks):
    import subprocess

    head = "f" * 40

    def fake_gh(argv, repo):
        body = json.dumps({
            "state": "OPEN",
            "headRefOid": head,
            "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
            "statusCheckRollup": checks,
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    ready, reason = review_merge._pr_is_mergeable(
        "/tmp", "https://github.com/x/y/pull/10"
    )
    assert not ready
    assert reason == "checks_not_green: ['Acceptance gate']"


def test_repo_from_pr_url():
    assert review_merge._repo_from_pr_url(
        "https://github.com/voyn88/aios/pull/273"
    ) == "aios"
    assert review_merge._repo_from_pr_url(
        "https://github.com/voyn88/ai-command-center/pull/1"
    ) == "ai-command-center"
    assert review_merge._repo_from_pr_url("not a url") is None
    assert review_merge._repo_from_pr_url("https://github.com/voyn88/aios") is None


def test_review_key_scopes_by_pr_number_head_sha_and_policy_version():
    base = "https://github.com/voyn88/aios/pull/273"
    sha_a = "a" * 40
    sha_b = "b" * 40
    key_a = review_merge._review_key("VOYN-W0-X", base, _snapshot(sha_a))
    key_b = review_merge._review_key("VOYN-W0-X", base, _snapshot(sha_b))
    assert key_a != key_b  # a new commit is a new review, automatically
    assert f":{sha_a}:{review_merge._REVIEW_POLICY_VERSION}:base:{BASE}:diff:" in key_a
    assert review_merge._review_key("VOYN-W0-X", "not a url", _snapshot(sha_a)) is None


def test_review_once_gives_a_second_push_to_the_same_task_its_own_fresh_review(rig, _test_repo_routes, monkeypatch):  # noqa: F811, E501
    """The general case the review-cycle key fixes, not just remediation: an
    ordinary task still IN_PROGRESS/READY_TO_REVIEW that gets a second push
    to its PR must be reviewable again, not permanently deduped against
    whatever the first push's review concluded."""
    import subprocess as sp

    app_factory, store, _ = rig
    pr_url = "https://github.com/x/repo-d2/pull/20"
    _ready(store, app_factory, "VOYN-W0-R5", pr_url)

    def fake_gh(argv, repo):
        if argv[0] == "api" and "/pulls/20" in argv[1]:
            body = {"base": {"sha": BASE, "repo": {"full_name": "x/repo-d2"}},
                    "head": {"sha": next(current_head)}, "changed_files": 1,
                    "additions": 1, "deletions": 0}
            return sp.CompletedProcess(argv, 0, json.dumps(body), "")
        if argv[0] == "api":
            return sp.CompletedProcess(argv, 0, DIFF, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", ORIGINAL_PR_SNAPSHOT)
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


# --- VOYN-W0-AICC-REVIEW-AUTO-ACCEPT: finding-verification adjudication -----

def _force_chunk_reject(monkeypatch, verification, findings="isolated-chunk finding text"):
    """Drive publish_review_verdicts down the multi-chunk REJECT path and make
    the finding-verification lookup return `verification` (a result text) or
    None (no verification run has completed yet)."""
    monkeypatch.setattr(
        review_merge, "_chunk_review_rows",
        lambda factory, task_id, pr_url, snapshot: ("prefix", [{"chunk": 0}]),
    )
    monkeypatch.setattr(
        review_merge, "_aggregate_chunk_verdict",
        lambda rows, snapshot, prefix: ("REJECT", findings),
    )

    def fake_latest(factory, task_id, key):
        # Only the verification key is ever looked up on the multi-chunk
        # REJECT path -- keyed to head + the exact rejecting findings.
        assert key.startswith("verify:"), key
        assert ":findings:" in key
        return {"result_text": verification} if verification is not None else None

    monkeypatch.setattr(review_merge, "_latest_review_result", fake_latest)


def _fake_pr_view(head):
    import subprocess as sp
    posted_comments = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
        if argv[:2] == ["pr", "comment"]:
            posted_comments.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    fake_gh.posted_comments = posted_comments
    return fake_gh


def test_chunk_reject_overridden_by_verification_accept_with_audit(rig, monkeypatch):  # noqa: F811, E501
    """A multi-chunk PR whose chunk aggregation REJECTs must still be accepted
    when the finding-verification run at the exact head ACCEPTs: the audit
    comment recording the overridden findings is posted FIRST, then the
    marker, and no remediation is dispatched."""
    app_factory, store, _ = rig
    head = "a" * 40
    pr_url = "https://github.com/x/y/pull/21"
    _ready(store, app_factory, "VOYN-W0-ADJ-A", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, f"FINDING 1: ARTIFACT -- tree contradicts the claim.\nSECURITY_CLAIMS: NONE\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n")
    fake_gh = _fake_pr_view(head)
    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda creds, pr, decision, sha: (posted.append((pr, decision, sha)) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert posted == [(pr_url, "ACCEPT", head)]
    assert not report.remediated
    assert len(fake_gh.posted_comments) == 1
    audit_body = fake_gh.posted_comments[0][-1]
    assert f"AUTO-ACCEPT-AUDIT {head}" in audit_body
    assert "isolated-chunk finding text" in audit_body  # every overridden finding
    assert "ARTIFACT" in audit_body  # the verifier's classification


def test_auto_accept_is_conditioned_on_the_audit_comment(rig, monkeypatch):  # noqa: F811, E501
    """No audit comment, no marker: if the audit post fails, the override is
    withheld this tick (skip, retried later) rather than merging with no
    trail of what was overridden."""
    import subprocess as sp

    app_factory, store, _ = rig
    head = "d" * 40
    pr_url = "https://github.com/x/y/pull/24"
    _ready(store, app_factory, "VOYN-W0-ADJ-E", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, f"FINDING 1: ARTIFACT -- cited.\nSECURITY_CLAIMS: NONE\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n")

    def failing_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return sp.CompletedProcess(argv, 0, json.dumps({"headRefOid": head, "reviews": []}), "")
        return sp.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(review_merge, "_gh", failing_gh)
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-E", "auto_accept_audit_post_failed") in report.skipped
    assert not posted
    assert not report.remediated


def test_chunk_reject_confirmed_by_verification_reject_remediates(rig, monkeypatch):  # noqa: F811, E501
    """When verification CONFIRMS a blocking finding, the REJECT stands:
    remediation is dispatched carrying the confirmation, no marker."""
    app_factory, store, _ = rig
    head = "b" * 40
    pr_url = "https://github.com/x/y/pull/22"
    _ready(store, app_factory, "VOYN-W0-ADJ-B", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, f"FINDING 1: CONFIRMED_BLOCKING -- reproduced.\nVERDICT: REJECT\nHEAD_SHA: {head}\n")
    posted = []
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-B", "VOYN-W0-ADJ-B-REM") in report.remediated
    assert not posted
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT body FROM backlog_task WHERE task_id=%s", ("VOYN-W0-ADJ-B-REM",))
        body = cur.fetchone()[0]
    assert "Verification confirmed a blocking finding" in body
    assert "CONFIRMED_BLOCKING" in body


def test_chunk_reject_enqueues_verification_and_waits(rig, monkeypatch):  # noqa: F811, E501
    """No verification result yet: ONE verification run is enqueued -- keyed
    to head + findings, task_type verification_review, head-pinned for the
    worker -- and the task skips as `verification_pending` with no
    remediation and no marker."""
    app_factory, store, _ = rig
    head = "c" * 40
    pr_url = "https://github.com/x/y/pull/23"
    _ready(store, app_factory, "VOYN-W0-ADJ-C", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, None)
    from command_center.orchestrator import planner
    monkeypatch.setattr(planner, "repo_route", lambda _r: ("AICC", "/srv/repo"))
    monkeypatch.setattr(review_merge, "cascade_for", lambda _k: [{"executor": "codex"}])
    posted = []
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    enq = []
    report = publish_review_verdicts(
        app_factory, "/tmp",
        enqueue=lambda q, k, pl, tid, mx: enq.append((q, k, pl, tid, mx)),
    )
    assert ("VOYN-W0-ADJ-C", "verification_pending") in report.skipped
    assert not report.remediated
    assert not posted
    assert len(enq) == 1
    _q, key, payload, tid, _mx = enq[0]
    assert key.startswith("verify:VOYN-W0-ADJ-C:23:")
    assert ":findings:" in key
    assert tid == "VOYN-W0-ADJ-C"
    assert payload["task_type"] == "verification_review"
    assert payload["untrusted"] is True
    assert payload["review_head"] == {"pr_number": "23", "head_sha": head}
    assert all(link["task_type"] == "verification_review" for link in payload["cascade"])
    # The findings travel as data in a hashed envelope, never as bare text.
    marker = review_merge._VERIFICATION_INPUT_MARKER
    envelope = json.loads(payload["prompt"].split(marker, 1)[1])
    assert envelope["findings"]["text"] == "isolated-chunk finding text"
    assert envelope["head_sha"] == head


def test_chunk_reject_without_enqueue_falls_back_to_remediation(rig, monkeypatch):  # noqa: F811, E501
    """A caller that cannot enqueue (legacy signature) keeps the
    pre-AUTO-ACCEPT behavior: REJECT remediates on the original findings."""
    app_factory, store, _ = rig
    head = "e" * 40
    pr_url = "https://github.com/x/y/pull/25"
    _ready(store, app_factory, "VOYN-W0-ADJ-F", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, None)
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-F", "VOYN-W0-ADJ-F-REM") in report.remediated


def test_oversized_findings_fall_closed_to_remediation(rig, monkeypatch):  # noqa: F811
    """Findings over the verification byte cap are never auto-verified --
    the REJECT remediates exactly as before AUTO-ACCEPT existed."""
    app_factory, store, _ = rig
    head = "f" * 40
    pr_url = "https://github.com/x/y/pull/26"
    _ready(store, app_factory, "VOYN-W0-ADJ-G", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, None)
    monkeypatch.setattr(review_merge, "_MAX_VERIFICATION_FINDINGS_BYTES", 4)
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    enq = []
    report = publish_review_verdicts(
        app_factory, "/tmp", enqueue=lambda *a: enq.append(a)
    )
    assert ("VOYN-W0-ADJ-G", "VOYN-W0-ADJ-G-REM") in report.remediated
    assert enq == []


def test_single_chunk_reject_is_also_verification_gated(rig, monkeypatch, _test_repo_routes):  # noqa: F811, E501
    """The task's core acceptance: a SINGLE-chunk REJECT no longer remediates
    on the reviewer's word alone -- it enqueues the same finding
    verification, waits, and auto-accepts on verification ACCEPT."""
    app_factory, store, worker = rig
    head = "9" * 40
    pr_url = "https://github.com/x/repo-d2/pull/27"
    _ready(store, app_factory, "VOYN-W0-ADJ-H", pr_url)
    findings = f"Speculative: helper may not validate input.\nVERDICT: REJECT\nHEAD_SHA: {head}\n"
    _complete_review(app_factory, worker, "VOYN-W0-ADJ-H", pr_url, head, findings)
    fake_gh = _fake_pr_view(head)
    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    posted = []
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda creds, pr, decision, sha: (posted.append((decision, sha)) or (True, "")))
    enq = []
    report = publish_review_verdicts(
        app_factory, "/tmp",
        enqueue=lambda q, k, pl, tid, mx: enq.append((q, k, pl, tid, mx)),
    )
    assert ("VOYN-W0-ADJ-H", "verification_pending") in report.skipped
    assert not report.remediated and not posted
    assert len(enq) == 1
    _q, vkey, payload, _tid, _mx = enq[0]
    assert vkey.startswith("verify:VOYN-W0-ADJ-H:27:")
    assert payload["review_head"]["head_sha"] == head

    # The verification run completes with ACCEPT at the exact head -> the
    # next tick posts audit + marker, zero-touch.
    from command_center.db.work_queue_store import WorkQueueStore

    vstore = WorkQueueStore(app_factory)
    vstore.enqueue("execution", idempotency_key=vkey, payload=payload, task_id="VOYN-W0-ADJ-H")
    claimed = worker.claim("execution", visibility_seconds=60)
    assert worker.complete(claimed, {
        "status": "completed",
        "result_text": f"FINDING 1: ARTIFACT -- cited.\nSECURITY_CLAIMS: NONE\nVERDICT: ACCEPT\nHEAD_SHA: {head}",
    })
    second = publish_review_verdicts(app_factory, "/tmp", enqueue=lambda *a: None)
    assert posted == [("ACCEPT", head)]
    assert not second.remediated
    assert any(f"AUTO-ACCEPT-AUDIT {head}" in argv[-1] for argv in fake_gh.posted_comments)


def test_verification_key_is_scoped_by_head_and_findings():
    snap_a = _snapshot("a" * 40)
    key = review_merge._verification_key("T", "https://github.com/x/y/pull/1", snap_a, "f1")
    assert key.startswith("verify:T:1:" + "a" * 40)
    other_findings = review_merge._verification_key(
        "T", "https://github.com/x/y/pull/1", snap_a, "f2"
    )
    other_head = review_merge._verification_key(
        "T", "https://github.com/x/y/pull/1", _snapshot("b" * 40), "f1"
    )
    assert len({key, other_findings, other_head}) == 3
    assert review_merge._verification_key("T", "not-a-pr-url", snap_a, "f1") is None


def test_malformed_verifier_output_never_auto_accepts(rig, monkeypatch):  # noqa: F811
    """A verifier that cannot state its verdict/head cleanly (garbled text,
    or a HEAD_SHA that is not the current head) must fall to remediation on
    the ORIGINAL findings -- never to an override, and never to a permanent
    park."""
    app_factory, store, _ = rig
    head = "3" * 40
    pr_url = "https://github.com/x/y/pull/28"
    _ready(store, app_factory, "VOYN-W0-ADJ-I", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, "VERDICT: ACCEPT\nHEAD_SHA: " + "4" * 40 + "\n")
    posted = []
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-I", "VOYN-W0-ADJ-I-REM") in report.remediated
    assert not posted


def test_a_malformed_accept_never_auto_accepts(rig, monkeypatch):  # noqa: F811
    """Independent review of this change (32bf893, then 6eb71aa): an ACCEPT
    that violates the verifier's machine-parsed output contract -- bare
    trailer, a classification token merely QUOTED mid-line from the
    untrusted findings, a CONFIRMED_BLOCKING disposition contradicting the
    ACCEPT trailer, or a missing SECURITY_CLAIMS attestation -- is malformed
    output, not an override. Fail closed to remediation."""
    app_factory, store, _ = rig
    head = "5" * 40
    pr_url = "https://github.com/x/y/pull/29"
    _ready(store, app_factory, "VOYN-W0-ADJ-J", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    _force_chunk_reject(monkeypatch, f"VERDICT: ACCEPT\nHEAD_SHA: {head}\n")
    posted = []
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-J", "VOYN-W0-ADJ-J-REM") in report.remediated
    assert not posted

    # The full contract, unit-level: every malformed shape is refused, the
    # exact well-formed shape passes.
    ok = review_merge._verification_accept_is_well_formed
    assert not ok("VERDICT: ACCEPT")  # bare trailer
    assert not ok("the findings mention ARTIFACT and UNVERIFIABLE tokens")  # quoted mid-line
    assert not ok("FINDING 1: ARTIFACT -- cited.")  # no security attestation
    assert not ok(  # disposition contradicts the ACCEPT trailer
        "FINDING 1: CONFIRMED_BLOCKING -- reproduced.\nSECURITY_CLAIMS: NONE"
    )
    assert not ok("SECURITY_CLAIMS: NONE")  # attestation with no dispositions
    assert not ok(  # attestation value outside the vocabulary
        "FINDING 1: ARTIFACT -- cited.\nSECURITY_CLAIMS: UNVERIFIABLE"
    )
    assert ok(
        "FINDING 1: ARTIFACT -- src/x.py:10 already validates.\n"
        "FINDING 2: CONFIRMED_MINOR -- naming only.\n"
        "SECURITY_CLAIMS: DISPROVEN"
    )


# --- VOYN-OPS-AICC-VERIFY-DISPOSITION-FLOOR: disposition-count floor and ---
# --- sequential FINDING numbering ------------------------------------------

def test_disposition_floor_requires_one_per_rejecting_chunk_section():
    """`_aggregate_chunk_verdict` builds the findings text handed to
    verification as one `Chunk i/N:` section per REJECTing chunk --
    orchestrator-known, not the verifier's self-report. A verifier that only
    classifies chunk 1 of a 2-chunk rejection has silently dropped chunk 2's
    findings and must not be honored as a well-formed ACCEPT."""
    ok = review_merge._verification_accept_is_well_formed
    two_chunk_findings = "Chunk 1/2:\nfoo bug\n\nChunk 2/2:\nbar bug"
    one_disposition = "FINDING 1: ARTIFACT -- cited.\nSECURITY_CLAIMS: NONE"
    two_dispositions = (
        "FINDING 1: ARTIFACT -- cited.\n"
        "FINDING 2: ARTIFACT -- cited.\n"
        "SECURITY_CLAIMS: NONE"
    )
    assert not ok(one_disposition, two_chunk_findings)
    assert ok(two_dispositions, two_chunk_findings)
    # A single-chunk (or non-aggregated) REJECT has no `Chunk i/N:` section
    # at all and still floors at 1 -- unchanged from verify-v2.
    assert ok(one_disposition, "free-text findings, no chunk headers here")
    assert ok(one_disposition)  # default findings="" also floors at 1


def test_disposition_numbering_must_be_sequential_without_gaps():
    """FINDING numbers must cover exactly 1..K -- a gap or a duplicate means
    the verifier skipped or double-counted a finding rather than classifying
    each one, which the count-floor check alone would not catch."""
    ok = review_merge._verification_accept_is_well_formed
    assert not ok(  # gap: no FINDING 2
        "FINDING 1: ARTIFACT -- cited.\n"
        "FINDING 3: ARTIFACT -- cited.\n"
        "SECURITY_CLAIMS: NONE"
    )
    assert not ok(  # duplicate numbering, no FINDING 2
        "FINDING 1: ARTIFACT -- cited.\n"
        "FINDING 1: ARTIFACT -- cited.\n"
        "SECURITY_CLAIMS: NONE"
    )
    assert ok(
        "FINDING 1: ARTIFACT -- cited.\n"
        "FINDING 2: CONFIRMED_MINOR -- naming only.\n"
        "SECURITY_CLAIMS: DISPROVEN"
    )


def test_partial_chunk_verification_coverage_remediates(rig, monkeypatch):  # noqa: F811, E501
    """VOYN-OPS-AICC-VERIFY-DISPOSITION-FLOOR's core acceptance: two chunks
    REJECT, but the verifier's ACCEPT only classifies one of them -- one
    disposition against a two-section floor -- so the override is refused
    and the task remediates on the original (both-chunk) findings, exactly
    the malformed-output fail-closed leg."""
    app_factory, store, _ = rig
    head = "6" * 40
    pr_url = "https://github.com/x/y/pull/30"
    _ready(store, app_factory, "VOYN-W0-ADJ-K", pr_url)
    SNAPSHOTS[pr_url] = _snapshot(head)
    two_chunk_findings = "Chunk 1/2:\nfoo bug\n\nChunk 2/2:\nbar bug"
    _force_chunk_reject(
        monkeypatch,
        f"FINDING 1: ARTIFACT -- cited.\nSECURITY_CLAIMS: NONE\nVERDICT: ACCEPT\nHEAD_SHA: {head}\n",
        findings=two_chunk_findings,
    )
    posted = []
    monkeypatch.setattr(review_merge, "_gh", _fake_pr_view(head))
    monkeypatch.setattr(
        review_merge, "_acceptance_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials("1", "2", "/dev/null"),
    )
    monkeypatch.setattr(review_merge, "_post_marker_as_bot",
                        lambda *a: (posted.append(a) or (True, "")))
    report = publish_review_verdicts(app_factory, "/tmp")
    assert ("VOYN-W0-ADJ-K", "VOYN-W0-ADJ-K-REM") in report.remediated
    assert not posted
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT body FROM backlog_task WHERE task_id=%s", ("VOYN-W0-ADJ-K-REM",))
        body = cur.fetchone()[0]
    assert "Chunk 1/2" in body and "Chunk 2/2" in body  # both chunks, not just the verified one


def test_review_once_no_longer_enqueues_an_eager_adjudication(rig, _test_repo_routes, monkeypatch):  # noqa: F811, E501
    """The eager MODEL_ONLY full-context adjudication of VOYN-W0-AICC-REVIEW-
    ADJUDICATE is retired (it ran with zero tools and could gather no
    evidence): a multi-chunk PR enqueues its chunks and NOTHING else --
    adjudication now happens lazily, by finding verification, on REJECT."""
    app_factory, store, _ = rig
    pr_url = "https://github.com/x/repo-d2/pull/31"
    _ready(store, app_factory, "VOYN-W0-ADJ-D", pr_url)
    head = "d" * 40
    snap = _snapshot(head)
    SNAPSHOTS[pr_url] = snap
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda _r, _p: snap)
    monkeypatch.setattr(review_merge, "_review_chunks", lambda s, t, p: review_merge._make_diff_chunks(["a", "b", "c"]))
    monkeypatch.setattr(review_merge, "_render_review_prompt", lambda t, p, s, c: "prompt")
    monkeypatch.setattr(review_merge, "_prompt_size_bytes", lambda s: 1)
    enq = []
    report = review_once(app_factory, lambda q, k, pay, task_id, mx: enq.append(k), "/tmp")
    assert ("VOYN-W0-ADJ-D", pr_url) in report.reviewed
    assert len(enq) == 3
    assert all(":chunk:" in k for k in enq), enq


# --- VOYN-W0-AICC-MERGE-TRAIN-COORDINATOR: keep behind PRs from gridlocking --

def test_merge_train_updates_a_behind_pr(rig, monkeypatch):  # noqa: F811
    """A PR only BEHIND main (base advanced after it branched) is brought
    current with `gh pr update-branch` so it re-enters the merge path instead
    of gridlocking; nothing is merged this tick."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MT1", "https://github.com/x/y/pull/41")
    head = "a" * 40
    calls = []

    def fake_gh(argv, repo):
        import subprocess
        calls.append(argv[:2])
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head, "mergeStateStatus": "BEHIND",
                "author": {"login": "writer-bot"},
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}",
                             "author": {"login": "voyn88-acceptance-gate[bot]"}}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "update-branch"]:
            return subprocess.CompletedProcess(argv, 0, "updated", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ["pr", "update-branch"] in calls
    assert ("VOYN-W0-MT1", "branch_updated_behind_main") in report.skipped
    assert not report.merged


def test_merge_train_does_not_update_an_unaccepted_behind_pr(rig, monkeypatch):  # noqa: F811
    """A BEHIND PR with no ACCEPT marker is NOT branch-updated: the review tick
    reviews it from its own diff regardless of base distance, so the merge tick
    must not spend CI + the update quota on a PR that is not yet merge-ready
    (which would starve accepted-but-behind PRs). (MERGE-TRAIN-COORDINATOR)"""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MT3", "https://github.com/x/y/pull/43")
    head = "d" * 40
    calls = []

    def fake_gh(argv, repo):
        import subprocess
        calls.append(argv[:2])
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head, "mergeStateStatus": "BEHIND",
                "author": {"login": "writer-bot"},
                "reviews": [], "statusCheckRollup": [],  # not accepted
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ["pr", "update-branch"] not in calls
    assert ("VOYN-W0-MT3", "no_accept_marker_on_head") in report.skipped
    assert not report.merged


def test_merge_state_treats_malformed_gh_output_as_a_failed_lookup(monkeypatch):
    """A zero-exit-but-unparseable `gh pr view` must return "" (a failed
    lookup), never raise and abort the merge tick.
    (VOYN-W0-AICC-MERGE-TRAIN-COORDINATOR)"""
    import subprocess

    def fake_gh(argv, repo):
        return subprocess.CompletedProcess(argv, 0, "not json <html>", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    assert review_merge._merge_state("/tmp", "https://github.com/x/y/pull/9") == ""


def test_merge_train_leaves_a_dirty_pr_for_rebase(rig, monkeypatch):  # noqa: F811
    """A DIRTY (conflicting) PR is flagged for a rebase and never auto-updated
    -- update-branch cannot resolve a real conflict."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-MT2", "https://github.com/x/y/pull/42")
    head = "b" * 40
    calls = []

    def fake_gh(argv, repo):
        import subprocess
        calls.append(argv[:2])
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head, "mergeStateStatus": "DIRTY",
                "author": {"login": "writer-bot"},
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}",
                             "author": {"login": "voyn88-acceptance-gate[bot]"}}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ["pr", "update-branch"] not in calls
    assert ("VOYN-W0-MT2", "branch_dirty_needs_rebase") in report.skipped


def test_merge_train_update_cap_is_bounded(rig, monkeypatch):  # noqa: F811
    """The per-tick branch-update count is bounded so a moving base cannot make
    one tick spend everything re-updating branches that will fall behind again."""
    app_factory, store, _ = rig
    for i in range(4):
        _ready(store, app_factory, f"VOYN-W0-MTC{i}", f"https://github.com/x/y/pull/5{i}")
    head = "c" * 40
    updates = []

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head, "mergeStateStatus": "BEHIND",
                "author": {"login": "writer-bot"},
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}",
                             "author": {"login": "voyn88-acceptance-gate[bot]"}}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "update-branch"]:
            updates.append(argv)
            return subprocess.CompletedProcess(argv, 0, "updated", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp", review_merge.ReviewConfig(max_branch_updates_per_tick=2))
    assert len(updates) == 2
    assert sum(1 for _, r in report.skipped if r == "branch_updated_behind_main") == 2
    assert sum(1 for _, r in report.skipped if r == "branch_behind_update_capped") == 2


def test_marker_post_reruns_the_failing_pull_request_acceptance_gate(monkeypatch):
    """After the marker is posted, the failing pull_request-triggered
    Acceptance-gate run for the exact head is re-run so branch protection stops
    seeing a red required check. (VOYN-W0-AICC-ACCEPTANCE-GATE-AUTO-REEVAL)"""
    import subprocess

    sha = "a" * 40
    reran = []

    branches = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefName": "feature/x"}), "")
        if argv[:2] == ["run", "list"]:
            branches.append(argv)  # must be scoped to the PR's branch
            body = json.dumps([
                {"databaseId": 111, "headSha": sha, "event": "pull_request",
                 "status": "completed", "conclusion": "failure"},
                {"databaseId": 222, "headSha": sha, "event": "pull_request_review",
                 "status": "completed", "conclusion": "success"},
                {"databaseId": 333, "headSha": "b" * 40, "event": "pull_request",
                 "status": "completed", "conclusion": "failure"},
            ])
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "rerun"]:
            reran.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    review_merge._rerun_failing_acceptance_gate("/tmp", "https://github.com/x/y/pull/1", sha)
    assert reran == ["111"]  # only the failing pull_request run for THIS head
    assert branches and "--branch" in branches[0] and "feature/x" in branches[0]


# --- VOYN-W0-AICC-MERGE-DONE-BEFORE-TARGET-VERIFY + CI-FLAKE-AUTO-RERUN -----


def test_a_queued_merge_is_a_wait_not_a_done(rig, monkeypatch):  # noqa: F811
    """Live 2026-08-26 (PR #399): on a merge-queue-protected repo,
    `gh pr merge` exits 0 having only ENQUEUED the PR. The task must stay
    READY_TO_REVIEW (`merge_queued_awaiting_target`) until GitHub reports
    MERGED -- and then complete idempotently on a later tick with the true
    merge commit, even though `_pr_is_mergeable` reports a merged PR as
    not-ready."""
    import subprocess as sp

    app_factory, store, _ = rig
    head, merge_oid = "3" * 40, "4" * 40
    pr_url = "https://github.com/x/y/pull/30"
    _ready(store, app_factory, "VOYN-W0-MQ", pr_url)
    queue_state = {"merged": False}
    merge_calls = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            if queue_state["merged"]:
                body = json.dumps({
                    "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                    "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            else:
                body = json.dumps({
                    "state": "OPEN", "headRefOid": head,
                    "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                    "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
                })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            merge_calls.append(argv)
            return sp.CompletedProcess(argv, 0, "queued", "")  # enqueued, NOT merged
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MQ", "merge_queued_awaiting_target") in report.skipped
    assert not report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MQ",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"

    # The queue lands the PR between ticks; the next tick completes DONE
    # without calling `gh pr merge` again.
    queue_state["merged"] = True
    calls_before = len(merge_calls)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MQ", merge_oid) in report.merged
    assert len(merge_calls) == calls_before
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MQ",))
        assert cur.fetchone()[0] == "DONE"
        cur.execute("SELECT value FROM backlog_evidence WHERE task_id=%s AND kind='sha'", ("VOYN-W0-MQ",))
        assert cur.fetchone()[0] == merge_oid


def test_failed_checks_on_an_accepted_head_get_one_bounded_rerun(rig, monkeypatch):  # noqa: F811, E501
    """VOYN-W0-AICC-CI-FLAKE-AUTO-RERUN: a red required check on a PR that
    already carries the ACCEPT marker triggers `gh run rerun --failed` for
    completed-failed runs at attempt 1 on the current head -- and never for
    a run already at attempt 2 (GitHub's own attempt counter is the bound),
    never for other heads, never while checks are merely pending."""
    import subprocess as sp

    app_factory, store, _ = rig
    head = "6" * 40
    pr_url = "https://github.com/x/y/pull/31"
    _ready(store, app_factory, "VOYN-W0-MF", pr_url)
    reruns = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"] and "headRefName" in argv[-1]:
            return sp.CompletedProcess(argv, 0, json.dumps(
                {"headRefName": "backlog/x", "headRefOid": head}), "")
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "list"]:
            return sp.CompletedProcess(argv, 0, json.dumps([
                {"databaseId": 11, "headSha": head, "status": "completed",
                 "conclusion": "failure", "attempt": 1},
                {"databaseId": 12, "headSha": head, "status": "completed",
                 "conclusion": "failure", "attempt": 2},
                {"databaseId": 13, "headSha": "7" * 40, "status": "completed",
                 "conclusion": "failure", "attempt": 1},
                {"databaseId": 14, "headSha": head, "status": "in_progress",
                 "conclusion": None, "attempt": 1},
            ]), "")
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv)
            return sp.CompletedProcess(argv, 0, "", "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    skip = dict(report.skipped)["VOYN-W0-MF"]
    assert skip.startswith("checks_not_green") and "flaky_rerun_dispatched:1" in skip
    assert reruns == [["run", "rerun", "11", "--failed"]]
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MF",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_no_rerun_without_an_accept_marker(rig, monkeypatch):  # noqa: F811
    """An unaccepted PR's failures are the review path's business -- the
    flake retry must not spend reruns on a PR that may never be accepted."""
    import subprocess as sp

    app_factory, store, _ = rig
    head = "8" * 40
    pr_url = "https://github.com/x/y/pull/32"
    _ready(store, app_factory, "VOYN-W0-MG", pr_url)
    reruns = []

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head, "reviews": [],
                "statusCheckRollup": [{"name": "CI", "conclusion": "FAILURE"}],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv)
        return sp.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MG", "no_accept_marker_on_head") in report.skipped
    assert reruns == []


def _fake_reconcile_gh(default_branch, statuses):
    """statuses: {sha: compare-status}. `--jq` mocking is by-hand since the
    real `gh` process never runs in these tests."""
    import subprocess as sp

    def fake_gh(argv, repo):
        if argv[:2] == ["api", "repos/x/y"] and argv[-1] == ".default_branch":
            return sp.CompletedProcess(argv, 0, default_branch, "")
        if argv[0] == "api" and "/compare/" in argv[1]:
            sha = argv[1].rsplit("...", 1)[1]
            status = statuses.get(sha)
            if status is None:
                return sp.CompletedProcess(argv, 1, "", "not found")
            return sp.CompletedProcess(argv, 0, status, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    return fake_gh


def test_reconcile_flags_a_sha_not_on_the_default_branch(rig, monkeypatch):  # noqa: F811
    """VOYN-W0-AICC-MERGE-DONE-BEFORE-TARGET-VERIFY: a DONE task whose sha
    evidence predates the fix (a PR head that a squash merge never puts on
    main) is surfaced as suspect -- report-only."""
    app_factory, store, _ = rig
    bad_sha = "9" * 40
    _done(store, app_factory, "VOYN-W0-RC1", "https://github.com/x/y/pull/40", bad_sha)
    monkeypatch.setattr(
        review_merge, "_gh", _fake_reconcile_gh("main", {bad_sha: "diverged"})
    )
    report = reconcile_merge_evidence(app_factory, "/tmp")
    assert report.suspect == [("VOYN-W0-RC1", bad_sha, "sha_not_on_main")]
    assert report.verified == []
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-RC1",))
        assert cur.fetchone()[0] == "DONE"  # report-only: never flipped


def test_reconcile_verifies_a_sha_that_is_an_ancestor(rig, monkeypatch):  # noqa: F811
    """A post-fix DONE row, whose sha IS the target-branch merge commit,
    is verified rather than flagged."""
    app_factory, store, _ = rig
    good_sha = "a" * 40
    _done(store, app_factory, "VOYN-W0-RC2", "https://github.com/x/y/pull/41", good_sha)
    monkeypatch.setattr(
        review_merge, "_gh", _fake_reconcile_gh("main", {good_sha: "behind"})
    )
    report = reconcile_merge_evidence(app_factory, "/tmp")
    assert report.verified == [("VOYN-W0-RC2", good_sha)]
    assert report.suspect == []


def test_reconcile_skips_without_pr_evidence_to_identify_the_repo(rig):  # noqa: F811
    """A DONE task carrying sha evidence but no pr evidence cannot be
    checked -- an inconclusive skip, never a false suspect."""
    from tests.db.test_backlog_planner import _task

    app_factory, store, _ = rig
    assert store.upsert_task(_task("VOYN-W0-RC3", repo="repo-x", status="DONE"))[0]
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT backlog_record_evidence(%s,'sha',%s)", ("VOYN-W0-RC3", "b" * 40)
        )
        c.commit()
    report = reconcile_merge_evidence(app_factory, "/tmp")
    assert ("VOYN-W0-RC3", "no_pr_evidence_to_identify_repo") in report.skipped
    assert report.suspect == []
    assert report.verified == []


def test_reconcile_skips_an_unresolvable_lookup_instead_of_flagging(rig, monkeypatch):  # noqa: F811, E501
    """A failed default-branch lookup (gh hiccup, auth issue) is inconclusive
    -- it must never be reported as a suspect sha, which would read as a
    confirmed defect rather than "the check itself didn't run"."""
    app_factory, store, _ = rig
    sha = "c" * 40
    _done(store, app_factory, "VOYN-W0-RC4", "https://github.com/x/y/pull/42", sha)

    import subprocess as sp

    monkeypatch.setattr(
        review_merge, "_gh", lambda argv, repo: sp.CompletedProcess(argv, 1, "", "down")
    )
    report = reconcile_merge_evidence(app_factory, "/tmp")
    assert ("VOYN-W0-RC4", "default_branch_lookup_failed") in report.skipped
    assert report.suspect == []


def test_an_externally_merged_pr_without_acceptance_never_goes_done(rig, monkeypatch):  # noqa: F811, E501
    """Verification of 53c7b52 (CONFIRMED): a PR merged around the queue --
    admin bypass, hand merge -- with no acceptance marker on its merged head
    must not be silently blessed DONE. It skips loudly for the operator."""
    import subprocess as sp

    app_factory, store, _ = rig
    head, merge_oid = "a1" * 20, "b2" * 20
    pr_url = "https://github.com/x/y/pull/33"
    _ready(store, app_factory, "VOYN-W0-MX", pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                "headRefOid": head, "reviews": [],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MX", "merged_without_acceptance_evidence") in report.skipped
    assert not report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-MX",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_an_empty_check_rollup_on_a_merged_pr_is_inconclusive(rig, monkeypatch):  # noqa: F811, E501
    """Review of eabe0d3: `any()` over an empty rollup is False -- a merged
    PR whose check data is unavailable must fail closed as missing
    acceptance evidence, never be blessed DONE."""
    import subprocess as sp

    app_factory, store, _ = rig
    head, merge_oid = "d3" * 20, "e4" * 20
    pr_url = "https://github.com/x/y/pull/34"
    _ready(store, app_factory, "VOYN-W0-MY", pr_url)

    def fake_gh(argv, repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED", "mergeCommit": {"oid": merge_oid},
                "headRefOid": head,
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {head}"}],
                "statusCheckRollup": [],
            })
            return sp.CompletedProcess(argv, 0, body, "")
        return sp.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-MY", "merged_without_acceptance_evidence") in report.skipped
    assert not report.merged


# --- acceptance-tail reconcile + telemetry (VOYN-W0-AICC-ACCEPTANCE-TAIL-LATENCY)


def test_stale_red_acceptance_gate_is_rerun_and_capped(rig, monkeypatch):  # noqa: F811
    """An accepted head whose ONLY red check is the Acceptance gate gets the
    gate re-driven by the merge tick — bounded at max_gate_reruns_per_head
    across ticks via persistent evidence rows, and never merged around."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1E", "https://github.com/x/y/pull/24")
    head = "3" * 40
    reruns = []

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"] and "headRefName" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefName": "backlog/task-x"}), ""
            )
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [{
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "author": {"login": "voyn88-acceptance-gate[bot]"},
                }],
                "statusCheckRollup": [
                    {"name": "CI", "conclusion": "SUCCESS"},
                    {"name": "Acceptance gate (independent verdict on exact SHA)",
                     "conclusion": "FAILURE"},
                ],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["run", "list"]:
            runs = [{"databaseId": 77, "headSha": head, "event": "pull_request",
                     "status": "completed", "conclusion": "failure"}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(runs), "")
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    for expected_attempts in (1, 2, 3):
        report = merge_once(app_factory, "/tmp")
        assert not report.merged
        assert ("VOYN-W0-M1E", "acceptance_gate_rerun_requeued") in report.skipped
        assert len(reruns) == expected_attempts
    # Fourth tick: the persistent per-head budget is spent — no more reruns,
    # the genuinely refusing gate is left for a human.
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1E", "acceptance_gate_rerun_capped") in report.skipped
    assert len(reruns) == 3
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE %s",
            ("VOYN-W0-M1E", f"gate_rerun:{head}:%"),
        )
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1E",))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"


def test_stale_gate_reconcile_needs_the_gate_to_be_the_only_red_check(rig, monkeypatch):  # noqa: F811, E501
    """CI red alongside the gate: re-driving the gate could not make the PR
    mergeable, so the tick keeps the plain passive skip and spends nothing."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1F", "https://github.com/x/y/pull/25")
    head = "5" * 40
    reruns = []

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }],
            "statusCheckRollup": [
                {"name": "CI", "conclusion": "FAILURE"},
                {"name": "Acceptance gate (independent verdict on exact SHA)",
                 "conclusion": "FAILURE"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert any(
        task_id == "VOYN-W0-M1F" and reason.startswith("checks_not_green")
        for task_id, reason in report.skipped
    )
    assert reruns == []


def test_merge_records_acceptance_latency_evidence(rig, monkeypatch):  # noqa: F811
    """A successful merge persists the verdict-marker -> merge latency with
    the DONE evidence, so the acceptance tail is measurable from the store.
    Queue-aware: the fake reports MERGED with its merge commit after the
    merge call, per MERGE-DONE-BEFORE-TARGET-VERIFY."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1G", "https://github.com/x/y/pull/26")
    head, merge_oid = "6" * 40, "a1" * 20
    marker_at = "2026-08-26T03:00:09Z"
    state = {"merged": False}

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED" if state["merged"] else "OPEN",
                "mergeCommit": {"oid": merge_oid} if state["merged"] else None,
                "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [{
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "author": {"login": "voyn88-acceptance-gate[bot]"},
                    "submittedAt": marker_at,
                }],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            state["merged"] = True
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1G", merge_oid) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT value FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE 'latency:%%'",
            ("VOYN-W0-M1G",),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        parts = rows[0][0].split(":")
        assert parts[0] == "latency" and parts[1] == merge_oid
        assert marker_at.replace("Z", "") in rows[0][0]
        assert int(rows[0][0].rsplit(":", 1)[1]) >= 0

def test_pending_acceptance_gate_is_not_stale_red_and_burns_no_attempt(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding 1 on 8208ede: a still-running gate run has
    conclusion None — not a completed red run. It must take the plain
    passive skip, never the reconcile path (whose rerun helper requires a
    completed run anyway), and must not burn a rerun attempt."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1H", "https://github.com/x/y/pull/27")
    head = "7" * 40
    reruns = []

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["run", "rerun"]:
            reruns.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }],
            "statusCheckRollup": [
                {"name": "CI", "conclusion": "SUCCESS"},
                {"name": "Acceptance gate (independent verdict on exact SHA)",
                 "status": "IN_PROGRESS"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert not report.merged
    assert any(
        task_id == "VOYN-W0-M1H" and reason.startswith("checks_not_green")
        for task_id, reason in report.skipped
    )
    assert reruns == []
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance'",
            ("VOYN-W0-M1H",),
        )
        assert cur.fetchone()[0] == 0


def test_gate_rerun_cap_holds_under_concurrent_reconcilers(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding 2 on 8208ede: concurrent reconcilers used to
    read the same pre-transaction count, collide on the (idempotent-by-
    UNIQUE) evidence value, and BOTH issue a rerun — exceeding the cap.
    Under the advisory-lock claim, N concurrent calls with cap=3 yield
    exactly 3 distinct attempt rows and exactly 3 reruns."""
    import threading

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1I", "https://github.com/x/y/pull/28")
    head = "8" * 40
    reruns = []
    monkeypatch.setattr(
        review_merge,
        "_rerun_failing_acceptance_gate",
        lambda repo, pr, sha: (reruns.append(sha), "dispatched")[1],
    )
    results = []

    def call():
        results.append(
            review_merge._reconcile_stale_acceptance_gate(
                app_factory, "/tmp", "VOYN-W0-M1I",
                "https://github.com/x/y/pull/28", head, 3,
            )
        )

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [
        "acceptance_gate_rerun_capped",
        "acceptance_gate_rerun_capped",
        "acceptance_gate_rerun_requeued",
        "acceptance_gate_rerun_requeued",
        "acceptance_gate_rerun_requeued",
    ]
    assert len(reruns) == 3
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT value FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' ORDER BY value",
            ("VOYN-W0-M1I",),
        )
        values = [r[0] for r in cur.fetchall()]
        assert values == [f"gate_rerun:{head}:1", f"gate_rerun:{head}:2", f"gate_rerun:{head}:3"]


def test_failed_rerun_dispatch_does_not_consume_the_budget(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding on 862ad82: a transient gh failure used to burn
    a persistent attempt without any rerun dispatched — three hiccups could
    permanently cap an accepted PR. A failed dispatch now rolls the claim
    back, reports its own reason, and the budget stays intact for the next
    tick."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1J", "https://github.com/x/y/pull/29")
    head = "9" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"] and "headRefName" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefName": "backlog/task-j"}), ""
            )
        if argv[:2] == ["run", "list"]:
            return subprocess.CompletedProcess(argv, 1, "", "transient")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }],
            "statusCheckRollup": [
                {"name": "CI", "conclusion": "SUCCESS"},
                {"name": "Acceptance gate (independent verdict on exact SHA)",
                 "conclusion": "FAILURE"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    for _ in range(4):  # more rounds than the cap: none may consume budget
        report = merge_once(app_factory, "/tmp")
        assert ("VOYN-W0-M1J", "acceptance_gate_rerun_dispatch_failed") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        # Every failed dispatch leaves a reservation AND its refund: spent
        # budget stays zero, the audit trail of the attempts stays real.
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE %s",
            ("VOYN-W0-M1J", f"gate_rerun:{head}:%"),
        )
        assert cur.fetchone()[0] == 4
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE %s",
            ("VOYN-W0-M1J", f"gate_rerun_fail:{head}:%"),
        )
        assert cur.fetchone()[0] == 4


def test_naive_marker_timestamp_never_aborts_post_merge_bookkeeping(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding on 692ba90: a timezone-naive submittedAt made
    the latency subtraction raise TypeError AFTER the merge, so the DONE
    transition never ran on an already-merged PR. Naive is treated as UTC:
    the merge completes, the task goes DONE, the latency row still lands."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1K", "https://github.com/x/y/pull/30")
    head, merge_oid = "b" * 40, "b2" * 20
    state = {"merged": False}

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED" if state["merged"] else "OPEN",
                "mergeCommit": {"oid": merge_oid} if state["merged"] else None,
                "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [{
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "author": {"login": "voyn88-acceptance-gate[bot]"},
                    "submittedAt": "2026-08-26T03:00:09",  # naive, no zone
                }],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            state["merged"] = True
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1K", merge_oid) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1K",))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE 'latency:%%'",
            ("VOYN-W0-M1K",),
        )
        assert cur.fetchone()[0] == 1

def test_malformed_review_data_never_aborts_post_merge_bookkeeping(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding on cee3a19: a null entry in `reviews` raised
    inside the telemetry marker lookup AFTER the irreversible merge, before
    evidence and DONE. Telemetry that cannot be computed records nothing;
    the merge still completes its bookkeeping."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1L", "https://github.com/x/y/pull/31")
    head, merge_oid = "c" * 40, "c3" * 20
    state = {"merged": False}

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"] and "reviews,author,headRefOid" in argv:
            # telemetry's own lookup: malformed — a null review entry
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"reviews": [None], "author": {}}), ""
            )
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "MERGED" if state["merged"] else "OPEN",
                "mergeCommit": {"oid": merge_oid} if state["merged"] else None,
                "headRefOid": head,
                "author": {"login": "dimastov-lab"},
                "reviews": [{
                    "body": f"ACCEPTANCE: ACCEPT {head}",
                    "author": {"login": "voyn88-acceptance-gate[bot]"},
                    "submittedAt": "2026-08-26T03:00:09Z",
                }],
                "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "merge"]:
            state["merged"] = True
            return subprocess.CompletedProcess(argv, 0, "merged", "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1L", merge_oid) in report.merged
    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", ("VOYN-W0-M1L",))
        assert cur.fetchone()[0] == "DONE"
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE 'latency:%%'",
            ("VOYN-W0-M1L",),
        )
        assert cur.fetchone()[0] == 0  # telemetry skipped, never fatal

def test_ambiguous_rerun_dispatch_keeps_the_reservation_spent(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding on 439347c: nonzero from `gh run rerun` itself
    may follow a server-side accept — refunding there could let later ticks
    exceed the cap. Ambiguity keeps the reservation spent: three ambiguous
    rounds exhaust the budget, no refunds appear, and the cap holds."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-M1M", "https://github.com/x/y/pull/32")
    head = "d" * 40

    def fake_gh(argv, repo):
        import subprocess
        if argv[:2] == ["pr", "view"] and "headRefName" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headRefName": "backlog/task-m"}), ""
            )
        if argv[:2] == ["run", "list"]:
            runs = [{"databaseId": 99, "headSha": head, "event": "pull_request",
                     "status": "completed", "conclusion": "failure"}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(runs), "")
        if argv[:2] == ["run", "rerun"]:
            return subprocess.CompletedProcess(argv, 1, "", "http 502")
        body = json.dumps({
            "state": "OPEN", "headRefOid": head,
            "author": {"login": "dimastov-lab"},
            "reviews": [{
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "voyn88-acceptance-gate[bot]"},
            }],
            "statusCheckRollup": [
                {"name": "CI", "conclusion": "SUCCESS"},
                {"name": "Acceptance gate (independent verdict on exact SHA)",
                 "conclusion": "FAILURE"},
            ],
        })
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    for _ in range(3):
        report = merge_once(app_factory, "/tmp")
        assert ("VOYN-W0-M1M", "acceptance_gate_rerun_ambiguous") in report.skipped
    report = merge_once(app_factory, "/tmp")
    assert ("VOYN-W0-M1M", "acceptance_gate_rerun_capped") in report.skipped
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE %s",
            ("VOYN-W0-M1M", f"gate_rerun:{head}:%"),
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT count(*) FROM backlog_evidence "
            "WHERE task_id=%s AND kind='acceptance' AND value LIKE %s",
            ("VOYN-W0-M1M", f"gate_rerun_fail:{head}:%"),
        )
        assert cur.fetchone()[0] == 0


def test_rerun_budget_is_per_head_across_duplicate_tasks(rig, monkeypatch):  # noqa: F811, E501
    """Verification finding on 6b74c6a: the cap was effectively
    per-(task, head), so duplicate READY_TO_REVIEW tasks sharing one PR
    (remediation chains do) could each spend `cap` reruns on the same
    head. The ledger is keyed by head alone: two tasks together still
    dispatch at most `cap` reruns."""
    app_factory, store, _ = rig
    pr = "https://github.com/x/y/pull/33"
    _ready(store, app_factory, "VOYN-W0-M1N", pr)
    _ready(store, app_factory, "VOYN-W0-M1O", pr)
    head = "e" * 40
    reruns = []
    monkeypatch.setattr(
        review_merge,
        "_rerun_failing_acceptance_gate",
        lambda repo, prq, sha: (reruns.append(sha), "dispatched")[1],
    )
    for task in ("VOYN-W0-M1N", "VOYN-W0-M1O"):
        for _ in range(3):
            review_merge._reconcile_stale_acceptance_gate(
                app_factory, "/tmp", task, pr, head, 3
            )
    assert len(reruns) == 3  # cap holds across BOTH tasks
