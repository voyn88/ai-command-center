"""The planner tick (BO-S2): eligible tasks -> atomic dispatches -> a report.

One tick, no loop: the schedule is a systemd oneshot timer
(deploy/systemd/aicc-backlog-planner.timer), the reaper's pattern — a missed
tick delays planning and never corrupts it, because every mutating step is
one call to ``backlog_dispatch`` (0006), which is atomic or refused.

Single planner, machine-held: the tick first takes the ``planner:global``
lease. A second control host running the same timer gets ``planner_busy``
and an empty report — not a second writer.

The report is the owner's answer to "why is my task waiting": every
non-dispatched candidate lands in exactly one bucket with the dispatch
function's own refusal reason, including ``skipped_by_wave_gate``
(approved decision 1) so the UI can say "wave N is still working".

The reuse gate (VOYN-W0-AICC-DISPATCH-REUSE-GATE): before dispatching a
candidate that is a re-attempt of earlier work -- a remediation task (REM,
linked to a parent via ``backlog_task_remediation``) or a resumed retry
(RETRY, a task with a granted ``resume_deferred`` in its history) -- the
planner checks whether the work it names is already on the target branch.
Live case 2026-09-06: a REM task's branch (PR 636) re-implemented a function
its parent had already delivered via a merged PR (624); the two
implementations collided and broke CI once both landed. An ordinary
first-time OPEN task has neither signal and skips the check entirely, so
this costs nothing on the common path. A match closes the task DONE via
``backlog_close_superseded`` (0019), evidenced by the already-landed commit,
instead of dispatching a duplicate; the count lands in
``PlanReport.superseded``, the tick's telemetry for prevented duplicate
dispatches. No match, or an inconclusive lookup, falls through to the
ordinary dispatch path unchanged -- a missed detection reproduces today's
behavior; a false positive would silently discard a task that still needs
doing, which is the worse failure, so the gate never invents a match.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from command_center.orchestrator.routing import cascade_for
from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = ["PlanLimits", "PlanReport", "plan_once"]

_PLANNER_AUTHORITY = "planner:global"

#: The reuse gate's git lookup: (repository_path, task_id, branch) -> the
#: matching commit's (sha, subject), or None when no match is found.
ReuseLookup = Callable[[str, str, str], "tuple[str, str] | None"]


@dataclass(frozen=True, slots=True)
class PlanLimits:
    planner: str = "aicc-planner"
    wip_limit: int = 4
    #: Repo leases must outlive the dispatched RUN (hours).
    lease_ttl_seconds: int = 7200
    #: The planner:global lease covers one TICK (seconds) — its own parameter
    #: (PLANNER-LEASE-TTL follow-up): a tick that dies before releasing must
    #: not lock every control host out for the repo-lease horizon.
    planner_lease_ttl_seconds: int = 300
    #: Per-tick dispatch cap, distinct from WIP: one tick must stay short.
    max_dispatches_per_tick: int = 4
    timeout_seconds: int = 900
    #: Per-tick cap on DEFER_TO_USER auto-resumes (VOYN-W0-AICC-DEFER-AUTO-
    #: RESUME). Bounded so a large parked backlog drains gradually across
    #: ticks instead of flooding OPEN in one; 0 disables the reconcile.
    max_resumes_per_tick: int = 10
    #: The planner's dispatch-only backpressure fence: once this many
    #: READY_TO_REVIEW tasks are carrying `pr` evidence, this tick dispatches
    #: nothing new, so implementation supply stops outrunning review/merge
    #: capacity. Scoped to dispatch alone -- see `Planner.plan_once`'s
    #: comment at the fence check for why ingest and the DEFER_TO_USER
    #: resume reconcile above run unconditionally regardless of this fence.
    #: 0 disables the fence, matching `max_resumes_per_tick`'s convention
    #: in this same class (not a threshold of 1: an operator following that
    #: sibling field's convention would otherwise get a silently different
    #: meaning for the same sentinel value on this field).
    review_backlog_limit: int = 20
    #: The branch the reuse gate checks a REM/RETRY candidate's source task
    #: against (VOYN-W0-AICC-DISPATCH-REUSE-GATE). "main" everywhere this
    #: fleet runs today, same default as `orchestrator.publish.PublishConfig
    #: .base` -- kept configurable rather than hard-coded so a repo with a
    #: different default branch is not silently checked against the wrong one.
    reuse_check_branch: str = "main"


@dataclass(slots=True)
class PlanReport:
    dispatched: list[tuple[str, str]] = field(default_factory=list)  # (task, work_item)
    skipped_by_wave_gate: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    undispatchable: list[tuple[str, str]] = field(default_factory=list)
    ingested: list[tuple[str, str]] = field(default_factory=list)  # (task, action)
    #: (task, original park reason) — DEFER_TO_USER technical parks the 0014
    #: gate returned to OPEN this tick (VOYN-W0-AICC-DEFER-AUTO-RESUME).
    resumed: list[tuple[str, str]] = field(default_factory=list)
    #: (task, source_task, evidence_sha) — a REM/RETRY candidate closed DONE
    #: via `backlog_close_superseded` instead of dispatched, because
    #: `source_task`'s acceptance criteria were already met on
    #: `PlanLimits.reuse_check_branch` (VOYN-W0-AICC-DISPATCH-REUSE-GATE).
    #: This list's length is the tick's count of prevented duplicate
    #: dispatches.
    superseded: list[tuple[str, str, str]] = field(default_factory=list)
    planner_busy: bool = False
    #: The review backlog count that tripped `PlanLimits.review_backlog_
    #: limit` this tick (None when the fence never fired). Ingest and the
    #: DEFER_TO_USER resume reconcile still ran -- only dispatch paused.
    review_window_full: int | None = None


# Repo → (canonical project_id, worker-host repository path). The worker's
# validate_repository accepts only PROJECT_IDS members with the configured
# path, so the planner must translate the backlog's repo hint into that
# vocabulary — a task whose repo has no route is reported, never dispatched
# into a guaranteed dead-letter. One fleet, one table, env-overridable;
# per-host routing belongs to the multi-host slice (recorded in the epic).
_DEFAULT_REPO_ROUTES: dict[str, tuple[str, str]] = {
    "ai-command-center": ("AICC", "/home/voynadmin/Projects/ai-command-center"),
    "aios": ("AIOS", "/home/voynadmin/Projects/aios"),
    "~/Projects/aios": ("AIOS", "/home/voynadmin/Projects/aios"),
    "~/Projects/ai-command-center": ("AICC", "/home/voynadmin/Projects/ai-command-center"),
}


def repo_route(repo: str) -> tuple[str, str] | None:
    raw = os.environ.get("AICC_PLANNER_REPO_ROUTES", "")
    if raw:
        # A broken override routes NOTHING — and "broken" includes decodable
        # JSON of the wrong shape: a bare string would be silently misrouted
        # by indexing ("AICC" -> ("A", "I")), a dict raises out of the tick,
        # a non-string path dead-letters downstream. Review proved all three
        # live; every value must be exactly two non-empty strings.
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(decoded, dict):
            return None
        table: dict[str, tuple[str, str]] = {}
        for key, value in decoded.items():
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 2
                or not all(isinstance(part, str) and part for part in value)
            ):
                return None
            table[key] = (value[0], value[1])
        return table.get(repo)
    return _DEFAULT_REPO_ROUTES.get(repo)


_REUSE_GIT_TIMEOUT_SECONDS = 30


def _merged_commit_for(repository_path: str, task_id: str, branch: str) -> tuple[str, str] | None:
    """(sha, subject) of the newest commit on ``origin/<branch>`` whose title
    opens with ``<task_id>:`` — the shape every autonomous delivery in this
    fleet already produces (``publish._verified_pr_result``'s PR title, and
    GitHub's own squash-merge default of "<pr title> (#N)"). Anchored at the
    start of the subject, not a bare substring search: a task id that is a
    string-prefix of another (``VOYN-W0-X`` inside ``VOYN-W0-X-REM``) must
    not read as a match, and the anchor plus the literal colon after it is
    exactly what tells the two apart.

    A best-effort ``git fetch`` first, because this process's local clone is
    the same shared control-plane checkout ``orchestrator.publish`` pushes
    through (`repo_route`'s table) and nothing else keeps its remote-tracking
    refs current on a schedule -- a merge landed by ``review_merge.merge_
    once`` (a server-side ``gh pr merge``, not a local push) never touches
    this clone's ``origin/<branch>`` until something fetches it. The fetch's
    own failure is swallowed: this then answers off whatever ``origin/
    <branch>`` already points at, which can only be MISSING a very recent
    merge, never inventing one that never happened -- so the worst case of a
    stale or unreachable remote is "the gate does not fire yet," the same
    behavior as before this gate existed, never a false close.

    Any other failure (git absent, not a git repository, malformed branch)
    is equally inconclusive and returns None on the same reasoning.
    """
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", branch],
            cwd=repository_path,
            capture_output=True,
            text=True,
            timeout=_REUSE_GIT_TIMEOUT_SECONDS,
        )
        result = subprocess.run(
            [
                "git",
                "log",
                f"origin/{branch}",
                "-1",
                f"--grep=^{re.escape(task_id)}:",
                "--format=%H%x1f%s",
            ],
            cwd=repository_path,
            capture_output=True,
            text=True,
            timeout=_REUSE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    sha, _, subject = result.stdout.strip().partition("\x1f")
    return (sha, subject) if sha else None


def _payload_for(
    task: dict[str, Any], limits: PlanLimits, route: tuple[str, str]
) -> tuple[dict[str, Any], int]:
    """The agent_run payload plus the attempt budget (= cascade length).

    Prompt discipline: the task record IS the assignment — id, title, body
    travel verbatim; the worker's provenance gate still applies, and
    ``untrusted=False`` is on the authority of the planner being the control
    plane acting on the canonical store.
    """
    cascade = cascade_for("implementation")
    project_id, repository_path = route
    prompt = (
        f"Central task: {task['task_id']} ({task['title']}).\n"
        f"Wave {task['wave']}, priority {task['priority'] or 'unset'}.\n\n"
        f"{task['body']}\n\n"
        # The publisher (`orchestrator.publish.publish_run`, under the writer
        # lease) refuses a task clone that is not clean, and an agent has no
        # push capability at all. Asking the agent to "open a pull request"
        # therefore asked for the one thing it cannot do, while never asking
        # for the one thing it must: the commit. Live consequence
        # (VOYN-W0-AICC-AGENT-COMMIT-CONTRACT-GAP): completed work sat
        # uncommitted in the clone, `agent_worktree_clean` refused it, and the
        # cascade spent every remaining attempt reproducing the same refusal.
        "Commit every change you make to the task branch in this clone before "
        "you finish -- `git add` and `git commit` are yours to run, and an "
        "uncommitted change is discarded work, not a result. Do NOT push and "
        "do NOT open a pull request: you have no push capability, and the "
        "orchestrator publishes your commits through the guarded publisher "
        "after you exit.\n"
        "End your final message with a line of exactly this form so the "
        "orchestrator can record the evidence:\n"
        "HEAD_SHA: <the branch head commit sha>"
    ).strip()
    payload = {
        "kind": "agent_run",
        "v": AGENT_RUN_SCHEMA_VERSION,
        "project_id": project_id,
        "repository_path": repository_path,
        "prompt": prompt,
        "task_type": cascade[0]["task_type"],
        "timeout_seconds": limits.timeout_seconds,
        "untrusted": False,
        "cascade": cascade,
        "backlog_task_id": task["task_id"],
    }
    return payload, len(cascade)


class Planner:
    """Owns nothing but the composition; every decision is the database's."""

    def __init__(
        self, connection_factory: Any, reuse_lookup: ReuseLookup = _merged_commit_for
    ) -> None:
        self._factory = connection_factory
        #: Injectable for tests (VOYN-W0-AICC-DISPATCH-REUSE-GATE): the real
        #: default shells out to git, which a unit test has no repository to
        #: run against.
        self._reuse_lookup = reuse_lookup

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def _row(self, sql: str, params: tuple[Any, ...]) -> tuple:
        return self._rows(sql, params)[0]

    def _reuse_anchor(self, task_id: str) -> str | None:
        """The task_id whose already-landed evidence would make `task_id` a
        duplicate dispatch, or None when neither signal applies.

        Two durable, DB-recorded signals -- not a `-REM` suffix convention,
        the same reasoning `review_merge._remediation_depth` gives for
        walking the recorded parent chain instead of a naming convention:

        * A remediation (REM) task: linked to its parent via
          `backlog_task_remediation` (0010). The parent's own merged work is
          what this task's PR might collide with (the live 2026-09-06 case).
        * A resumed retry (RETRY): a task with a granted `resume_deferred`
          anywhere in its history (0014/0017) has already run the cascade at
          least once; the source to check is the task's own id, in case that
          earlier attempt's commit landed before the technical park that
          made the planner try again.

        An ordinary first-time OPEN task matches neither and returns None,
        so the git lookup below never runs for the common dispatch path.
        """
        parent = self._rows(
            "SELECT parent_task_id FROM backlog_task_remediation WHERE task_id = %s",
            (task_id,),
        )
        if parent:
            return str(parent[0][0])
        retried = self._rows(
            "SELECT 1 FROM backlog_event "
            "WHERE task_id = %s AND event = 'resume_deferred' AND outcome = 'granted' "
            "LIMIT 1",
            (task_id,),
        )
        if retried:
            return task_id
        return None

    def plan_once(self, limits: PlanLimits = PlanLimits()) -> PlanReport:
        report = PlanReport()
        ok, reason, *_ = self._row(
            "SELECT * FROM backlog_lease_acquire(%s, %s, %s)",
            (
                _PLANNER_AUTHORITY,
                limits.planner,
                max(limits.planner_lease_ttl_seconds, 60),
            ),
        )
        if not ok:
            report.planner_busy = True
            return report
        try:
            # Ingest finished work first (BO-S3): evidence + READY_TO_REVIEW
            # for the succeeded, return-to-pool (or park) for the dead, lanes
            # freed — so this very tick can refill them.
            for task_id, queue_state, action, _detail in self._rows(
                "SELECT * FROM backlog_ingest_results(%s)", (limits.planner,)
            ):
                report.ingested.append((task_id, action))

            # Reconcile technical DEFER_TO_USER parks back to OPEN (VOYN-W0-
            # AICC-DEFER-AUTO-RESUME) before selecting candidates, so a
            # resumed task is eligible in this very tick. The candidate query
            # mirrors the 0014 gate's own conditions purely as a FILTER --
            # so ineligible parks are not attempted (and not audit-spammed)
            # every tick; the SECURITY DEFINER function remains the only
            # authority and revalidates everything under the row lock.
            if limits.max_resumes_per_tick > 0:
                resumable = self._rows(
                    "SELECT t.task_id, park.reason FROM backlog_task t "
                    "CROSS JOIN LATERAL ("
                    "  SELECT e.reason, e.event_id FROM backlog_event e"
                    "   WHERE e.task_id = t.task_id"
                    "     AND e.event = 'return_to_pool'"
                    "     AND e.outcome = 'granted'"
                    "     AND e.detail->>'target' = 'DEFER_TO_USER'"
                    "   ORDER BY e.event_id DESC LIMIT 1"
                    ") park "
                    "WHERE t.status = 'DEFER_TO_USER' AND t.kind = 'task' "
                    "  AND park.reason LIKE 'cascade_exhausted:%%' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM backlog_event e2"
                    "     WHERE e2.task_id = t.task_id"
                    "       AND e2.outcome = 'granted'"
                    "       AND e2.event IN ('upsert', 'transition', 'triage',"
                    "                        'dispatch', 'return_to_pool',"
                    "                        'resume_deferred')"
                    "       AND e2.event_id > park.event_id) "
                    # The anti-ping-pong bound is a sliding WINDOW, not a
                    # lifetime score: three granted resumes within the
                    # trailing 48h say "this park re-arms itself faster than
                    # automation can help — a human should look". A lifetime
                    # count buried tasks forever: parks from the dead-codex
                    # era (2026-09) exhausted their 3 and stayed DEFER even
                    # after the pipeline that parked them was fixed
                    # (VOYN-W0-AICC-DEFER-AUTO-RESUME-REM).
                    "  AND (SELECT count(*) FROM backlog_event e"
                    "        WHERE e.task_id = t.task_id"
                    "          AND e.event = 'resume_deferred'"
                    "          AND e.outcome = 'granted'"
                    "          AND e.created_at > now() - interval '48 hours'"
                    "       ) < 3 "
                    "ORDER BY t.priority NULLS LAST, t.wave, t.task_id "
                    "LIMIT %s",
                    (limits.max_resumes_per_tick,),
                )
                for task_id, park_reason in resumable:
                    ok, _reason, _revision = self._row(
                        "SELECT * FROM backlog_resume_deferred(%s)", (task_id,)
                    )
                    if ok:
                        report.resumed.append((task_id, park_reason))

            # The review-backlog fence is DISPATCH-only backpressure: it
            # must never short-circuit anything above it in the tick. An
            # earlier version of this fence was a blanket `return report`
            # placed before the DEFER_TO_USER reconcile above, which meant a
            # full review backlog also silently froze parked-task recovery
            # for as long as PRs sat unreviewed -- a control whose blast
            # radius (the whole rest of the tick) was wider than its stated
            # purpose (gate new dispatch). Ingest and the resume reconcile
            # both run unconditionally above; only the candidate/dispatch
            # loop below is skipped when the fence fires.
            if limits.review_backlog_limit > 0:
                (review_backlog,) = self._row(
                    "SELECT count(DISTINCT t.task_id) FROM backlog_task t "
                    "JOIN backlog_evidence e "
                    "  ON e.task_id = t.task_id AND e.kind = 'pr' "
                    "WHERE t.status = 'READY_TO_REVIEW'",
                    (),
                )
                review_backlog = int(review_backlog)
                if review_backlog >= limits.review_backlog_limit:
                    report.review_window_full = review_backlog
                    return report

            candidates = self._rows(
                "SELECT task_id, wave, priority, title, body, repo, dispatchable "
                "FROM backlog_eligible"
            )
            for task_id, wave, priority, title, body, repo, dispatchable in candidates:
                if len(report.dispatched) >= limits.max_dispatches_per_tick:
                    break
                task = {
                    "task_id": task_id,
                    "wave": wave,
                    "priority": priority,
                    "title": title,
                    "body": body,
                    "repo": repo,
                }
                if not dispatchable:
                    report.undispatchable.append((task_id, "no_repo"))
                    continue
                route = repo_route(repo)
                if route is None:
                    # An unrouted repo is a report line, never a dispatch into
                    # a guaranteed dead-letter (the first live tick proved the
                    # worker refuses unknown projects three times, honestly).
                    report.undispatchable.append((task_id, "unknown_repo_route"))
                    continue

                # The reuse gate (VOYN-W0-AICC-DISPATCH-REUSE-GATE): only a
                # REM/RETRY candidate carries a source task to check, so an
                # ordinary first-time OPEN task skips straight to dispatch
                # below with no extra query or git call.
                source_task_id = self._reuse_anchor(task_id)
                if source_task_id is not None:
                    landed = self._reuse_lookup(
                        route[1], source_task_id, limits.reuse_check_branch
                    )
                    if landed is not None:
                        sha, subject = landed
                        ok, _reason, _revision = self._row(
                            "SELECT * FROM backlog_close_superseded(%s, %s, %s, %s)",
                            (task_id, source_task_id, sha, subject),
                        )
                        if ok:
                            report.superseded.append((task_id, source_task_id, sha))
                        else:
                            # The match still stands even if recording it lost
                            # a race (e.g. the task moved out of OPEN between
                            # the view read and this call) -- never fall
                            # through to dispatch on top of a detected
                            # duplicate; a future tick re-evaluates it fresh.
                            report.refused.append((task_id, f"reuse_close_failed:{_reason}"))
                        continue

                payload, budget = _payload_for(task, limits, route)
                ok, reason, work_item_id, _revision = self._row(
                    "SELECT * FROM backlog_dispatch(%s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        task_id,
                        limits.planner,
                        limits.lease_ttl_seconds,
                        limits.wip_limit,
                        json.dumps(payload),
                        budget,
                    ),
                )
                if ok:
                    report.dispatched.append((task_id, work_item_id))
                elif reason == "earlier_wave_has_eligible_work":
                    report.skipped_by_wave_gate.append((task_id, reason))
                elif reason == "wip_exhausted":
                    report.refused.append((task_id, reason))
                    break  # the cap is global; later candidates cannot pass
                else:
                    report.refused.append((task_id, reason))
        finally:
            # The global lease is per TICK; repo leases outlive it by design.
            self._row(
                "SELECT * FROM backlog_lease_release(%s, %s)",
                (_PLANNER_AUTHORITY, limits.planner),
            )
        return report


def plan_once(connection_factory: Any, limits: PlanLimits = PlanLimits()) -> PlanReport:
    return Planner(connection_factory).plan_once(limits)
