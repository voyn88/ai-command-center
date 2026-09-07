"""Automated historical DLQ redrive with eligibility checks
(VOYN-W0-AICC-DLQ-REDRIVE-AUTOMATION).

Manual batches on 2026-09-06/07 recovered 6/6 ``uncommitted_changes``
dead-letters (PRs 693, 747, 751, 761, 767 + canary 644): each one's worker had
finished real work in its isolated task clone, but ``publish_run`` refused it
(``command_center/orchestrator/publish.py``'s ``uncommitted_changes`` reason
--- ``git status --porcelain`` was not empty at publish time, so the change
was never pushed and the item burned its attempt budget into ``work_dlq``).
The fix in every one of those six cases was: commit the surviving dirty
clone's changes, then ``queue-redrive`` the dead-lettered item so publish gets
a clean tree to work with on the next attempt. This module is that loop, made
repeatable and safe to run unattended:

1. ``find_uncommitted_changes_pool`` -- the only candidates this automation
   ever touches are dead letters whose ``dead_reason`` is exactly
   ``uncommitted_changes``. Every other dead-letter reason is a different
   failure class and is left to ``classify_remaining`` instead.
2. ``check_eligibility`` -- four checks, each a refusal on its own:
   no live ``ready``/``claimed`` duplicate already covers the task (redriving
   would just fan out two runs racing each other), no open PR or remote
   ``backlog/<task>`` branch already exists (the work already escaped through
   the normal path; redriving would re-do finished work), and the standalone
   clone that failed to publish must still exist AND still be dirty (an
   absent or clean clone means there is nothing left to commit -- redriving
   it would only re-burn the budget for the same reason).
3. ``redrive_pool_one_at_a_time`` -- redrives exactly one eligible item, then
   blocks on ``wait_for_resolution`` before considering the next. This is not
   an optimisation to relax later: per
   VOYN-W0-AICC-PUBLISH-LEASE-CONTENTION-BURNS-ATTEMPT, two redriven items
   whose publishes land concurrently collide on the writer lease and one of
   them burns the very attempt this automation just granted it. Serializing
   at the queue level is the only way this loop cannot reproduce that finding
   against itself.

The other half of the task -- the roughly 390 dead letters that are not
``uncommitted_changes`` -- is a one-shot classification pass
(``classify_remaining``), not a retry loop: those items get a recorded
disposition (``superseded`` / ``unrecoverable`` / ``needs_review``), appended
to a durable report (``write_disposition_report``). Nothing here ever deletes
or mutates a ``work_dlq`` row -- the DLQ's history is the queue's own audit
trail (``work_event``/``work_attempt``), and this module only ever reads it
and writes its own separate, append-only report alongside it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from command_center.db.work_queue_admin import DeadLetter, WorkQueueAdmin

__all__ = [
    "BACKLOG_BRANCH_PREFIX",
    "CloneProbe",
    "DISPOSITION_NEEDS_REVIEW",
    "DISPOSITION_SUPERSEDED",
    "DISPOSITION_UNRECOVERABLE",
    "Disposition",
    "EligibilityResult",
    "RedriveOutcome",
    "UNCOMMITTED_CHANGES_REASON",
    "build_default_clone_locator",
    "build_default_duplicate_checker",
    "build_default_pr_or_branch_checker",
    "build_default_superseded_checker",
    "check_eligibility",
    "classify_remaining",
    "find_uncommitted_changes_pool",
    "poll_until_resolved",
    "redrive_pool_one_at_a_time",
    "write_disposition_report",
]

#: The one dead_reason this automation's redrive loop ever acts on. Every
#: other reason is a different failure class, routed to classify_remaining.
UNCOMMITTED_CHANGES_REASON = "uncommitted_changes"

#: The branch naming convention publish_run uses (backlog branch is
#: idempotent per task -- see publish_run's own docstring).
BACKLOG_BRANCH_PREFIX = "backlog/"

# Queue states that mean "still in flight" -- a live duplicate in either
# blocks a redrive, and the one-at-a-time gate waits for a redriven item to
# leave both before considering the next candidate.
_IN_FLIGHT_STATES = ("ready", "claimed")

DISPOSITION_SUPERSEDED = "superseded"
DISPOSITION_UNRECOVERABLE = "unrecoverable"
DISPOSITION_NEEDS_REVIEW = "needs_review"

# dead_reason substrings that mean the queue could never have succeeded no
# matter how many times it was redriven -- a permanent, payload/environment
# level refusal rather than a transient one. Matched case-insensitively
# against the substring, not the whole string: exact wording varies with the
# gate that produced it (e.g. "non_retryable: ...").
_UNRECOVERABLE_REASON_MARKERS = (
    "non_retryable",
    "validation",
    "missing required field",
    "unknown project_id",
    "repository not found",
    "leak_guard_failed",
)


@dataclass(frozen=True, slots=True)
class CloneProbe:
    """What the surviving-standalone-clone check found for one task's clone.

    ``exists=False`` and ``dirty=False`` are both first-class outcomes, not
    errors -- a clone that was already cleaned up, or one that published
    successfully on a later manual attempt, looks exactly like this."""

    exists: bool
    dirty: bool
    path: str | None = None


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Whether one dead-lettered item may be redriven, and why (or why not)
    -- always a value, never an exception: a refusal is ordinary DLQ
    triage, not a fault in this automation."""

    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RedriveOutcome:
    """What happened when the loop considered one candidate."""

    work_item_id: str
    task_id: str | None
    eligibility: EligibilityResult
    redriven: bool = False
    resolved_state: str | None = None


