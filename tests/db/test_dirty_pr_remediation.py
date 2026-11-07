"""dirty_pr_remediation.remediate_dirty_prs (VOYN-W0-AICC-DIRTY-PR-REBASE-
REMEDIATION) on live PostgreSQL: the store side is real (READY_TO_REVIEW
tasks with pr evidence, the backlog_task_remediation lineage table), gh's
`pr view` is faked in-process by patching `review_merge._gh`, and the git
mechanics (`_attempt_local_merge`, `_remove_worktree`) plus `publish_run` are
faked by patching this module's own references to them -- covered against
real git separately in tests/orchestrator/test_dirty_pr_remediation_git.py.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import dirty_pr_remediation as dpr
from command_center.orchestrator import review_merge
from command_center.orchestrator.dirty_pr_remediation import (
    ConflictFile,
    DirtyPrRemediationConfig,
    MergeAttempt,
    remediate_dirty_prs,
)
from command_center.orchestrator.publish import PublishConfig, PublishResult
from tests.db.test_backlog_planner import (  # noqa: F401 — pytest fixtures
    _test_repo_routes,
    rig,
)

HEAD = "a" * 40


def _ready(store, factory, task_id, pr):
    """A task in READY_TO_REVIEW with a pr evidence row -- the state
    review_merge's publish half leaves behind, and this remediation's own
    entry condition."""
    from tests.db.test_backlog_planner import _task

    assert store.upsert_task(_task(task_id, repo="repo-x", status="OPEN"))[0]
    with factory() as c, c.cursor() as cur:
        def _rev():
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev()))
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task_id, _rev()))
        c.commit()


def _task_status(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT status, body FROM backlog_task WHERE task_id=%s", (task_id,))
        return cur.fetchone()


def _remediation_row(factory, parent_task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT task_id, pr_url, rejected_head_sha FROM backlog_task_remediation "
            "WHERE parent_task_id = %s",
            (parent_task_id,),
        )
        return cur.fetchone()


def _fake_gh(
    *, merge_state="DIRTY", head=HEAD, base="main", branch="pr-branch",
    additions=10, deletions=5, changed_files=2,
):
    """A `review_merge._gh` stand-in answering exactly the three distinct
    `pr view --json <fields>` shapes this module issues, keyed on which
    fields were requested -- the same shape real `gh` responses take, with
    only the fields asked for populated."""

    def fake(argv, repo):
        if argv[:2] == ["pr", "view"]:
            fields = argv[4] if len(argv) > 4 else ""
            data: dict = {"state": "OPEN"}
            if "mergeStateStatus" in fields:
                data["mergeStateStatus"] = merge_state
            if "headRefName" in fields:
                data.update(headRefName=branch, baseRefName=base, headRefOid=head)
            if "additions" in fields:
                data.update(additions=additions, deletions=deletions, changedFiles=changed_files)
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        return subprocess.CompletedProcess(argv, 1, "", "?")

    return fake


def _publish_cfg():
    return PublishConfig(
        lease_tool="/bin/true",
        repository="ai-command-center",
        owner="server-worker",
        session="s1",
        task="placeholder",
        deploy_key="/dev/null",
    )


def test_remediate_heals_a_cleanly_mergeable_dirty_pr(rig, monkeypatch, tmp_path):  # noqa: F811
    """A DIRTY PR that merges cleanly is published (guarded) from the
    merge-attempt worktree, and only THEN is the worktree removed -- publish
    needs the merged commit still checked out there to push it, so the
    ordering itself (not just that both happened) is asserted."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D1", "https://github.com/x/y/pull/1")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="DIRTY"))

    worktree = tmp_path / "wt1"
    worktree.mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_attempt(*args, **kwargs):
        return MergeAttempt(
            clean=True, old_head_sha=HEAD, new_head_sha="f" * 40,
            worktree_path=str(worktree),
        )

    def fake_publish(repo_path, cfg):
        calls.append(("publish", str(repo_path), cfg.task, cfg.remote_sha, cfg.remote_sha_known))
        return PublishResult(ok=True, head_sha="f" * 40)

    def fake_remove(repo_path, worktree_path, timeout):
        calls.append(("remove", worktree_path))

    monkeypatch.setattr(dpr, "_attempt_local_merge", fake_attempt)
    monkeypatch.setattr(dpr, "publish_run", fake_publish)
    monkeypatch.setattr(dpr, "_remove_worktree", fake_remove)

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert ("VOYN-W0-D1", "f" * 40) in report.healed
    assert len(calls) == 2
    assert calls[0][0] == "publish"
    assert calls[1] == ("remove", str(worktree))
    # The guard is against a concurrent writer racing this exact PR: pinned
    # to the pre-merge head, known.
    assert calls[0][2] == "VOYN-W0-D1"
    assert calls[0][3] == HEAD
    assert calls[0][4] is True


