"""Auto-heal DIRTY pull requests the merge-train coordinator only flags
(VOYN-W0-AICC-DIRTY-PR-REBASE-REMEDIATION).

``merge_once`` (review_merge.py) already refuses to touch a PR whose
``mergeStateStatus`` is ``DIRTY``: a real conflict with the base branch that
only a rebase (or an equivalent merge) can resolve, and forcing one through
there was explicitly out of scope for that loop. The result, live: DIRTY
PRs just pile up (#380, #384, #388, #390, #391 at the time this was
written) with nothing ever acting on the flag.

This module is that action, run as its own bounded timer tick,
``remediate_dirty_prs``. For every READY_TO_REVIEW task whose PR is DIRTY
and has no remediation dispatched for it yet:

1. Attempt an automatic ``git merge origin/<base>`` -- never a rebase, so
   nothing here ever needs a force-push -- onto the PR branch, entirely
   inside a throwaway ``git worktree`` (``_attempt_local_merge``). The
   caller's own checkout of ``repo_path`` is never touched.
2. If it merges CLEANLY: gate + guarded publish. ``publish_run`` pushes the
   merge commit back onto the PR's own branch, pinned against the PR's
   pre-merge head via ``remote_sha``/``remote_sha_known`` -- a concurrent
   push to the same branch between our fetch and our push is refused, never
   silently overwritten. The new head re-runs CI and review exactly like any
   other push (no special-casing needed: it is just a new commit on the same
   branch).
3. If it CONFLICTS: nothing here ever auto-resolves a conflict. A scoped
   remediation task is dispatched carrying the exact conflicting files and
   both sides' content (``_dispatch_conflict_task``) -- the same
   parent/child linkage ``review_merge._remediate_rejection`` uses
   (``backlog_task_remediation``), so the original task and its PR are left
   exactly as they are, and the same remediation-depth cap
   (``review_merge.MAX_REMEDIATION_DEPTH``) stops an endless chain. A PR
   large or stale enough that a scoped rebase task cannot safely reason
   about it (module-configurable; the design's own example is PR #384's
   +6697/-373 across 56 files) instead gets a triage-only follow-up task
   asking a human "still wanted vs superseded?" rather than a task that
   would have to swallow the whole diff.

Every outcome -- healed, a rebase task dispatched, a triage task dispatched,
or skipped -- is reported (`DirtyPrRemediationReport`) and, for the
dispatch paths, recorded durably in ``backlog_task_remediation`` before this
function ever reports success; nothing here silently drops a PR. Attempts
(real merge attempts, not cheap per-task examinations) are capped per tick
by `DirtyPrRemediationConfig.max_attempts_per_tick`; the scan cursor
(``scan:dirty_pr_remediation``) only ever advances past rows this tick
actually looked at -- a row that would exceed the cap is left for the next
tick, never counted as processed (see the cap-ordering note in
``remediate_dirty_prs``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from command_center.orchestrator import review_merge
from command_center.orchestrator.publish import PublishConfig, publish_run

__all__ = [
    "ConflictFile",
    "DirtyPrRemediationConfig",
    "DirtyPrRemediationReport",
    "MergeAttempt",
    "remediate_dirty_prs",
]

#: Matches `_gh`'s own bound in review_merge.py (120s) -- a slow fetch/merge/
#: worktree add on a huge, stale PR is exactly the case this module targets,
#: so the bound must be generous, but it must still be a bound: an unbounded
#: `subprocess.run` here would hang the whole tick on one PR.
_DEFAULT_GIT_TIMEOUT_SECONDS = 120

#: `git status --porcelain=v1` codes for an unresolved conflict path, across
#: every combination `git merge` can leave: both sides modified (UU), both
#: added (AA), both deleted (DD), and the four one-side-deleted variants.
_UNMERGED_STATUS_CODES = frozenset({"UU", "AA", "DD", "AU", "UA", "DU", "UD"})

_CONFLICT_SNIPPET_SUFFIX = "\n... (truncated)"


@dataclass(frozen=True, slots=True)
class DirtyPrRemediationConfig:
    #: Per-tick cap on REAL merge attempts (a worktree is created, `git
    #: merge` actually runs). Cheap per-task examinations (the mergeStateStatus
    #: lookup, the existing-remediation check) are bounded by `scan_cap`
    #: instead, the same split `review_merge.merge_once` uses between its
    #: `scan_cap` and `max_per_tick`.
    max_attempts_per_tick: int = 5
    #: Per-tick cap on tasks EXAMINED. See the window-starvation rationale in
    #: `review_merge.merge_once`'s own docstring -- unbounded examination
    #: costs unbounded gh API traffic regardless of the attempt cap above.
    scan_cap: int = 40
    #: Bound on every `git` subprocess this module runs (fetch, worktree add,
    #: merge, show, status, abort, remove). A `subprocess.TimeoutExpired` is
    #: caught at the call site (`_git`) and turned into a normal failed
    #: `CompletedProcess`, never an uncaught exception that would abort every
    #: other PR still waiting in this tick.
    git_timeout_seconds: int = _DEFAULT_GIT_TIMEOUT_SECONDS
    #: A conflicting file's content on either side is truncated past this
    #: many characters before it goes into a dispatched task body -- a task
    #: body is not the place for an entire multi-megabyte generated file.
    conflict_snippet_max_chars: int = 4000
    #: A DIRTY PR at or above either threshold (additions+deletions, or
    #: files changed) is routed to DEFER_TO_USER-style triage instead of a
    #: scoped rebase task: past this size, "list the conflicting files and
    #: both sides" stops being a task a writer can safely act on unattended,
    #: and the right question is whether the PR is still wanted at all.
    #: PR #384 (+6697/-373 across 56 files) is the design's own motivating
    #: example and clears both thresholds by a wide margin.
    huge_pr_lines_threshold: int = 1500
    huge_pr_files_threshold: int = 30
    #: Parent directory for the throwaway merge-attempt worktrees. None uses
    #: the platform temp directory.
    worktree_root: str | None = None


@dataclass
class DirtyPrRemediationReport:
    #: (task_id, new_head_sha) -- merged cleanly and published (guarded).
    healed: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, new_task_id) -- a genuine conflict, scoped rebase task
    #: dispatched carrying the conflicting files and both sides' content.
    rebase_dispatched: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, new_task_id) -- conflicting AND huge/stale: routed to a
    #: triage-only follow-up task instead ("still wanted vs superseded?").
    deferred: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, reason) -- examined but no action taken this tick.
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ConflictFile:
    path: str
    #: The PR branch's content at this path, or None if the path is absent
    #: on this side (e.g. deleted-by-us / add-only-on-theirs).
    ours: str | None
    #: origin/<base>'s content at this path, or None if absent on this side.
    theirs: str | None
    #: True if either side's blob failed UTF-8 decoding -- a binary file is
    #: never dumped into a task body; both `ours`/`theirs` are None instead.
    binary: bool = False
    #: True if either side's content was cut at `conflict_snippet_max_chars`.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    clean: bool
    old_head_sha: str
    #: Set only when `clean` is True: the merge commit's own sha.
    new_head_sha: str | None = None
    #: Set whenever a worktree was actually created on disk, on EVERY
    #: outcome (clean, conflicting, or a post-worktree error) -- the caller
    #: MUST remove it in all three cases; only a failure before the
    #: worktree was ever created (a failed fetch, a failed `worktree add`)
    #: leaves this None, because there is nothing left to clean up.
    worktree_path: str | None = None
    conflicts: tuple[ConflictFile, ...] = ()
    #: Non-empty on an infrastructure failure (timeout, fetch/worktree
    #: failure, or a merge failure that produced no detectable conflict
    #: markers) -- distinct from a genuine conflict, so the caller reports
    #: and skips rather than dispatching a remediation task with nothing to
    #: act on.
    error: str = ""


def _git(
    argv: list[str], cwd: str, timeout: int = _DEFAULT_GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """`git` with a bounded timeout that is DATA on expiry, never an
    exception: an uncaught `subprocess.TimeoutExpired` here would unwind
    through the whole per-tick loop and abort every other PR still waiting,
    not just the one slow operation -- exactly the crash-the-whole-tick
    defect this module must not repeat on the huge/stale PRs it targets."""
    try:
        return subprocess.run(
            ["git", *argv], cwd=cwd, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"git_timeout_after_{timeout}s"
        )


def _git_bytes(
    argv: list[str], cwd: str, timeout: int = _DEFAULT_GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    """As `_git`, but reads raw bytes -- a conflicting path may be a binary
    file, and decoding it as text unconditionally (`text=True`, strict
    errors) would raise `UnicodeDecodeError` on real, expected conflicts
    (images, compiled artifacts) instead of the graceful "binary, omit"
    handling `_stage_content` gives it."""
    try:
        return subprocess.run(
            ["git", *argv], cwd=cwd, capture_output=True, text=False,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, b"", b"git_timeout")


def _fetch_refs(repo_path: str, branch: str, base: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Explicit refspecs, never a bare `git fetch origin`: the routed
    clone's own configured refspec is not this module's business, and a
    bare fetch depending on it would silently do the wrong thing (or
    nothing) on a clone configured differently. `+refs/heads/X:refs/remotes/
    origin/X` both fetches the object and updates the remote-tracking ref
    `_attempt_local_merge` reads next, which a fetch of the bare branch name
    alone does not reliably do."""
    return _git(
        ["fetch", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
         f"+refs/heads/{base}:refs/remotes/origin/{base}"],
        repo_path, timeout,
    )


