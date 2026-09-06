"""Auto-rebase remediation for conflicting (DIRTY) PRs
(VOYN-W0-AICC-DIRTY-PR-REBASE-REMEDIATION).

``merge_once`` (review_merge.py, the merge-train coordinator) only ever
*flags* a DIRTY PR -- ``report.skipped.append((task_id, "branch_dirty_needs_
rebase"))`` -- and does nothing further: ``update-branch`` cannot resolve a
real conflict, so the safe thing was always to leave it alone. Nothing picked
that flag back up, so DIRTY PRs piled up (five live at once: #380, #384,
#388, #390, #391) with no path back to green short of a human noticing the
skip line and rebasing by hand.

This module is that path. Per tick, for every PR the coordinator would flag
DIRTY (same gates: an ACCEPT marker on the head, green required checks --
see ``review_merge._pr_is_mergeable`` -- and ``mergeStateStatus == "DIRTY"``,
see ``review_merge._merge_state``), it attempts one **merge** (never rebase)
of the base branch into the PR branch, in a disposable ``git worktree`` off
the routed clone -- never touching that clone's own checkout, never the PR
branch's working state until a push is actually warranted.

* Merges cleanly -> the new head is pushed onto the SAME PR branch through
  ``publish.publish_run`` (the "gate + guarded publish" the task names: its
  own non-executing static-quality and leak-guard gates, then a
  writer-lease-fenced, ``--force-with-lease``-safe push -- never a raw
  force-push, and the lease compares against the PR's own last-known head,
  so a race with someone else pushing to the branch refuses cleanly instead
  of clobbering). ``publish_run`` resolves the PR's already-open branch by
  name (``backlog/<task_id>``) rather than opening a new one, so this reads
  to GitHub as an ordinary new commit on the existing PR -- CI and review
  both re-run against the new head exactly as they would for any push.
* Conflicts for real -> never auto-resolved. A small, focused conflict gets
  a new linked task (``<task_id>-REBASE``) carrying the exact conflicting
  files and both sides' content, dispatched through the planner like any
  other OPEN task. A huge/stale PR (default: >2000 changed lines or >40
  changed files -- #384 was +6697/-373 across 56 files) or a conflict chain
  already several rebase attempts deep is routed to a human instead
  (``<task_id>-TRIAGE``, ``DEFER_TO_USER``, with a "still wanted vs
  superseded?" note) rather than spending another automatic guess on a PR
  that may not be worth rebasing at all.

Every outcome is logged on ``DirtyRemediationReport`` -- healed, a rebase
task dispatched, deferred to a human, or skipped (with why) -- so nothing
here can go silently missing the way the flag-only coordinator's skip did.
Attempts (local merges, and everything that follows one) are capped per
tick by ``DirtyRemediationConfig.max_attempts_per_tick``; examination is
capped by ``scan_cap``, and both use the exact same persisted-cursor scan
window ``merge_once`` does (``review_merge._scan_tasks``/``_scan_commit``),
so fairness across ticks holds the same guarantee documented there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from command_center.orchestrator import review_merge
from command_center.orchestrator.publish import PublishConfig, publish_run

__all__ = [
    "DirtyRemediationConfig",
    "DirtyRemediationReport",
    "MergeAttempt",
    "remediate_dirty_prs",
]


@dataclass(frozen=True, slots=True)
class DirtyRemediationConfig:
    #: Per-tick cap on local merge attempts (and whatever follows one: a
    #: guarded publish or a remediation-task dispatch). Bounded the same way
    #: ``ReviewConfig.max_branch_updates_per_tick`` is: a burst of newly-DIRTY
    #: PRs must not let one tick spend unbounded local git/gh work.
    max_attempts_per_tick: int = 3
    #: Per-tick cap on tasks EXAMINED -- see ``review_merge._scan_tasks``'s
    #: window-fairness rationale, reused verbatim here.
    scan_cap: int = 40
    #: A conflicting PR at or above either threshold is routed to
    #: DEFER_TO_USER triage instead of a scoped rebase task -- a guess at
    #: resolving 56 files across +6697/-373 lines is not a "scoped" rebase,
    #: it is asking the automation to redo the review's job blind.
    defer_lines_threshold: int = 2000
    defer_files_threshold: int = 40
    #: How many conflicting files get full ours/theirs content in a
    #: dispatched task body before the rest are just named and counted.
    max_conflict_files_in_body: int = 20
    #: Per-side content is truncated past this many characters -- a task
    #: body is a prompt, not a paste bin.
    max_conflict_snippet_chars: int = 4000
    #: How many links may already stand above a task before a further
    #: conflict is routed to a human instead of another rebase task --
    #: same rationale as ``review_merge.MAX_REMEDIATION_DEPTH``, kept as an
    #: independent knob because this is a different lineage chain (rebase
    #: attempts, not review rejections).
    max_remediation_depth: int = 3


@dataclass
class DirtyRemediationReport:
    #: (task_id, new_head_sha) -- merged cleanly and pushed onto the PR's
    #: own branch; CI and review re-run against the new head on their own.
    healed: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, new_task_id) -- a real conflict, small enough to hand to a
    #: writer as a scoped rebase task.
    rebase_dispatched: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, new_task_id) -- a real conflict, but huge/stale or already
    #: several rebase attempts deep: routed to a human instead.
    deferred: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, reason) -- examined and deliberately left alone (not
    #: DIRTY, not yet accepted, already remediated, a transient failure).
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    #: Whether the attempt itself completed (fetch, worktree, merge all
    #: ran) -- independent of whether the merge was clean. False means a
    #: transient/tooling failure; `reason` says which.
    ok: bool
    clean: bool
    #: The new merge-commit sha, when `clean`.
    head_sha: str = ""
    #: The resolved `origin/<base>` sha that was merged in.
    base_sha: str = ""
    #: The PR branch's tip before merging -- echoes the caller's own
    #: `gh pr view` reading back, for the force-with-lease compare-and-swap
    #: `publish_run` performs before it pushes.
    old_head_sha: str = ""
    #: (path, ours, theirs) for each conflicting file, only when not clean.
    conflicts: tuple[tuple[str, str, str], ...] = ()
    #: The disposable worktree holding the clean merge commit, kept around
    #: (not yet removed) so the caller can run `publish_run` against it --
    #: only set when `clean`. The caller owns cleanup via `_remove_worktree`.
    worktree_path: Path | None = None
    reason: str = ""


def _git(argv: list[str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False, timeout=120,
    )


def _remove_worktree(repo_path: str, scratch: Path) -> None:
    removed = _git(["worktree", "remove", "--force", str(scratch)], repo_path)
    if removed.returncode != 0:
        # The worktree add/merge sequence never leaves anything precious
        # behind (it is either a clean checkout, an aborted merge, or a
        # merge commit already handed to the caller) -- best-effort manual
        # cleanup rather than leaking a scratch directory and a stale
        # `git worktree list` entry forever.
        shutil.rmtree(scratch, ignore_errors=True)
        _git(["worktree", "prune"], repo_path)


def _conflict_side(scratch: Path, stage_ref: str, cfg: DirtyRemediationConfig) -> str:
    """The content of one side of a conflicted file, read from the merge's
    unresolved index (stage 2 = ours/the PR branch, stage 3 = theirs/the
    base branch) while the merge is still in progress, before it is
    aborted. A missing stage (an add/add or modify/delete conflict) is
    reported as absence, not a crash."""
    shown = _git(["show", stage_ref], scratch)
    if shown.returncode != 0:
        return "(absent on this side -- the file was added or deleted here)"
    text = shown.stdout
    if len(text) > cfg.max_conflict_snippet_chars:
        return text[: cfg.max_conflict_snippet_chars] + "\n... (truncated)"
    return text


def _attempt_local_merge(
    repo_path: str, base_ref: str, branch: str, cfg: DirtyRemediationConfig
) -> MergeAttempt:
    """Merge (never rebase) `origin/<base_ref>` into `origin/<branch>` in a
    disposable worktree off `repo_path`.

    Explicit refspecs on the fetch -- not a bare `git fetch origin` -- so
    this does not depend on `repo_path`'s configured fetch refspec covering
    an arbitrary branch; the remote-tracking refs it reads afterward are
    guaranteed current no matter how that clone is configured.
    """
    fetched = _git(
        [
            "fetch", "origin",
            f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        repo_path,
    )
    if fetched.returncode != 0:
        return MergeAttempt(
            ok=False, clean=False, reason=f"fetch_failed: {fetched.stderr.strip()[:160]}"
        )

    old_head = _git(["rev-parse", f"refs/remotes/origin/{branch}"], repo_path)
    base_sha = _git(["rev-parse", f"refs/remotes/origin/{base_ref}"], repo_path)
    if old_head.returncode != 0 or base_sha.returncode != 0:
        return MergeAttempt(ok=False, clean=False, reason="cannot_resolve_fetched_refs")
    old_head_sha = old_head.stdout.strip()
    base_sha_value = base_sha.stdout.strip()

    scratch = Path(tempfile.mkdtemp(prefix="dirty-pr-remediation-"))
    added = _git(
        ["worktree", "add", "--detach", str(scratch), f"refs/remotes/origin/{branch}"],
        repo_path,
    )
    if added.returncode != 0:
        _remove_worktree(repo_path, scratch)
        return MergeAttempt(
            ok=False, clean=False, old_head_sha=old_head_sha, base_sha=base_sha_value,
            reason=f"worktree_add_failed: {added.stderr.strip()[:160]}",
        )

    merged = _git(["merge", "--no-edit", f"refs/remotes/origin/{base_ref}"], scratch)
    if merged.returncode == 0:
        head = _git(["rev-parse", "HEAD"], scratch)
        if head.returncode != 0:
            _remove_worktree(repo_path, scratch)
            return MergeAttempt(
                ok=False, clean=False, old_head_sha=old_head_sha, base_sha=base_sha_value,
                reason="cannot_read_merge_head",
            )
        return MergeAttempt(
            ok=True, clean=True, head_sha=head.stdout.strip(), base_sha=base_sha_value,
            old_head_sha=old_head_sha, worktree_path=scratch,
        )

    listed = _git(["diff", "--name-only", "--diff-filter=U"], scratch)
    paths = [line for line in listed.stdout.splitlines() if line.strip()]
    if not paths:
        # A nonzero merge exit with no unmerged paths is not a content
        # conflict -- a hook, a corrupt object, disk pressure. Report it as
        # a failed attempt rather than guessing it was a conflict with
        # nothing to show for it.
        _git(["merge", "--abort"], scratch)
        _remove_worktree(repo_path, scratch)
        return MergeAttempt(
            ok=False, clean=False, old_head_sha=old_head_sha, base_sha=base_sha_value,
            reason=f"merge_failed_without_conflicts: {merged.stderr.strip()[:160]}",
        )

    conflicts = tuple(
        (path, _conflict_side(scratch, f":2:{path}", cfg), _conflict_side(scratch, f":3:{path}", cfg))
        for path in paths[: cfg.max_conflict_files_in_body]
    )
    omitted = len(paths) - len(conflicts)
    _git(["merge", "--abort"], scratch)
    _remove_worktree(repo_path, scratch)
    return MergeAttempt(
        ok=True, clean=False, old_head_sha=old_head_sha, base_sha=base_sha_value,
        conflicts=conflicts,
        reason=(f"{omitted} more conflicting file(s) omitted" if omitted else ""),
    )


def _pr_stats(repo_path: str, pr_url: str) -> dict[str, Any] | None:
    view = review_merge._gh(
        [
            "pr", "view", pr_url, "--json",
            "state,headRefOid,baseRefName,additions,deletions,changedFiles",
        ],
        repo_path,
    )
    if view.returncode != 0:
        return None
    try:
        data = json.loads(view.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("state") != "OPEN":
        return None
    return data


def _format_conflicts(attempt: MergeAttempt, branch: str, base_ref: str) -> str:
    header = f"Conflicting files ({len(attempt.conflicts)}"
    if attempt.reason:
        header += f"; {attempt.reason}"
    header += "):\n"
    parts = [header]
    for path, ours, theirs in attempt.conflicts:
        parts.append(
            f"\n#### `{path}`\n\n"
            f"**Ours** (`{branch}`):\n```\n{ours}\n```\n\n"
            f"**Theirs** (`{base_ref}`):\n```\n{theirs}\n```\n"
        )
    return "".join(parts)


def _dispatch_conflict_task(
    factory: Any,
    task_id: str,
    pr_url: str,
    head_sha: str,
    base_ref: str,
    branch: str,
    attempt: MergeAttempt,
    huge: bool,
    depth_cap: int,
) -> tuple[str, str] | None:
    """Create the linked follow-up task for a genuine merge conflict: a
    scoped rebase task carrying the exact conflicting files and both sides,
    or -- huge/stale PR, or the rebase chain already this deep -- a
    DEFER_TO_USER triage task instead. Returns ``(kind, new_task_id)``
    where kind is ``"rebase"`` or ``"deferred"``, or None if a concurrent
    tick already dispatched a remediation for this exact parent (the same
    idempotency check the caller already made, re-checked inside the
    transaction against a race).

    Reuses ``backlog_task_remediation`` -- the review-reject lineage table
    -- for this different relationship on purpose: the shape is identical
    (a parent task, a follow-up task, the PR and head sha that prompted it)
    and a new migration/table for the same three columns would be
    duplication, not distinction. The parent is deliberately left exactly
    as it is (still READY_TO_REVIEW, still visible to `merge_once` as
    `branch_dirty_needs_rebase` every tick until the conflict is actually
    resolved) -- unlike a REJECT, a merge conflict says nothing about
    whether the accepted work was right, so nothing about the parent
    changes here.
    """
    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore

    with factory() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM backlog_task_remediation WHERE parent_task_id = %s",
                    (task_id,),
                )
                if cur.fetchone() is not None:
                    conn.rollback()
                    return None
                depth = review_merge._remediation_depth(cur, task_id)
                cur.execute(
                    "SELECT wave, priority, title, body, repo "
                    "FROM backlog_task WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return None
                wave, priority, title, body, repo = row

            conflict_section = _format_conflicts(attempt, branch, base_ref)
            store = BacklogStore(lambda: nullcontext(conn))

            if huge or depth + 1 > depth_cap:
                kind = "deferred"
                new_task_id = f"{task_id}-TRIAGE"
                new_title = f"Triage: {title} -- still wanted vs superseded?"
                why = (
                    "large/stale PR"
                    if huge
                    else f"rebase chain already at depth {depth} (limit {depth_cap})"
                )
                new_body = (
                    f"{body}\n\n---\n"
                    f"{pr_url} at {head_sha} conflicts with `{base_ref}` and was routed to "
                    f"a human instead of another automatic rebase attempt: {why}.\n\n"
                    f"{conflict_section}\n\n"
                    "Decide: still wanted (rebase it -- by hand, or by opening a scoped "
                    "rebase task yourself) or superseded (close the PR and mark this "
                    "task REJECTED)."
                )
                new_status = "DEFER_TO_USER"
            else:
                kind = "rebase"
                new_task_id = f"{task_id}-REBASE"
                new_title = f"Rebase: {title}"
                new_body = (
                    f"{body}\n\n---\n"
                    f"{pr_url} at {head_sha} no longer merges cleanly into `{base_ref}` "
                    "(GitHub reports it DIRTY). This is not a content rejection -- the "
                    "review that already accepted this work stands as-is. Fetch the "
                    f"latest `{base_ref}`, resolve the conflicts below on branch "
                    f"`{branch}` (or a fresh branch off it), and push -- the existing "
                    "PR picks up the new head automatically.\n\n"
                    f"{conflict_section}"
                )
                new_status = "OPEN"

            ok, _reason, _changed = store.upsert_task(
                ParsedTask(
                    task_id=new_task_id, wave=wave, priority=priority,
                    status=new_status, kind="task", title=new_title, body=new_body,
                    repo=repo, line_no=0,
                )
            )
            if not ok:
                conn.rollback()
                return None
            ok, _reason = store.record_remediation(new_task_id, task_id, pr_url, head_sha)
            if not ok:
                conn.rollback()
                return None
            conn.commit()
            return kind, new_task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


def remediate_dirty_prs(
    factory: Any,
    repo_path: str,
    publish_cfg_template: PublishConfig,
    cfg: DirtyRemediationConfig | None = None,
) -> DirtyRemediationReport:
    """One remediation tick over every DIRTY PR the merge-train coordinator
    would otherwise just flag and leave.

    `publish_cfg_template` supplies the writer-lease identity (lease tool,
    repository, owner, session, deploy key) a clean-merge push needs --
    `task`/`base`/`base_sha`/`remote_sha`/`remote_sha_known` are overridden
    per PR via `dataclasses.replace` and must not be relied on from the
    template.
    """
    cfg = cfg or DirtyRemediationConfig()
    report = DirtyRemediationReport()
    tasks, scan_token = review_merge._scan_tasks(
        factory,
        "scan:dirty_pr_remediation",
        "SELECT t.task_id, e.value FROM backlog_task t "
        "JOIN backlog_evidence e ON e.task_id = t.task_id AND e.kind = 'pr' "
        "WHERE t.status = 'READY_TO_REVIEW' "
        "AND (t.task_id, e.value) > (%s, %s) "
        "ORDER BY t.task_id, e.value LIMIT %s",
        (), cfg.scan_cap,
    )
    last_processed = None
    attempts = 0
    for task_id, pr_url in tasks:
        last_processed = (task_id, pr_url)
        if attempts >= cfg.max_attempts_per_tick:
            break

        # Same readiness gate `merge_once` applies before it would ever
        # report `branch_dirty_needs_rebase`: an unaccepted or failing PR
        # is review_once's/merge_once's business, not this remediation's --
        # spending a local merge attempt on it would be wasted work on a PR
        # that may never even reach DIRTY-relevance.
        ready, detail = review_merge._pr_is_mergeable(repo_path, pr_url)
        if not ready:
            report.skipped.append((task_id, detail))
            continue
        state = review_merge._merge_state(repo_path, pr_url)
        if state != "DIRTY":
            # CLEAN lands through merge_once directly; BEHIND fast-forwards
            # there too -- neither is this remediation's concern.
            report.skipped.append((task_id, f"not_dirty: {state or 'unknown'}"))
            continue

        existing = review_merge._rows(
            factory,
            "SELECT 1 FROM backlog_task_remediation WHERE parent_task_id = %s",
            (task_id,),
        )
        if existing:
            report.skipped.append((task_id, "remediation_already_dispatched"))
            continue

        attempts += 1
        stats = _pr_stats(repo_path, pr_url)
        if stats is None:
            report.skipped.append((task_id, "pr_view_failed"))
            continue
        base_ref = str(stats.get("baseRefName") or publish_cfg_template.base)
        branch = review_merge._task_branch(task_id)

        attempt = _attempt_local_merge(repo_path, base_ref, branch, cfg)
        if not attempt.ok:
            report.skipped.append((task_id, attempt.reason or "merge_attempt_failed"))
            continue

        if attempt.clean:
            assert attempt.worktree_path is not None  # set whenever clean=True
            try:
                publish_cfg = replace(
                    publish_cfg_template,
                    task=task_id, base=base_ref, base_sha=attempt.base_sha,
                    remote_sha=attempt.old_head_sha, remote_sha_known=True,
                )
                result = publish_run(attempt.worktree_path, publish_cfg)
            finally:
                _remove_worktree(repo_path, attempt.worktree_path)
            if result.ok:
                report.healed.append((task_id, result.head_sha or attempt.head_sha))
            else:
                report.skipped.append((task_id, f"guarded_publish_failed: {result.reason}"))
            continue

        additions = int(stats.get("additions") or 0)
        deletions = int(stats.get("deletions") or 0)
        changed_files = int(stats.get("changedFiles") or 0)
        huge = (
            additions + deletions > cfg.defer_lines_threshold
            or changed_files > cfg.defer_files_threshold
        )
        dispatched = _dispatch_conflict_task(
            factory, task_id, pr_url, attempt.old_head_sha, base_ref, branch,
            attempt, huge, cfg.max_remediation_depth,
        )
        if dispatched is None:
            report.skipped.append((task_id, "remediation_already_dispatched"))
            continue
        kind, new_task_id = dispatched
        if kind == "deferred":
            report.deferred.append((task_id, new_task_id))
        else:
            report.rebase_dispatched.append((task_id, new_task_id))

    review_merge._scan_commit(factory, "scan:dirty_pr_remediation", scan_token, last_processed)
    return report
