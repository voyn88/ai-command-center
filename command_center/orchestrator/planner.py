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

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from command_center.orchestrator.routing import cascade_for
from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = ["PlanLimits", "PlanReport", "plan_once"]

_PLANNER_AUTHORITY = "planner:global"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    planner: str = "aicc-planner"
    wip_limit: int = 4
    #: Repo leases must outlive the dispatched RUN (hours).
    lease_ttl_seconds: int = 7200
    #: The planner:global lease covers one TICK (seconds) — its own parameter
    #: (VOYN-W0-AICC-PLANNER-LEASE-TTL, resolved and pinned by
    #: test_the_tick_lease_uses_its_own_ttl_not_the_repo_horizon): a tick
    #: that dies before releasing must not lock every control host out for
    #: the repo-lease horizon.
    planner_lease_ttl_seconds: int = 300
    #: Per-tick dispatch cap, distinct from WIP: one tick must stay short.
    max_dispatches_per_tick: int = 4
    #: Wall-time envelope handed to each dispatched RUN. 900 s timed out 6 of
    #: the first 68 isolated implementation runs on 2026-09-09 (VOYN-W0-AICC-
    #: FLEET-LAST-MILE-PUBLISH); 45 min matches the launcher's own run bound
    #: while staying under the 2 h repo lease horizon above.
    timeout_seconds: int = 2700
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
    planner_busy: bool = False
    #: The review backlog count that tripped `PlanLimits.review_backlog_
    #: limit` this tick (None when the fence never fired). Ingest and the
    #: DEFER_TO_USER resume reconcile still ran -- only dispatch paused.
    review_window_full: int | None = None
    #: Pipeline tasks dispatched in decomposition mode this tick (the second
    #: non-technical return asked for a split; 0021).
    split_dispatched: list[str] = field(default_factory=list)
    #: (task, failure) pipeline tasks created this tick from open monitor
    #: findings (0021 monitor_finding).
    monitor_tasks: list[tuple[str, str]] = field(default_factory=list)
    #: Pipeline-class tasks dispatched THROUGH the review-backlog fence this
    #: tick: work that repairs CI, review, merge train, planner or queue must
    #: never wait behind the backlog it exists to drain
    #: (VOYN-W0-AICC-PLANNER-PIPELINE-CLASS-PRIORITY-AND-WINDOW-PAUSE).
    pipeline_bypass: list[str] = field(default_factory=list)
    #: The fence fired while no execution work item was ready or claimed:
    #: idle lanes are pure waste, so the tick dispatches its ordinary bounded
    #: batch anyway (backpressure resumes as soon as a lane has work).
    idle_trickle: bool = False
    #: Functional candidates held back by the fence this tick (count only).
    fenced: int = 0


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


#: The decomposition assignment for a pipeline task the fleet returned twice
#: without a technical cause (VOYN-W0-AICC-PLANNER-AUTO-SPLIT-PIPELINE-TASKS).
#: The run makes NO code change: its whole result is the trailer that
#: `backlog_ingest_results` turns into subtasks via `backlog_split_task`.
_SPLIT_INSTRUCTIONS = (
    "This task was returned twice by executors without a technical cause: it is "
    "too large for one run. Do NOT implement it and do NOT commit anything. "
    "Decompose it into 2 to 8 bounded subtasks, each closable by one run with one "
    "pull request and its own acceptance criteria and tests, ordered so that "
    "producers come before consumers. End your final message with a single line of "
    "exactly this form (one line, valid JSON, no code fences):\n"
    'SPLIT_TASKS_JSON: [{"suffix": "S1-<SHORT-NAME>", "title": "<imperative title>", '
    '"body": "<scope, acceptance, tests, files>", "priority": "P1"}, ...]\n'
    "Suffixes are uppercase [A-Z0-9-], unique, 2-40 chars; the orchestrator creates "
    "<this task id>-<suffix> for each entry and closes this task as SPLIT."
)


def _monitor_task_id(source: str, failure: str) -> str:
    """Deterministic, exact task id for a monitor finding: the same
    (source, failure) always maps to the same id, so re-opening a finding
    re-uses the task instead of creating a twin. The readable slug is
    followed by a digest of the UNMODIFIED pair: two findings that differ
    only in punctuation, case, or past the slug's length must not share an
    id (review of fc167cf7) -- the second upsert would have collided and
    that finding would have stayed unlinked forever."""
    slug = re.sub(r"[^A-Z0-9]+", "-", f"{source}-{failure}".upper()).strip("-")[:70]
    digest = hashlib.sha256(f"{source}\x00{failure}".encode("utf-8")).hexdigest()[:10].upper()
    return f"VOYN-MON-{slug}-{digest}"


def _split_requested(rows: Any, task_id: str) -> bool:
    """True when the task's latest granted return_to_pool asked for a split."""
    row = rows(
        "SELECT (e.detail ->> 'split_requested')::boolean "
        "FROM backlog_event e WHERE e.task_id = %s AND e.event = 'return_to_pool' "
        "AND e.outcome = 'granted' ORDER BY e.event_id DESC LIMIT 1",
        (task_id,),
    )
    return bool(row and row[0] and row[0][0])