@dataclass(frozen=True, slots=True)
class Disposition:
    """The recorded classification for one dead-lettered item that is not
    (or is no longer) part of the uncommitted_changes redrive pool."""

    work_item_id: str
    task_id: str | None
    dead_reason: str
    disposition: str
    rationale: str


def find_uncommitted_changes_pool(
    admin: WorkQueueAdmin, *, queue: str | None = None, limit: int = 500
) -> list[DeadLetter]:
    """The redrive loop's candidate pool: dead letters whose ``dead_reason``
    is exactly ``uncommitted_changes``, newest death first (``dead_letters``'
    own order) -- everything else belongs to ``classify_remaining``."""
    return [
        letter
        for letter in admin.dead_letters(queue, limit=limit)
        if letter.dead_reason == UNCOMMITTED_CHANGES_REASON
    ]


# -- eligibility ---------------------------------------------------------


def check_eligibility(
    candidate: DeadLetter,
    *,
    has_live_duplicate: Callable[[DeadLetter], bool],
    has_open_pr_or_branch: Callable[[DeadLetter], bool],
    locate_clone: Callable[[str], CloneProbe],
) -> EligibilityResult:
    """The four refusals, checked in the cheapest-first order (no network
    call is made once a local/DB check has already refused)."""
    if not candidate.task_id:
        return EligibilityResult(False, "dead letter has no task_id to act on")
    if has_live_duplicate(candidate):
        return EligibilityResult(
            False,
            "a live ready/claimed duplicate already covers this task; "
            "redriving would race it",
        )
    if has_open_pr_or_branch(candidate):
        return EligibilityResult(
            False,
            "an open PR or a remote backlog/<task> branch already exists; "
            "the work already escaped through the normal path",
        )
    probe = locate_clone(candidate.task_id)
    if not probe.exists:
        return EligibilityResult(
            False, "no surviving standalone clone for this task"
        )
    if not probe.dirty:
        return EligibilityResult(
            False,
            "the surviving clone carries no uncommitted changes; "
            "nothing left to recover",
        )
    return EligibilityResult(True, f"surviving dirty clone at {probe.path}")


def build_default_duplicate_checker(
    connection: Any,
) -> Callable[[DeadLetter], bool]:
    """The production ``has_live_duplicate``: a plain ``SELECT`` against
    ``work_item`` -- ``aicc_app`` already holds ``SELECT`` on it
    (``roles._APP_QUEUE_TABLES``), so this needs no new grant."""

    def _has_live_duplicate(candidate: DeadLetter) -> bool:
        if not candidate.task_id:
            return False
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM work_item WHERE task_id = %s "
                "AND state IN ('ready', 'claimed') "
                "AND work_item_id != %s LIMIT 1",
                (candidate.task_id, candidate.work_item_id),
            )
            return cur.fetchone() is not None

    return _has_live_duplicate