def _stage_content(
    worktree_path: str, path: str, stage: int, timeout: int, max_chars: int
) -> tuple[str | None, bool, bool]:
    """One side (`stage` 2 = ours, 3 = theirs) of a conflicting path.
    Returns (text_or_None, is_binary, was_truncated). A non-zero exit means
    this stage has no blob (the path does not exist on this side); that is
    a legitimate outcome (e.g. deleted-by-us / added-only-on-theirs), not a
    failure worth surfacing."""
    result = _git_bytes(["show", f":{stage}:{path}"], worktree_path, timeout)
    if result.returncode != 0:
        return None, False, False
    raw = result.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, True, False
    if len(text) > max_chars:
        return text[:max_chars] + _CONFLICT_SNIPPET_SUFFIX, False, True
    return text, False, False


def _conflict_side(
    worktree_path: str, path: str, timeout: int, max_chars: int
) -> ConflictFile:
    ours, ours_binary, ours_truncated = _stage_content(worktree_path, path, 2, timeout, max_chars)
    theirs, theirs_binary, theirs_truncated = _stage_content(worktree_path, path, 3, timeout, max_chars)
    binary = ours_binary or theirs_binary
    return ConflictFile(
        path=path,
        ours=None if binary else ours,
        theirs=None if binary else theirs,
        binary=binary,
        truncated=ours_truncated or theirs_truncated,
    )