def _payload_for(
    task: dict[str, Any], limits: PlanLimits, route: tuple[str, str], *, mode: str = "implement"
) -> tuple[dict[str, Any], int]:
    """The agent_run payload plus the attempt budget (= cascade length).

    ``mode="split"`` sends the decomposition assignment instead of the
    implementation contract; everything else (route, cascade, provenance) is
    identical, so a split run passes the same gates as any run.

    Prompt discipline: the task record IS the assignment — id, title, body
    travel verbatim; the worker's provenance gate still applies, and
    ``untrusted=False`` is on the authority of the planner being the control
    plane acting on the canonical store.
    """
    cascade = cascade_for("implementation")
    project_id, repository_path = route
    if mode == "split":
        prompt = (
            f"Central task: {task['task_id']} ({task['title']}).\n"
            f"Wave {task['wave']}, priority {task['priority'] or 'unset'}.\n\n"
            f"{task['body']}\n\n"
            f"{_SPLIT_INSTRUCTIONS}"
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
            "mode": "split",
        }
        return payload, len(cascade)
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
        # Follow-up (VOYN-W0-AICC-PUBLISH-PREP-UNTRACKED-FILES): a bare `git
        # commit` (or `git commit -a`) never stages a NEW file -- a created
        # migration stayed untracked, `agent_worktree_clean` still refused,
        # and the cascade parked again for the same reason. `git add -A`
        # (not `git add` alone) is what actually stages untracked work.
        "Commit every change you make to the task branch in this clone before "
        "you finish -- run `git add -A` (this also stages new files you "
        "created, such as a migration, which a plain `git add` or "
        "`git commit -a` would leave untracked) and then `git commit`. Run "
        "`git status --porcelain` afterward and confirm it prints nothing: "
        "an uncommitted or untracked change is discarded work, not a result. "
        "Do NOT push and do NOT open a pull request: you have no push "
        "capability, and the orchestrator publishes your commits through the "
        "guarded publisher after you exit.\n"
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

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

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

            # Fail-closed monitors record what they measured (0021
            # monitor_finding); every open finding becomes a pipeline task
            # once, so a red monitor is a task the fleet fixes rather than a
            # permanently failed unit (owner instruction 2026-09-08).
            for finding_id, source, failure in self._rows(
                "SELECT finding_id, source, failure FROM monitor_finding "
                "WHERE state = 'open' AND task_id IS NULL ORDER BY finding_id LIMIT 20",
                (),
            ):
                task_id_for_finding = _monitor_task_id(source, failure)
                ok, reason, _changed, _rev = self._row(
                    "SELECT * FROM backlog_upsert_task(%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        task_id_for_finding, "0", "P1", "OPEN", "task",
                        f"monitor finding: {failure} on {source}",
                        f"Recorded by the fail-closed monitor '{source}' (monitor_finding "
                        f"#{finding_id}): failure '{failure}'. Find the root cause and fix "
                        "it so the measurement is healthy again; the monitor clears the "
                        "finding when it measures healthy. Acceptance: the monitor reports "
                        "ok for 24h and the root-cause fix is merged with a regression test.",
                        "ai-command-center",
                    ),
                )
                if ok or reason == "unchanged":
                    self._exec(
                        "SELECT backlog_set_task_class(%s, 'pipeline')",
                        (task_id_for_finding,),
                    )
                    self._exec(
                        "SELECT monitor_link_task(%s, %s)", (finding_id, task_id_for_finding)
                    )
                    report.monitor_tasks.append((task_id_for_finding, failure))

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
            fence_active = False
            lanes_idle = False
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
                    fence_active = True
                    # Backpressure exists to keep review from drowning, not to
                    # idle the fleet: with nothing ready or claimed on the
                    # execution queue the fence holds nothing back this tick
                    # (observed 2026-09-08: four lanes idle for hours at
                    # review backlog 164 while accepted PRs waited on CI).
                    (live_items,) = self._row(
                        "SELECT count(*) FROM work_item_public "
                        "WHERE queue = 'execution' "
                        "  AND state IN ('ready', 'claimed')",
                        (),
                    )
                    lanes_idle = int(live_items) == 0
                    report.idle_trickle = lanes_idle

            candidates = self._rows(
                "SELECT task_id, wave, priority, title, body, repo, dispatchable, "
                "       task_class "
                "FROM backlog_eligible"
            )
            for (
                task_id, wave, priority, title, body, repo, dispatchable, task_class,
            ) in candidates:
                if len(report.dispatched) >= limits.max_dispatches_per_tick:
                    break
                if fence_active and not lanes_idle and task_class != "pipeline":
                    report.fenced += 1
                    continue
                if fence_active and task_class == "pipeline":
                    report.pipeline_bypass.append(task_id)
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
                mode = "split" if _split_requested(self._rows, task_id) else "implement"
                payload, budget = _payload_for(task, limits, route, mode=mode)
                if mode == "split":
                    report.split_dispatched.append(task_id)
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