def test_remediate_skips_and_still_cleans_up_worktree_on_guarded_publish_failure(
    rig, monkeypatch, tmp_path  # noqa: F811
):  # noqa: F811
    """A guarded-publish refusal (e.g. a lease race) must leave the PR
    skipped -- never silently dropped -- and the worktree is still removed."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D2", "https://github.com/x/y/pull/2")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="DIRTY"))

    worktree = tmp_path / "wt2"
    worktree.mkdir()
    removed = []

    monkeypatch.setattr(
        dpr, "_attempt_local_merge",
        lambda *a, **k: MergeAttempt(
            clean=True, old_head_sha=HEAD, new_head_sha="f" * 40, worktree_path=str(worktree)
        ),
    )
    monkeypatch.setattr(
        dpr, "publish_run",
        lambda repo_path, cfg: PublishResult(ok=False, reason="lease_unavailable: x"),
    )
    monkeypatch.setattr(dpr, "_remove_worktree", lambda repo, path, timeout: removed.append(path))

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert ("VOYN-W0-D2", "guarded_publish_failed: lease_unavailable: x") in report.skipped
    assert not report.healed
    assert removed == [str(worktree)]


def test_remediate_dispatches_a_scoped_rebase_task_for_a_conflict(
    rig, monkeypatch, tmp_path  # noqa: F811
):  # noqa: F811
    """A genuine conflict never auto-resolves: a new, linked, scoped task is
    dispatched carrying the conflicting file's content, the parent task and
    its status are left completely untouched, and the worktree is cleaned
    up -- the primary path this feature exists to handle, and the exact
    path a prior review found leaking a worktree on every visit."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D3", "https://github.com/x/y/pull/3")
    before_status, before_body = _task_status(app_factory, "VOYN-W0-D3")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="DIRTY", additions=10, deletions=5, changed_files=2))

    worktree = tmp_path / "wt3"
    worktree.mkdir()
    removed = []
    conflict = ConflictFile(path="src/x.py", ours="ours content", theirs="theirs content")

    monkeypatch.setattr(
        dpr, "_attempt_local_merge",
        lambda *a, **k: MergeAttempt(
            clean=False, old_head_sha=HEAD, worktree_path=str(worktree), conflicts=(conflict,)
        ),
    )
    monkeypatch.setattr(dpr, "_remove_worktree", lambda repo, path, timeout: removed.append(path))

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert removed == [str(worktree)]
    assert not report.healed
    assert not report.deferred
    assert len(report.rebase_dispatched) == 1
    parent_task_id, new_task_id = report.rebase_dispatched[0]
    assert parent_task_id == "VOYN-W0-D3"
    assert new_task_id == "VOYN-W0-D3-REBASE"

    row = _remediation_row(app_factory, "VOYN-W0-D3")
    assert row == (new_task_id, "https://github.com/x/y/pull/3", HEAD)

    after_status, after_body = _task_status(app_factory, "VOYN-W0-D3")
    assert after_status == before_status == "READY_TO_REVIEW"
    assert after_body == before_body

    new_status, new_body = _task_status(app_factory, new_task_id)
    assert new_status == "OPEN"
    assert "src/x.py" in new_body
    assert "ours content" in new_body
    assert "theirs content" in new_body