def _attempt_local_merge(
    repo_path: str,
    branch: str,
    base: str,
    old_head_sha: str,
    *,
    worktree_root: str | None,
    timeout: int,
    conflict_snippet_max_chars: int,
) -> MergeAttempt:
    """Fetch `branch` and `base` from origin and attempt
    `git merge origin/<base>` onto `branch`, entirely inside a fresh,
    isolated worktree -- never the caller's own checkout of `repo_path`, and
    never a rebase (so nothing here ever needs a force-push).

    A worktree is created for both the clean and the conflicting outcome
    (conflict content must be read from it before `merge --abort` runs), so
    `MergeAttempt.worktree_path` is set on both -- the caller MUST clean it
    up in both cases. Only a failure before any worktree exists (fetch,
    `worktree add`) leaves it None."""
    worktree_path = os.path.join(
        worktree_root or tempfile.gettempdir(),
        f"dirty-pr-remediation-{uuid.uuid4().hex}",
    )
    fetch = _fetch_refs(repo_path, branch, base, timeout)
    if fetch.returncode != 0:
        return MergeAttempt(
            clean=False, old_head_sha=old_head_sha,
            error=f"fetch_failed: {fetch.stderr.strip()[:200]}",
        )
    add = _git(
        ["worktree", "add", "--detach", worktree_path, f"refs/remotes/origin/{branch}"],
        repo_path, timeout,
    )
    if add.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)
        return MergeAttempt(
            clean=False, old_head_sha=old_head_sha,
            error=f"worktree_add_failed: {add.stderr.strip()[:200]}",
        )
    merge = _git(
        ["merge", "--no-edit", f"refs/remotes/origin/{base}"], worktree_path, timeout
    )
    if merge.returncode == 0:
        head = _git(["rev-parse", "HEAD"], worktree_path, timeout)
        new_head = head.stdout.strip() if head.returncode == 0 else None
        return MergeAttempt(
            clean=True, old_head_sha=old_head_sha, new_head_sha=new_head,
            worktree_path=worktree_path,
        )

    status = _git(["status", "--porcelain=v1"], worktree_path, timeout)
    conflicts: list[ConflictFile] = []
    if status.returncode == 0:
        paths = sorted({
            line[3:] for line in status.stdout.splitlines()
            if len(line) > 3 and line[:2] in _UNMERGED_STATUS_CODES
        })
        conflicts = [
            _conflict_side(worktree_path, p, timeout, conflict_snippet_max_chars)
            for p in paths
        ]
    # Capture BEFORE abort, always -- and always abort, so the worktree is
    # left in a clean (non-mid-merge) state for `_remove_worktree` either way.
    _git(["merge", "--abort"], worktree_path, timeout)
    if not conflicts:
        # `git merge` failed but left no detectable unmerged path: an
        # infrastructure problem (a dirty worktree state, an unexpected
        # merge strategy failure), not a conflict a remediation task could
        # act on. Reported and skipped, never dispatched with nothing to say.
        return MergeAttempt(
            clean=False, old_head_sha=old_head_sha, worktree_path=worktree_path,
            error=f"merge_failed_no_conflicts_detected: {merge.stderr.strip()[:200]}",
        )
    return MergeAttempt(
        clean=False, old_head_sha=old_head_sha, worktree_path=worktree_path,
        conflicts=tuple(conflicts),
    )


