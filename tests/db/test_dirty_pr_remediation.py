"""Auto-rebase remediation for DIRTY PRs (VOYN-W0-AICC-DIRTY-PR-REBASE-
REMEDIATION), on live PostgreSQL for the orchestration half and a real git
repo (no DB needed) for the local-merge half.

``gh`` is faked in-process exactly the way ``tests/db/test_review_merge.py``
fakes it, since ``remediate_dirty_prs`` reuses ``review_merge``'s own
``_pr_is_mergeable``/``_merge_state`` gates -- the same DIRTY definition the
merge-train coordinator uses, not a reimplementation of it.
"""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import dirty_pr_remediation, review_merge
from command_center.orchestrator.dirty_pr_remediation import (
    DirtyRemediationConfig,
    MergeAttempt,
    _attempt_local_merge,
    remediate_dirty_prs,
)
from command_center.orchestrator.publish import PublishConfig, PublishResult
from tests.db.test_backlog_planner import rig  # noqa: F401 — pytest fixture
from tests.db.test_review_merge import _ready

HEAD = "b" * 40


def _pr_body(*, mergeable=True, dirty=True, additions=10, deletions=5, changed_files=2):
    return json.dumps({
        "state": "OPEN",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "mergeStateStatus": "DIRTY" if dirty else "CLEAN",
        "author": {"login": "writer-bot"},
        "reviews": (
            [{"body": f"ACCEPTANCE: ACCEPT {HEAD}",
              "author": {"login": "voyn88-acceptance-gate[bot]"}}]
            if mergeable else []
        ),
        "statusCheckRollup": (
            [{"name": "CI", "conclusion": "SUCCESS"}] if mergeable else []
        ),
        "additions": additions,
        "deletions": deletions,
        "changedFiles": changed_files,
    })


def _fake_gh(body):
    def fake(argv, repo):
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "?")
    return fake


PUBLISH_CFG = PublishConfig(
    lease_tool="voyn-lease", repository="repo-x", owner="server-worker",
    session="server-worker", task="", deploy_key="/dev/null",
)


# --------------------------------------------------------------------------
# Local merge attempt: real git, no database.
# --------------------------------------------------------------------------


def _git(argv, cwd):
    result = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{argv} failed: {result.stderr}"
    return result


