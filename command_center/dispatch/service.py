"""Orchestration for the dispatch policy layer.

Assembles the inputs the pure engine needs from the *existing* primitives —
never reimplementing them:

* queued tasks       -> `tasks_repository.load_tasks` (single reader of the board)
* executor pool      -> `executors.EXECUTORS` + their live availability probes
* permitted per task -> `project_config.allowed_execution_providers`
* daily spend        -> `task_pipeline.daily_spend_usd` (the trailing-24h primitive)
* spend ceiling      -> `pipeline_settings.max_daily_spend_usd`
* kill switch        -> `pipeline_settings.enabled` (the master switch the
                        `task_pipeline.kill_switch` sets off)

`plan(...)` is a dry run (no writes). `assign(...)` applies the plan by
recording the chosen executor onto each assigned task **through
`tasks_repository`** (the board's single writer) — it never launches a
process and never touches supervisor semantics; the existing pipeline picks
the task up and launches it on the recorded executor.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from command_center import executors as executors_module
from command_center import pipeline_settings, project_config, tasks_repository
from command_center import task_pipeline
from command_center.project_config import is_sensitive
from command_center.dispatch import policy_config
from command_center.dispatch.models import (
    DispatchPlan,
    DispatchPolicy,
    ExecutorProfile,
    QueuedTask,
)
from command_center.dispatch.policy import plan_dispatch
from command_center.runtime import db as runtime_db

if TYPE_CHECKING:  # a type-only import: the service layer stays free of FastAPI
    from command_center.http_auth.identity import Principal

_LOG = logging.getLogger(__name__)

# Kanban statuses that mean "waiting to be dispatched" — not yet running,
# not in review, not done. These are the only tasks a dispatch plan considers.
QUEUED_STATUSES = frozenset({"Backlog", "Next"})

# Run states that occupy an executor's concurrency slot right now.
_ACTIVE_RUN_STATES = frozenset(runtime_db.EXECUTION_CENTER_ACTIVE_STATES)


# --------------------------------------------------------------------------
# Input collection
# --------------------------------------------------------------------------


def collect_executor_pool(policy: DispatchPolicy) -> list[ExecutorProfile]:
    """Snapshot the executor registry into policy-facing profiles, resolving
    each executor's per-task cost from the policy's cost matrix and its
    availability from its live probe (unavailable ones are kept in the pool but
    marked, so the engine can emit `no_available_executor` rather than pretend
    they don't exist)."""
    profiles: list[ExecutorProfile] = []
    for executor in executors_module.EXECUTORS.values():
        try:
            available = executor.available
        except Exception:  # noqa: BLE001 — a flaky probe means "not available"
            available = False
        profiles.append(
            ExecutorProfile(
                id=executor.id,
                label=executor.label,
                kind=executor.kind,
                is_local=policy.is_local(executor.id),
                available=available,
                cost_per_task_usd=policy.cost_for(executor.id),
            )
        )
    return profiles


def _permitted_executors(task: dict) -> frozenset[str] | None:
    """The executors this task is allowed to run on, per project policy. None
    means unconstrained; an empty set means the policy explicitly forbids all
    (fail closed — a malformed policy yields no eligible executor)."""
    project = task.get("project")
    if not project:
        return None
    try:
        return frozenset(project_config.allowed_execution_providers(project))
    except Exception:  # noqa: BLE001 — malformed policy disables execution
        return frozenset()


def collect_queued_tasks(root: Path) -> list[QueuedTask]:
    """Read the board and reduce the queued tasks to the engine's view.

    Applies the BANK/LEGAL redaction policy at collection: a task whose project
    :func:`is_sensitive` is dropped before it reaches the engine, so a sensitive
    project's ``id`` and ``project_ref`` never surface in a dispatch plan (the
    same per-read exclusion the conflicts/audit surfaces enforce). Dispatch of
    sensitive work is out of scope for this operator-facing plane entirely — the
    task is not merely redacted in the response, it is never considered.
    """
    queued: list[QueuedTask] = []
    for task in tasks_repository.load_tasks(root):
        if task.get("status") not in QUEUED_STATUSES:
            continue
        if is_sensitive(task.get("project") or ""):
            continue
        pinned = task.get("executor") if task.get("executor_pinned") else None
        queued.append(
            QueuedTask(
                id=str(task.get("id")),
                project=task.get("project"),
                priority=task.get("priority") or "Medium",
                allowed_executors=_permitted_executors(task),
                pinned_executor=pinned,
                sla_deadline=task.get("sla_deadline") or task.get("deadline"),
                created_at=task.get("created_at"),
            )
        )
    return queued


def active_by_executor(db_path: Path) -> dict[str, int]:
    """Count currently-active runs per executor, so per-agent concurrency
    limits account for work already in flight. Read-only; a query failure
    yields an empty map (the concurrency guard then only counts this plan's own
    assignments, which is still safe — it can never *raise* a limit)."""
    counts: dict[str, int] = {}
    try:
        with runtime_db.connect(db_path) as conn:
            rows = conn.execute("SELECT provider_id, state FROM run").fetchall()
    except Exception:  # noqa: BLE001
        return counts
    for row in rows:
        try:
            state = row["state"]
            # An executor id IS its provider id in this codebase (`executors`
            # wire each executor to `providers.get_provider(id)`), so the run's
            # `provider_id` is the executor whose concurrency slot it occupies.
            executor = row["provider_id"]
        except Exception:  # noqa: BLE001
            continue
        if state in _ACTIVE_RUN_STATES and executor:
            counts[executor] = counts.get(executor, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Plan (dry run) and Assign (apply)
# --------------------------------------------------------------------------


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else runtime_db.resolve_db_path()


def plan(root: Path, *, db_path: Path | None = None) -> DispatchPlan:
    """Dry run: what would be assigned, and why. No writes."""
    resolved_db = _resolve_db_path(db_path)
    policy = policy_config.load_policy(root)
    settings = pipeline_settings.load_settings(root)

    # The kill switch IS the master switch: `task_pipeline.kill_switch` persists
    # `enabled=False`. Dispatch must never launch while it is off.
    kill_switch_engaged = not settings.enabled

    try:
        spend = task_pipeline.daily_spend_usd(resolved_db)
    except Exception:  # noqa: BLE001 — no cost data => fail closed (assume ceiling hit)
        _LOG.warning(
            "dispatch.plan: daily_spend_usd failed, fail-closed at the ceiling "
            "(%.2f) — dispatch blocked until this is resolved",
            settings.max_daily_spend_usd or 0.0,
            exc_info=True,
        )
        spend = settings.max_daily_spend_usd or 0.0

    return plan_dispatch(
        collect_queued_tasks(root),
        collect_executor_pool(policy),
        policy,
        daily_spend_usd=spend,
        max_daily_spend_usd=settings.max_daily_spend_usd,
        kill_switch_engaged=kill_switch_engaged,
        active_by_executor=active_by_executor(resolved_db),
    )


def assign(
    root: Path,
    principal: "Principal",
    *,
    confirmed: bool,
    db_path: Path | None = None,
) -> dict:
    """Compute the plan and APPLY its assignments.

    There is no `actor` parameter, and that is the point: the caller is a
    `Principal`, a type only the HTTP boundary's platform verification can
    construct, so an actor cannot be declared by whoever calls this. Passing
    `actor="whoever"` is a `TypeError` at the call site rather than a value
    written to the record — the same property `queue_claim()` gets by taking no
    claimant argument at all (VOYN-W0-AICC-AUTH-HTTP-01, SRV-04b).

    Applying means: record the chosen executor onto each assigned task through
    `tasks_repository` (the board's single writer). It does **not** launch a
    process — the existing pipeline does that on the recorded executor, so
    supervisor semantics are untouched.

    Fail-closed: if the kill switch is engaged, nothing is applied even when
    the caller passed `confirmed=True`. `confirmed` is a required explicit
    opt-in for the *write*, mirroring every other mutating action in this
    codebase.
    """
    computed = plan(root, db_path=db_path)

    if not confirmed:
        return {
            "applied": False,
            "reason": "confirmation_required",
            "plan": computed.as_dict(),
        }
    if computed.kill_switch_engaged:
        return {
            "applied": False,
            "reason": "kill_switch_engaged",
            "plan": computed.as_dict(),
        }

    assignments = computed.assignments
    applied_ids: list[str] = []
    if assignments:
        by_id = {a.task_id: a.assigned_executor for a in assignments}

        def _mutator(tasks: list[dict]) -> list[str]:
            touched: list[str] = []
            for task in tasks:
                executor = by_id.get(task.get("id"))
                if executor is None:
                    continue
                task["executor"] = executor
                task["agent"] = executor
                task["updated_at"] = _now()
                touched.append(task["id"])
            return touched

        applied_ids = tasks_repository.mutate_tasks(root, _mutator)

    return {
        "applied": True,
        "actor": principal.principal_id,
        "assigned_task_ids": applied_ids,
        "plan": computed.as_dict(),
    }


def _now() -> str:
    from command_center import models

    return models.iso_now()