def _remove_worktree(repo_path: str, worktree_path: str, timeout: int) -> None:
    _git(["worktree", "remove", "--force", worktree_path], repo_path, timeout)
    shutil.rmtree(worktree_path, ignore_errors=True)
    _git(["worktree", "prune"], repo_path, timeout)


def _pr_branch_base_head(repo_path: str, pr_url: str) -> tuple[str, str, str] | None:
    """(head branch name, base branch name, head sha) for an open PR, or
    None on any lookup/parse failure."""
    view = review_merge._gh(
        ["pr", "view", pr_url, "--json", "headRefName,baseRefName,headRefOid,state"],
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
    branch, base, head = data.get("headRefName"), data.get("baseRefName"), data.get("headRefOid")
    if not branch or not base or not head:
        return None
    return str(branch), str(base), str(head)


def _pr_stats(repo_path: str, pr_url: str) -> tuple[int, int, int]:
    """(additions, deletions, changed_files); (0, 0, 0) on any lookup/parse
    failure -- a stats lookup failure must never accidentally look "huge"
    (it wouldn't cross either threshold) nor crash the tick."""
    view = review_merge._gh(
        ["pr", "view", pr_url, "--json", "additions,deletions,changedFiles"], repo_path,
    )
    if view.returncode != 0:
        return 0, 0, 0
    try:
        data = json.loads(view.stdout or "{}")
    except json.JSONDecodeError:
        return 0, 0, 0
    if not isinstance(data, dict):
        return 0, 0, 0
    return (
        int(data.get("additions") or 0),
        int(data.get("deletions") or 0),
        int(data.get("changedFiles") or 0),
    )


def _is_huge_or_stale(
    additions: int, deletions: int, changed_files: int, cfg: DirtyPrRemediationConfig
) -> bool:
    return (
        (additions + deletions) >= cfg.huge_pr_lines_threshold
        or changed_files >= cfg.huge_pr_files_threshold
    )


def _fence_for(text: str) -> str:
    """The shortest run of backticks (at least 3) that is longer than any
    run already present in `text` -- so a conflicting file that itself
    contains a triple-backtick sequence cannot corrupt the surrounding
    Markdown fence in a dispatched task body."""
    longest_run = 0
    current_run = 0
    for ch in text:
        if ch == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, longest_run + 1)