def _init_upstream(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(["init", "-b", "main"], upstream)
    _git(["config", "user.email", "t@example.com"], upstream)
    _git(["config", "user.name", "t"], upstream)
    (upstream / "a.txt").write_text("base\n")
    (upstream / "b.txt").write_text("base\n")
    _git(["add", "."], upstream)
    _git(["commit", "-m", "initial"], upstream)
    return upstream


def _clone(tmp_path, upstream):
    clone = tmp_path / "clone"
    _git(["clone", str(upstream), str(clone)], tmp_path)
    _git(["config", "user.email", "t@example.com"], clone)
    _git(["config", "user.name", "t"], clone)
    return clone


def test_attempt_local_merge_clean_when_sides_touch_different_files(tmp_path):
    upstream = _init_upstream(tmp_path)
    clone = _clone(tmp_path, upstream)

    _git(["checkout", "-b", "backlog/T1"], upstream)
    (upstream / "a.txt").write_text("base\nfrom pr branch\n")
    _git(["commit", "-am", "pr change"], upstream)

    _git(["checkout", "main"], upstream)
    (upstream / "b.txt").write_text("base\nfrom main\n")
    _git(["commit", "-am", "main change"], upstream)

    attempt = _attempt_local_merge(str(clone), "main", "backlog/T1", DirtyRemediationConfig())
    try:
        assert attempt.ok is True
        assert attempt.clean is True
        assert attempt.head_sha and attempt.head_sha != attempt.old_head_sha
        assert attempt.worktree_path is not None
        assert (attempt.worktree_path / "a.txt").read_text() == "base\nfrom pr branch\n"
        assert (attempt.worktree_path / "b.txt").read_text() == "base\nfrom main\n"
    finally:
        if attempt.worktree_path is not None:
            dirty_pr_remediation._remove_worktree(str(clone), attempt.worktree_path)


def test_attempt_local_merge_reports_both_sides_of_a_real_conflict(tmp_path):
    upstream = _init_upstream(tmp_path)
    clone = _clone(tmp_path, upstream)

    _git(["checkout", "-b", "backlog/T2"], upstream)
    (upstream / "a.txt").write_text("base\nPR VERSION\n")
    _git(["commit", "-am", "pr change"], upstream)

    _git(["checkout", "main"], upstream)
    (upstream / "a.txt").write_text("base\nMAIN VERSION\n")
    _git(["commit", "-am", "main change"], upstream)

    attempt = _attempt_local_merge(str(clone), "main", "backlog/T2", DirtyRemediationConfig())
    assert attempt.ok is True
    assert attempt.clean is False
    assert attempt.worktree_path is None  # already cleaned up on the conflict path
    assert [path for path, _, _ in attempt.conflicts] == ["a.txt"]
    _, ours, theirs = attempt.conflicts[0]
    assert "PR VERSION" in ours
    assert "MAIN VERSION" in theirs

    # No merge state (in progress or otherwise) is left behind on the shared
    # clone -- only the disposable worktree (already removed) ever touched
    # the conflict.
    status = _git(["status", "--porcelain"], clone)
    assert status.stdout == ""


def test_attempt_local_merge_truncates_and_counts_omitted_files(tmp_path):
    upstream = _init_upstream(tmp_path)
    clone = _clone(tmp_path, upstream)

    _git(["checkout", "-b", "backlog/T3"], upstream)
    (upstream / "a.txt").write_text("PR\n" * 2000)
    _git(["commit", "-am", "pr change"], upstream)

    _git(["checkout", "main"], upstream)
    (upstream / "a.txt").write_text("MAIN\n" * 2000)
    _git(["commit", "-am", "main change"], upstream)

    cfg = DirtyRemediationConfig(max_conflict_snippet_chars=50)
    attempt = _attempt_local_merge(str(clone), "main", "backlog/T3", cfg)
    assert attempt.clean is False
    _, ours, _theirs = attempt.conflicts[0]
    assert ours.endswith("... (truncated)")
    assert len(ours) < 100


# --------------------------------------------------------------------------
# Orchestration: real Postgres, faked gh, faked local-merge/publish.
# --------------------------------------------------------------------------


def test_remediate_skips_a_pr_that_is_not_dirty(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR1", "https://github.com/x/y/pull/60")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body(dirty=False)))

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert not report.healed
    assert not report.rebase_dispatched
    assert not report.deferred
    assert ("VOYN-W0-DR1", "not_dirty: CLEAN") in report.skipped


def test_remediate_skips_a_pr_not_yet_accepted(rig, monkeypatch):  # noqa: F811
    """DIRTY is irrelevant until the PR has cleared the same accept+green
    gate merge_once itself requires -- otherwise this would spend a merge
    attempt on a PR still mid-review."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR2", "https://github.com/x/y/pull/61")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body(mergeable=False)))

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert not report.healed and not report.rebase_dispatched and not report.deferred
    assert report.skipped == [("VOYN-W0-DR2", "no_accept_marker_on_head")]


def test_remediate_heals_a_cleanly_mergeable_dirty_pr(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR3", "https://github.com/x/y/pull/62")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body()))

    scratch_marker = object()
    attempt = MergeAttempt(
        ok=True, clean=True, head_sha="c" * 40, base_sha="d" * 40,
        old_head_sha=HEAD, worktree_path=scratch_marker,
    )
    monkeypatch.setattr(
        dirty_pr_remediation, "_attempt_local_merge",
        lambda repo_path, base_ref, branch, cfg: attempt,
    )
    removed = []
    monkeypatch.setattr(
        dirty_pr_remediation, "_remove_worktree",
        lambda repo_path, path: removed.append(path),
    )
    published = []

    def fake_publish_run(worktree_path, cfg):
        published.append((worktree_path, cfg))
        return PublishResult(ok=True, branch="backlog/VOYN-W0-DR3", head_sha="c" * 40,
                              pr_url="https://github.com/x/y/pull/62")

    monkeypatch.setattr(dirty_pr_remediation, "publish_run", fake_publish_run)

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert report.healed == [("VOYN-W0-DR3", "c" * 40)]
    assert not report.rebase_dispatched and not report.deferred and not report.skipped
    assert len(published) == 1
    worktree_path, publish_cfg = published[0]
    assert worktree_path is scratch_marker
    assert publish_cfg.task == "VOYN-W0-DR3"
    assert publish_cfg.base == "main"
    assert publish_cfg.base_sha == "d" * 40
    assert publish_cfg.remote_sha == HEAD
    assert publish_cfg.remote_sha_known is True
    # The template's own identity fields pass through unchanged.
    assert publish_cfg.lease_tool == PUBLISH_CFG.lease_tool
    assert publish_cfg.deploy_key == PUBLISH_CFG.deploy_key
    assert removed == [scratch_marker]  # the worktree is always cleaned up


def test_remediate_reports_a_failed_guarded_publish_as_a_skip(rig, monkeypatch):  # noqa: F811
    """A clean local merge that fails to publish (lease held elsewhere, a
    quality-gate finding) must never be reported healed -- and the PR is
    left DIRTY-skipped for the next tick to retry, not dropped."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR4", "https://github.com/x/y/pull/63")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body()))
    monkeypatch.setattr(
        dirty_pr_remediation, "_attempt_local_merge",
        lambda repo_path, base_ref, branch, cfg: MergeAttempt(
            ok=True, clean=True, head_sha="c" * 40, base_sha="d" * 40,
            old_head_sha=HEAD, worktree_path=object(),
        ),
    )
    monkeypatch.setattr(dirty_pr_remediation, "_remove_worktree", lambda repo_path, path: None)
    monkeypatch.setattr(
        dirty_pr_remediation, "publish_run",
        lambda worktree_path, cfg: PublishResult(ok=False, reason="lease_unavailable: x"),
    )

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert not report.healed
    assert report.skipped == [
        ("VOYN-W0-DR4", "guarded_publish_failed: lease_unavailable: x")
    ]


