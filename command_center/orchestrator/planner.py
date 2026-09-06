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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from command_center.orchestrator.routing import cascade_for
from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = [
    "PlanLimits",
    "PlanReport",
    "RepoRouteAudit",
    "audit_repo_routes",
    "plan_once",
]

_PLANNER_AUTHORITY = "planner:global"


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
    #: How long a candidate must have been continuously unroutable (no repo,
    #: or a repo with no route table entry) before `backlog_park_unroutable`
    #: parks it NEEDS_REFINEMENT instead of just reporting it again this tick
    #: (VOYN-W0-AICC-REPO-ROUTE-AND-EVIDENCE-HYGIENE). Long enough that an
    #: operator adding a missing route within the day never races a park;
    #: short enough that a genuinely repo-less task does not sit silently
    #: unrouted forever.
    unroutable_grace_seconds: int = 86400


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
    #: (task, undispatchable reason) — a candidate `backlog_park_unroutable`
    #: moved OPEN -> NEEDS_REFINEMENT this tick, having been continuously
    #: unroutable past the grace period (VOYN-W0-AICC-REPO-ROUTE-AND-
    #: EVIDENCE-HYGIENE). Distinct from `undispatchable`: every unroutable
    #: candidate lands there every tick, but only the ones that cross the
    #: grace period also land here.
    parked: list[tuple[str, str]] = field(default_factory=list)
    planner_busy: bool = False


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

    def __init__(self, connection_factory: Any) -> None:
        self._factory = connection_factory

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def _row(self, sql: str, params: tuple[Any, ...]) -> tuple:
        return self._rows(sql, params)[0]

    def _note_unroutable(
        self, task_id: str, reason: str, limits: PlanLimits, report: PlanReport
    ) -> None:
        """Record one more unroutable observation and, past the grace
        period, let `backlog_park_unroutable` move the task OPEN ->
        NEEDS_REFINEMENT (VOYN-W0-AICC-REPO-ROUTE-AND-EVIDENCE-HYGIENE) —
        so a repo that never gets a route stops being reported, identically,
        forever."""
        ok, verdict_reason, _revision = self._row(
            "SELECT * FROM backlog_park_unroutable(%s, %s, %s)",
            (task_id, reason, limits.unroutable_grace_seconds),
        )
        if ok and verdict_reason == "NEEDS_REFINEMENT":
            report.parked.append((task_id, reason))

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
                    "  AND (SELECT count(*) FROM backlog_event e"
                    "        WHERE e.task_id = t.task_id"
                    "          AND e.event = 'resume_deferred'"
                    "          AND e.outcome = 'granted') < 3 "
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
                    self._note_unroutable(task_id, "no_repo", limits, report)
                    continue
                route = repo_route(repo)
                if route is None:
                    # An unrouted repo is a report line, never a dispatch into
                    # a guaranteed dead-letter (the first live tick proved the
                    # worker refuses unknown projects three times, honestly).
                    report.undispatchable.append((task_id, "unknown_repo_route"))
                    self._note_unroutable(task_id, "unknown_repo_route", limits, report)
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


@dataclass(slots=True)
class RepoRouteAudit:
    """Report-only: see `audit_repo_routes`. Never changes a task's status —
    OPEN candidates missing a route are `plan_once`'s job (which, past the
    grace period, parks them NEEDS_REFINEMENT); a READY_TO_REVIEW task
    already carries real work product, so a route gap there is an operator
    fact (add the route) rather than something to park."""

    #: (task_id, repo or "") — OPEN, kind='task', repo missing or unrouted.
    open_unrouted: list[tuple[str, str]] = field(default_factory=list)
    #: (task_id, repo or "") — READY_TO_REVIEW, repo missing or unrouted.
    ready_to_review_unrouted: list[tuple[str, str]] = field(default_factory=list)


def audit_repo_routes(connection_factory: Any) -> RepoRouteAudit:
    """Every OPEN or READY_TO_REVIEW task's repo, checked against the route
    table `repo_route()` reads (VOYN-W0-AICC-REPO-ROUTE-AND-EVIDENCE-
    HYGIENE). The planner and review ticks already refuse to act on an
    unrouted repo rather than dead-lettering into one; this is the
    complementary read the ticket asked for -- the operator's answer to
    "which routes are actually missing," across both statuses in one pass,
    without waiting for either tick's own report to surface it."""
    audit = RepoRouteAudit()
    with connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, repo FROM backlog_task "
                "WHERE kind = 'task' AND status = 'OPEN' "
                "ORDER BY task_id"
            )
            open_rows = cur.fetchall()
            cur.execute(
                "SELECT task_id, repo FROM backlog_task "
                "WHERE kind = 'task' AND status = 'READY_TO_REVIEW' "
                "ORDER BY task_id"
            )
            review_rows = cur.fetchall()
    for task_id, repo in open_rows:
        if not repo or repo_route(repo) is None:
            audit.open_unrouted.append((task_id, repo or ""))
    for task_id, repo in review_rows:
        if not repo or repo_route(repo) is None:
            audit.ready_to_review_unrouted.append((task_id, repo or ""))
    return audit