def _format_conflicts(conflicts: tuple[ConflictFile, ...]) -> str:
    parts: list[str] = []
    for conflict in conflicts:
        parts.append(f"### `{conflict.path}`")
        if conflict.binary:
            parts.append("(binary file -- content omitted; resolve locally)")
            continue
        for label, content in (
            ("ours (PR branch)", conflict.ours),
            ("theirs (origin/base)", conflict.theirs),
        ):
            if content is None:
                parts.append(f"{label}: absent on this side")
                continue
            note = " (truncated)" if content.endswith(_CONFLICT_SNIPPET_SUFFIX) else ""
            fence = _fence_for(content)
            parts.append(f"{label}{note}:\n{fence}\n{content}\n{fence}")
    return "\n\n".join(parts)


class _DispatchOutcome:
    DISPATCHED = "dispatched"
    ALREADY_DISPATCHED = "already_dispatched"
    PARENT_VANISHED = "parent_vanished"
    DEPTH_EXCEEDED = "depth_exceeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _DispatchResult:
    outcome: str
    new_task_id: str | None = None
    detail: str = ""


def _dispatch_conflict_task(
    factory: Any,
    task_id: str,
    pr_url: str,
    old_head_sha: str,
    conflicts: tuple[ConflictFile, ...],
    *,
    defer_note: str | None,
) -> _DispatchResult:
    """Create a new, linked follow-up task for a genuine, unresolved
    conflict -- never cycling the DIRTY task's own state (it stays
    READY_TO_REVIEW, untouched, exactly the way it is while its PR still
    stands) -- the same parent/child linkage pattern
    `review_merge._remediate_rejection` uses for a REJECT verdict:
    ``backlog_task_remediation`` records the lineage, and the new task goes
    through the ordinary OPEN -> ... -> READY_TO_REVIEW pipeline with no new
    dispatch code.

    `defer_note` distinguishes the two remediation bodies this can produce:
    None dispatches a scoped rebase task carrying the exact conflicting
    files and both sides' content; a note (the PR's own size, e.g.
    ``"+6697/-373 across 56 files"``) instead dispatches a triage-only task
    asking a human whether the PR is still wanted, without embedding the
    (here, unreasonably large) conflict dump at all.

    Distinguishes every reason a fresh dispatch does NOT happen -- a prior
    dispatch already exists (idempotent, benign), the parent task vanished,
    the remediation-chain depth cap was hit, or a store write genuinely
    failed -- so the caller never has to collapse a real failure into the
    same bucket as "already handled" (which would hide it, and unlike a bare
    ``None`` return, would silently re-attempt and re-fail identically on
    every subsequent tick with no diagnostic difference from normal
    idempotent skip behaviour)."""
    from contextlib import nullcontext

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
                    return _DispatchResult(_DispatchOutcome.ALREADY_DISPATCHED)

                depth = review_merge._remediation_depth(cur, task_id)

                cur.execute(
                    "SELECT wave, priority, title, body, repo "
                    "FROM backlog_task WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return _DispatchResult(_DispatchOutcome.PARENT_VANISHED)
                wave, priority, title, body, repo = row

            if depth + 1 > review_merge.MAX_REMEDIATION_DEPTH:
                conn.rollback()
                return _DispatchResult(_DispatchOutcome.DEPTH_EXCEEDED)

            new_task_id = f"{task_id}-REBASE"
            if defer_note is not None:
                new_title = f"Triage: {title}"
                new_body = (
                    f"{body}\n\n---\n"
                    f"{task_id}'s PR {pr_url} (head {old_head_sha}) conflicts with "
                    "its base branch and is too large for automatic remediation to "
                    f"scope safely ({defer_note}).\n\n"
                    "triage: still wanted vs superseded? Decide whether to rebase "
                    "this PR by hand, close it as superseded, or split it into "
                    "smaller pieces first -- automatic remediation will not attempt "
                    "to resolve these conflicts itself."
                )
            else:
                new_title = f"Remediation: rebase {title}"
                new_body = (
                    f"{body}\n\n---\n"
                    f"{task_id}'s PR {pr_url} (head {old_head_sha}) conflicts with "
                    "its base branch; an automatic `git merge origin/<base>` "
                    "attempted in an isolated worktree left the following files "
                    "unresolved. Merge (never rebase) the base branch into the PR "
                    "branch, resolve every conflict below, push the result, and "
                    "open a fresh pull request -- the original PR is left as-is, "
                    "superseded by this task.\n\n" + _format_conflicts(conflicts)
                )

            store = BacklogStore(lambda: nullcontext(conn))
            ok, reason, _changed = store.upsert_task(
                ParsedTask(
                    task_id=new_task_id, wave=wave, priority=priority,
                    status="OPEN", kind="task", title=new_title, body=new_body,
                    repo=repo, line_no=0,
                )
            )
            if not ok:
                conn.rollback()
                return _DispatchResult(_DispatchOutcome.FAILED, detail=reason)
            ok, reason = store.record_remediation(new_task_id, task_id, pr_url, old_head_sha)
            if not ok:
                conn.rollback()
                return _DispatchResult(_DispatchOutcome.FAILED, detail=reason)
            conn.commit()
            return _DispatchResult(_DispatchOutcome.DISPATCHED, new_task_id=new_task_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


def remediate_dirty_prs(
    factory: Any,
    repo_path: str,
    base_publish_cfg: PublishConfig,
    cfg: DirtyPrRemediationConfig | None = None,
) -> DirtyPrRemediationReport:
    """One remediation tick over every READY_TO_REVIEW task whose PR is
    DIRTY, self-healing the cleanly-mergeable ones and dispatching a scoped
    follow-up task for the genuinely conflicting ones -- see the module
    docstring for the full design and rationale.

    ``base_publish_cfg`` is a template `PublishConfig` (lease tool,
    repository, owner, session, deploy key); per PR this replaces only
    ``task``, ``base``, ``remote_sha`` and ``remote_sha_known`` -- pinning
    the guarded publish against the PR's own pre-merge head, exactly the
    same race guard `publish_run` already gives every other caller.

    Uses the same scan-cursor protocol as `review_merge.merge_once`
    (`review_merge._scan_tasks` / `_scan_commit`, cursor name
    ``scan:dirty_pr_remediation``): examinations are bounded by
    ``cfg.scan_cap``, real merge ATTEMPTS by ``cfg.max_attempts_per_tick``.
    The cap is checked BEFORE this tick records a row as its last-processed
    one -- a row that would exceed the cap is left completely untouched and
    is not advanced past by the cursor, so it is reconsidered from scratch
    next tick rather than being permanently skipped with no record of why
    (the cursor must never claim to have handled a row this tick did zero
    work on)."""
    cfg = cfg or DirtyPrRemediationConfig()
    report = DirtyPrRemediationReport()
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
    last_processed: tuple[Any, ...] | None = None
    attempts = 0
    for task_id, pr_url in tasks:
        # The cap gates whether this row is processed AT ALL, so it must be
        # checked before `last_processed` is updated: recording a row as
        # processed and then doing zero work on it (the defect this fixes)
        # would permanently exclude that row from every future scan once the
        # cursor advances past it, with no work done and no reason logged.
        if attempts >= cfg.max_attempts_per_tick:
            break
        last_processed = (task_id, pr_url)

        state = review_merge._merge_state(repo_path, pr_url)
        if state != "DIRTY":
            # BEHIND/CLEAN/BLOCKED/UNKNOWN/"" are not this remediation's
            # business -- merge_once (or nothing, for a non-open PR) already
            # covers them.
            continue

        dup = review_merge._rows(
            factory,
            "SELECT 1 FROM backlog_task_remediation WHERE parent_task_id = %s",
            (task_id,),
        )
        if dup:
            report.skipped.append((task_id, "remediation_already_dispatched"))
            continue

        info = _pr_branch_base_head(repo_path, pr_url)
        if info is None:
            report.skipped.append((task_id, "pr_view_failed"))
            continue
        branch, base, old_head_sha = info

        attempts += 1
        additions, deletions, changed_files = _pr_stats(repo_path, pr_url)
        huge = _is_huge_or_stale(additions, deletions, changed_files, cfg)

        attempt = _attempt_local_merge(
            repo_path, branch, base, old_head_sha,
            worktree_root=cfg.worktree_root, timeout=cfg.git_timeout_seconds,
            conflict_snippet_max_chars=cfg.conflict_snippet_max_chars,
        )
        try:
            if attempt.error:
                report.skipped.append((task_id, attempt.error))
                continue
            if attempt.clean:
                assert attempt.worktree_path is not None
                publish_cfg = dataclasses.replace(
                    base_publish_cfg, task=task_id, base=base,
                    remote_sha=old_head_sha, remote_sha_known=True,
                )
                result = publish_run(Path(attempt.worktree_path), publish_cfg)
                if result.ok:
                    report.healed.append(
                        (task_id, result.head_sha or attempt.new_head_sha or "")
                    )
                else:
                    report.skipped.append(
                        (task_id, f"guarded_publish_failed: {result.reason}")
                    )
                continue

            defer_note = (
                f"+{additions}/-{deletions} across {changed_files} files" if huge else None
            )
            dispatch = _dispatch_conflict_task(
                factory, task_id, pr_url, old_head_sha, attempt.conflicts,
                defer_note=defer_note,
            )
            if dispatch.outcome == _DispatchOutcome.DISPATCHED:
                assert dispatch.new_task_id is not None
                if huge:
                    report.deferred.append((task_id, dispatch.new_task_id))
                else:
                    report.rebase_dispatched.append((task_id, dispatch.new_task_id))
            elif dispatch.outcome == _DispatchOutcome.ALREADY_DISPATCHED:
                report.skipped.append((task_id, "remediation_already_dispatched"))
            elif dispatch.outcome == _DispatchOutcome.PARENT_VANISHED:
                report.skipped.append((task_id, "task_vanished"))
            elif dispatch.outcome == _DispatchOutcome.DEPTH_EXCEEDED:
                report.skipped.append((task_id, "remediation_depth_exceeded"))
            else:
                reason = f"dispatch_failed: {dispatch.detail}" if dispatch.detail else "dispatch_failed"
                report.skipped.append((task_id, reason))
        finally:
            # Unconditional: a worktree exists on every outcome this branch
            # can reach (clean, conflicting, or the no-conflicts-detected
            # error) except the two pre-worktree failures already handled by
            # `continue` above (fetch_failed / worktree_add_failed, where
            # `_attempt_local_merge` itself already cleaned up and leaves
            # `worktree_path` None) -- cleaning up here regardless of which
            # branch ran is what stops every conflict outcome (the PRIMARY
            # code path this feature exists to handle) from leaking a
            # worktree on disk.
            if attempt.worktree_path is not None:
                _remove_worktree(repo_path, attempt.worktree_path, cfg.git_timeout_seconds)

    review_merge._scan_commit(factory, "scan:dirty_pr_remediation", scan_token, last_processed)
    return report