def test_remediate_routes_a_huge_stale_conflict_to_triage_instead_of_a_rebase_task(
    rig, monkeypatch, tmp_path  # noqa: F811
):  # noqa: F811
    """A conflicting PR at/above the size thresholds gets a triage-only
    follow-up ("still wanted vs superseded?") instead of a scoped rebase
    task -- the module's own motivating example (#384: +6697/-373, 56
    files) is far too large to safely hand a writer a full conflict dump."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D4", "https://github.com/x/y/pull/4")
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh(merge_state="DIRTY", additions=6697, deletions=373, changed_files=56),
    )

    worktree = tmp_path / "wt4"
    worktree.mkdir()
    conflict = ConflictFile(path="huge.py", ours="a" * 10, theirs="b" * 10)
    monkeypatch.setattr(
        dpr, "_attempt_local_merge",
        lambda *a, **k: MergeAttempt(
            clean=False, old_head_sha=HEAD, worktree_path=str(worktree), conflicts=(conflict,)
        ),
    )
    monkeypatch.setattr(dpr, "_remove_worktree", lambda repo, path, timeout: None)

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert not report.rebase_dispatched
    assert len(report.deferred) == 1
    parent_task_id, new_task_id = report.deferred[0]
    assert parent_task_id == "VOYN-W0-D4"
    new_status, new_body = _task_status(app_factory, new_task_id)
    assert new_status == "OPEN"
    assert "still wanted vs superseded?" in new_body
    assert "6697" in new_body and "373" in new_body and "56" in new_body


def test_remediate_skips_a_pr_with_a_remediation_already_dispatched(rig, monkeypatch, tmp_path):  # noqa: F811, E501
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D5", "https://github.com/x/y/pull/5")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="DIRTY"))
    store.upsert_task(
        __import__("tests.db.test_backlog_planner", fromlist=["_task"])._task(
            "VOYN-W0-D5-REBASE", repo="repo-x", status="OPEN"
        )
    )
    ok, reason = store.record_remediation(
        "VOYN-W0-D5-REBASE", "VOYN-W0-D5", "https://github.com/x/y/pull/5", HEAD
    )
    assert ok, reason

    attempted = []
    monkeypatch.setattr(dpr, "_attempt_local_merge", lambda *a, **k: attempted.append(1))

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert ("VOYN-W0-D5", "remediation_already_dispatched") in report.skipped
    assert not attempted


def test_remediate_ignores_a_pr_that_is_not_dirty(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D6", "https://github.com/x/y/pull/6")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="CLEAN"))
    attempted = []
    monkeypatch.setattr(dpr, "_attempt_local_merge", lambda *a, **k: attempted.append(1))

    report = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg())

    assert not attempted
    assert not report.healed and not report.rebase_dispatched and not report.deferred


def test_remediate_cap_never_marks_a_row_processed_without_doing_work_on_it(
    rig, monkeypatch, tmp_path  # noqa: F811
):  # noqa: F811
    """The per-tick attempt cap must be checked BEFORE the scan cursor's
    last-processed row is recorded: a row this tick did zero work on must
    never be advanced past, or it is permanently excluded from every future
    scan the moment the cursor commits (the exact regression this guards).
    With `max_attempts_per_tick=1` and two DIRTY PRs, only the first is
    touched this tick -- the second must still be found (and touched) on
    the very next tick, not silently skipped forever."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-D7A", "https://github.com/x/y/pull/71")
    _ready(store, app_factory, "VOYN-W0-D7B", "https://github.com/x/y/pull/72")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(merge_state="DIRTY"))

    worktree_a = tmp_path / "wta"
    worktree_a.mkdir()
    worktree_b = tmp_path / "wtb"
    worktree_b.mkdir()
    seen = []

    def fake_attempt(repo_path, branch, base, old_head_sha, **kwargs):
        seen.append(old_head_sha)
        wt = worktree_a if len(seen) == 1 else worktree_b
        return MergeAttempt(clean=True, old_head_sha=old_head_sha, new_head_sha="f" * 40, worktree_path=str(wt))

    monkeypatch.setattr(dpr, "_attempt_local_merge", fake_attempt)
    monkeypatch.setattr(dpr, "publish_run", lambda repo_path, cfg: PublishResult(ok=True, head_sha="f" * 40))
    monkeypatch.setattr(dpr, "_remove_worktree", lambda repo, path, timeout: None)

    cfg = DirtyPrRemediationConfig(max_attempts_per_tick=1)
    first = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg(), cfg)
    assert len(first.healed) == 1
    healed_first = {task_id for task_id, _ in first.healed}

    second = remediate_dirty_prs(app_factory, "/tmp", _publish_cfg(), cfg)
    assert len(second.healed) == 1
    healed_second = {task_id for task_id, _ in second.healed}

    # Between the two ticks, BOTH tasks were healed -- the cap-limited task
    # was picked up again on the very next tick rather than vanishing.
    assert healed_first | healed_second == {"VOYN-W0-D7A", "VOYN-W0-D7B"}
    assert healed_first != healed_second


def test_dispatch_conflict_task_reports_parent_vanished_distinct_from_already_dispatched(
    rig,  # noqa: F811
):  # noqa: F811
    app_factory, _store, _ = rig
    result = dpr._dispatch_conflict_task(
        app_factory, "VOYN-W0-NOPE", "https://github.com/x/y/pull/99", HEAD, (), defer_note=None
    )
    assert result.outcome == dpr._DispatchOutcome.PARENT_VANISHED
    assert result.new_task_id is None