def test_remediate_dispatches_a_scoped_rebase_task_for_a_small_conflict(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR5", "https://github.com/x/y/pull/64")
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh(_pr_body(additions=10, deletions=5, changed_files=2)),
    )
    monkeypatch.setattr(
        dirty_pr_remediation, "_attempt_local_merge",
        lambda repo_path, base_ref, branch, cfg: MergeAttempt(
            ok=True, clean=False, old_head_sha=HEAD, base_sha="d" * 40,
            conflicts=(("a.txt", "ours content", "theirs content"),),
        ),
    )

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert not report.healed and not report.deferred
    assert report.rebase_dispatched == [("VOYN-W0-DR5", "VOYN-W0-DR5-REBASE")]
    new_task = store.get_task("VOYN-W0-DR5-REBASE")
    assert new_task["status"] == "OPEN"
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM backlog_task WHERE task_id = %s", ("VOYN-W0-DR5-REBASE",)
        )
        body = cur.fetchone()[0]
    assert "a.txt" in body
    assert "ours content" in body
    assert "theirs content" in body
    assert "backlog/VOYN-W0-DR5" in body
    # The parent is untouched -- a merge conflict is not a rejection of the
    # accepted work, so it stays exactly where it was.
    assert store.get_task("VOYN-W0-DR5")["status"] == "READY_TO_REVIEW"
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parent_task_id, pr_url, rejected_head_sha "
            "FROM backlog_task_remediation WHERE task_id = %s",
            ("VOYN-W0-DR5-REBASE",),
        )
        row = cur.fetchone()
    assert row == ("VOYN-W0-DR5", "https://github.com/x/y/pull/64", HEAD)