def build_default_pr_or_branch_checker(
    repo_path: Path, *, github_client: Any = None, remote: str = "origin"
) -> Callable[[DeadLetter], bool]:
    """The production ``has_open_pr_or_branch``: an open PR whose head is
    ``backlog/<task_id>`` (any state considered, only OPEN blocks -- a
    merged/closed-unmerged PR is exactly what ``classify_remaining`` needs to
    see once the item leaves this pool), or that branch still present on the
    remote (``git ls-remote --heads``, VOYN-W0-AICC's own read-only
    convention -- see ``runtime.repo_state.remote_branch_exists``)."""
    if github_client is None:
        from command_center.runtime.github import GitHubClient

        github_client = GitHubClient()

    from command_center.runtime.repo_state import remote_branch_exists

    def _has_open_pr_or_branch(candidate: DeadLetter) -> bool:
        if not candidate.task_id:
            return False
        branch = f"{BACKLOG_BRANCH_PREFIX}{candidate.task_id}"
        pr = github_client.discover_pull_request(repo_path, branch=branch)
        if pr is not None and pr.is_open:
            return True
        return remote_branch_exists(repo_path, remote, branch)

    return _has_open_pr_or_branch


def build_default_clone_locator(clones_root: Path) -> Callable[[str], CloneProbe]:
    """The production ``locate_clone``: this very fleet's own naming
    convention (``backlog-<TASK_ID>-<hash>``, as this module's own clone is
    named) under ``clones_root``. Dirtiness is ``git status --porcelain``,
    the identical check ``publish_run`` itself makes -- so "still dirty" here
    means exactly what made the item dead-letter in the first place."""
    import subprocess

    def _locate(task_id: str) -> CloneProbe:
        matches = sorted(clones_root.glob(f"backlog-{task_id}-*"))
        candidate_dir = next((p for p in matches if p.is_dir()), None)
        if candidate_dir is None:
            return CloneProbe(exists=False, dirty=False)
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(candidate_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return CloneProbe(exists=True, dirty=False, path=str(candidate_dir))
        dirty = proc.returncode == 0 and bool(proc.stdout.strip())
        return CloneProbe(exists=True, dirty=dirty, path=str(candidate_dir))

    return _locate


# -- the one-at-a-time redrive loop --------------------------------------


def poll_until_resolved(
    state_of: Callable[[str], str | None],
    work_item_id: str,
    *,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Block until ``work_item_id`` leaves ``ready``/``claimed`` (returning
    the terminal state it settled into), or until ``timeout`` elapses
    (returning ``None`` -- a caller must treat that as "still contended",
    never as "safe to proceed", or it reopens exactly the writer-lease
    collision this loop exists to avoid)."""
    deadline = clock() + timeout
    while True:
        state = state_of(work_item_id)
        if state not in _IN_FLIGHT_STATES:
            return state
        if clock() >= deadline:
            return None
        sleep(poll_interval)


def redrive_pool_one_at_a_time(
    admin: WorkQueueAdmin,
    candidates: Iterable[DeadLetter],
    *,
    has_live_duplicate: Callable[[DeadLetter], bool],
    has_open_pr_or_branch: Callable[[DeadLetter], bool],
    locate_clone: Callable[[str], CloneProbe],
    wait_for_resolution: Callable[[str], str | None],
    extra_attempts: int = 1,
) -> list[RedriveOutcome]:
    """Redrive every eligible candidate, strictly one at a time: an eligible
    item is redriven, then the loop blocks on ``wait_for_resolution`` for
    that exact item before it even evaluates the next candidate's
    eligibility. A candidate found ineligible costs no wait -- only an actual
    redrive gates the loop, since only an actual redrive can produce a
    concurrent publish."""
    outcomes: list[RedriveOutcome] = []
    for candidate in candidates:
        eligibility = check_eligibility(
            candidate,
            has_live_duplicate=has_live_duplicate,
            has_open_pr_or_branch=has_open_pr_or_branch,
            locate_clone=locate_clone,
        )
        if not eligibility.eligible:
            outcomes.append(
                RedriveOutcome(candidate.work_item_id, candidate.task_id, eligibility)
            )
            continue
        accepted = admin.redrive(candidate.work_item_id, extra_attempts=extra_attempts)
        resolved_state = wait_for_resolution(candidate.work_item_id) if accepted else None
        outcomes.append(
            RedriveOutcome(
                candidate.work_item_id,
                candidate.task_id,
                eligibility,
                redriven=accepted,
                resolved_state=resolved_state,
            )
        )
    return outcomes


# -- classifying what is left ---------------------------------------------


def build_default_superseded_checker(
    repo_path: Path, *, github_client: Any = None, remote: str = "origin"
) -> Callable[[DeadLetter], bool]:
    """The production ``superseded`` predicate for ``classify_remaining``:
    true when the task's work already landed through the normal path --
    a merged PR, or an open one still working its way through review --
    even though this exact queue attempt died. Distinct from the redrive
    pool's ``has_open_pr_or_branch`` (which blocks a *redrive*, and stops at
    OPEN-only) because a MERGED PR is exactly the signal that makes a dead
    letter recoverable-in-spirit but not recoverable-by-redrive: the redrive
    has nothing left to do, and re-running it would just re-open a task
    that's already shipped."""
    if github_client is None:
        from command_center.runtime.github import GitHubClient

        github_client = GitHubClient()

    def _superseded(letter: DeadLetter) -> bool:
        if not letter.task_id:
            return False
        branch = f"{BACKLOG_BRANCH_PREFIX}{letter.task_id}"
        pr = github_client.discover_pull_request(repo_path, branch=branch)
        return pr is not None and (pr.is_open or pr.is_merged)

    return _superseded


def classify_remaining(
    dead_letters: Iterable[DeadLetter],
    *,
    superseded_check: Callable[[DeadLetter], bool],
    exclude_reason: str = UNCOMMITTED_CHANGES_REASON,
) -> list[Disposition]:
    """One-shot disposition for every dead letter that is not (or is no
    longer) part of the ``uncommitted_changes`` redrive pool:

    - ``superseded``: the task's work already landed through the normal PR
      path; this dead-lettered attempt is a stale duplicate of a completed
      story, not an outstanding one.
    - ``unrecoverable``: the recorded reason is a permanent, payload/
      environment-level refusal (``_UNRECOVERABLE_REASON_MARKERS``) that a
      redrive cannot fix -- the same input would fail the same way again.
    - ``needs_review``: neither of the above; an operator's judgement call,
      not a guess this automation should make for them.

    Never mutates or deletes a ``work_dlq``/``work_item`` row -- the caller
    persists the returned dispositions separately (``write_disposition_report``).
    """
    dispositions: list[Disposition] = []
    for letter in dead_letters:
        if letter.dead_reason == exclude_reason:
            continue
        if superseded_check(letter):
            dispositions.append(
                Disposition(
                    letter.work_item_id,
                    letter.task_id,
                    letter.dead_reason,
                    DISPOSITION_SUPERSEDED,
                    "an open or merged PR already covers this task; the "
                    "dead-lettered attempt is a stale duplicate",
                )
            )
            continue
        reason_lower = (letter.dead_reason or "").lower()
        marker = next(
            (m for m in _UNRECOVERABLE_REASON_MARKERS if m in reason_lower), None
        )
        if marker is not None:
            dispositions.append(
                Disposition(
                    letter.work_item_id,
                    letter.task_id,
                    letter.dead_reason,
                    DISPOSITION_UNRECOVERABLE,
                    f"dead_reason matches the permanent-failure marker {marker!r}",
                )
            )
            continue
        dispositions.append(
            Disposition(
                letter.work_item_id,
                letter.task_id,
                letter.dead_reason,
                DISPOSITION_NEEDS_REVIEW,
                "automation could not classify this dead_reason; needs an "
                "operator's judgement",
            )
        )
    return dispositions


def write_disposition_report(
    path: Path, dispositions: Iterable[Disposition], *, generated_at: str
) -> int:
    """Append the run's dispositions to ``path`` as JSON Lines -- one record
    per line, one line per (work_item_id, run). Deliberately append-only
    (``"a"``, never ``"w"``): a later run reclassifying the same item adds a
    new record rather than overwriting the earlier one, so the report is
    itself a history, matching the DLQ rows it describes rather than
    replacing them. Returns the number of records written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for disposition in dispositions:
            handle.write(
                json.dumps(
                    {
                        "generated_at": generated_at,
                        "work_item_id": disposition.work_item_id,
                        "task_id": disposition.task_id,
                        "dead_reason": disposition.dead_reason,
                        "disposition": disposition.disposition,
                        "rationale": disposition.rationale,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            count += 1
    return count