def test_remediate_defers_a_huge_conflicting_pr_to_a_human(rig, monkeypatch):  # noqa: F811
    """A #384-shaped PR (+6697/-373 across 56 files) is exactly what
    ``defer_lines_threshold``/``defer_files_threshold`` route away from an
    automatic rebase guess."""
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR6", "https://github.com/x/y/pull/65")
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh(_pr_body(additions=6697, deletions=373, changed_files=56)),
    )
    monkeypatch.setattr(
        dirty_pr_remediation, "_attempt_local_merge",
        lambda repo_path, base_ref, branch, cfg: MergeAttempt(
            ok=True, clean=False, old_head_sha=HEAD, base_sha="d" * 40,
            conflicts=(("a.txt", "ours", "theirs"),),
        ),
    )

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)

    assert not report.healed and not report.rebase_dispatched
    assert report.deferred == [("VOYN-W0-DR6", "VOYN-W0-DR6-TRIAGE")]
    new_task = store.get_task("VOYN-W0-DR6-TRIAGE")
    assert new_task["status"] == "DEFER_TO_USER"
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM backlog_task WHERE task_id = %s", ("VOYN-W0-DR6-TRIAGE",)
        )
        body = cur.fetchone()[0]
    assert "still wanted" in body
    assert store.get_task("VOYN-W0-DR6")["status"] == "READY_TO_REVIEW"


def test_remediate_is_idempotent_once_a_remediation_is_dispatched(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-DR7", "https://github.com/x/y/pull/66")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body()))
    calls = []

    def fake_attempt(repo_path, base_ref, branch, cfg):
        calls.append(1)
        return MergeAttempt(
            ok=True, clean=False, old_head_sha=HEAD, base_sha="d" * 40,
            conflicts=(("a.txt", "ours", "theirs"),),
        )

    monkeypatch.setattr(dirty_pr_remediation, "_attempt_local_merge", fake_attempt)

    first = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)
    assert first.rebase_dispatched == [("VOYN-W0-DR7", "VOYN-W0-DR7-REBASE")]
    assert len(calls) == 1

    second = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG)
    assert not second.rebase_dispatched and not second.healed and not second.deferred
    assert ("VOYN-W0-DR7", "remediation_already_dispatched") in second.skipped
    assert len(calls) == 1  # no second merge attempt spent on an already-handled PR


def test_remediate_caps_attempts_per_tick(rig, monkeypatch):  # noqa: F811
    app_factory, store, _ = rig
    for i in range(4):
        _ready(store, app_factory, f"VOYN-W0-DRC{i}", f"https://github.com/x/y/pull/7{i}")
    monkeypatch.setattr(review_merge, "_gh", _fake_gh(_pr_body()))
    attempts = []

    def fake_attempt(repo_path, base_ref, branch, cfg):
        attempts.append(branch)
        return MergeAttempt(
            ok=True, clean=False, old_head_sha=HEAD, base_sha="d" * 40,
            conflicts=(("a.txt", "ours", "theirs"),),
        )

    monkeypatch.setattr(dirty_pr_remediation, "_attempt_local_merge", fake_attempt)
    cfg = DirtyRemediationConfig(max_attempts_per_tick=2)

    report = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG, cfg)

    assert len(attempts) == 2
    assert len(report.rebase_dispatched) == 2

    # The two PRs left untouched by the cap must not be silently dropped from
    # the scan cursor -- a second tick has to pick them up and finish the
    # job, not skip straight past them forever.
    second = remediate_dirty_prs(app_factory, "/tmp", PUBLISH_CFG, cfg)
    assert len(attempts) == 4
    assert len(second.rebase_dispatched) == 2
    assert {t for t, _ in report.rebase_dispatched} | {
        t for t, _ in second.rebase_dispatched
    } == {f"VOYN-W0-DRC{i}" for i in range(4)}
