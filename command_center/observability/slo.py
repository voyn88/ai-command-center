"""Programmatic SLOs for invariants this session watched break live.

Confirmed false by audit: no Prometheus/Grafana alert rules exist anywhere
in this repository (`risk_alerts.py`/`alert_store.py` are AML customer-risk
alerting — a different domain that happens to share the word "alert"). This
module is the pipeline's own alerting surface, over three invariants that
have already broken in production, not hypothetical ones:

* **No two mutating attempts on one worktree** — each mutating backlog task
  gets exactly one isolated worktree (`backlog/<task_id>`); two claimed
  attempts against the same task_id means two writers were let into it.
* **No active attempt without a heartbeat** — `worker.daemon`'s heartbeat
  thread beats every ``visibility_seconds / 3`` (see its own docstring); an
  attempt still `claimed` that has missed two consecutive beats has a
  process that stopped reporting itself alive before the lease formally
  lapsed.
* **No review cycle without a terminal outcome inside its SLA** — a task in
  `READY_TO_REVIEW` is an open review cycle; it must reach `DONE` or
  `REJECTED` before the SLA elapses. Direct consequence of
  VOYN-W0-AICC-REVIEW-STUCK-ON-TRANSIENT-FAILURE, found live this session:
  a review cycle that stalls on a transient failure has no other signal
  that says so.

The check functions are pure — they take plain rows (dicts, whatever a
`SELECT` or a test fixture hands them) and a clock, and return
:class:`SloViolation` values. ``evaluate_slos`` is the aggregator; the
alerting side effect (structured ERROR-level log lines — this deployment's
only alerting channel, per the audit above) is `fire_alerts`, kept separate
so a check can be asserted against in a test without a logger in the loop.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "DEFAULT_REVIEW_SLA_SECONDS",
    "DEFAULT_HEARTBEAT_MISSED_BEATS",
    "DEFAULT_HEARTBEAT_GRACE_SECONDS",
    "SloViolation",
    "check_duplicate_mutating_attempts",
    "check_missing_heartbeat",
    "check_review_cycle_sla",
    "evaluate_slos",
    "fire_alerts",
]

#: A review cycle open longer than this is stuck, not merely slow —
#: generous enough that a normal cascade (multi-attempt review + rework)
#: fits comfortably inside it.
DEFAULT_REVIEW_SLA_SECONDS = 4 * 3600

#: `worker.daemon._heartbeat_loop`'s own tolerance: "two consecutive beats
#: may fail... before the lease actually lapses" — a third missed beat is
#: no longer within the daemon's own documented tolerance.
DEFAULT_HEARTBEAT_MISSED_BEATS = 3

#: Fallback when a row carries no `visibility_seconds` to derive a beat
#: interval from.
DEFAULT_HEARTBEAT_GRACE_SECONDS = 300

_ALERT_LOGGER = logging.getLogger("command_center.alerts")


@dataclass(frozen=True, slots=True)
class SloViolation:
    """One fired alert: which invariant, which task, and why."""

    slo: str
    task_id: str | None
    detail: str
    context: dict[str, Any] = field(default_factory=dict)


def _as_datetime(value: Any) -> datetime | None:
    """Accept a psycopg-native ``datetime`` or an ISO string; ``None`` for
    either a NULL column or an unparseable value — a missing timestamp is
    the caller's fact to act on, not this function's to raise over."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def check_duplicate_mutating_attempts(
    attempts: list[dict[str, Any]],
) -> list[SloViolation]:
    """Two attempts `claimed` at once for the same backlog task_id means two
    writers were let into the one worktree that task_id owns.

    ``attempts`` rows need ``task_id``, ``work_item_id``, ``attempt_id`` and
    ``state`` (the attempt's own state, ``work_attempt_public.state`` —
    ``"active"`` is the only live/mutating value; ``work_item.state`` is
    the one that spells its equivalent ``"claimed"``, a different column
    on a different table). A row with no task_id (a non-backlog queue
    item) has no worktree-per-task rule to violate.
    """
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        task_id = row.get("task_id")
        if task_id is None or row.get("state") != "active":
            continue
        by_task[str(task_id)].append(row)

    violations: list[SloViolation] = []
    for task_id, rows in by_task.items():
        if len(rows) < 2:
            continue
        violations.append(
            SloViolation(
                slo="no_duplicate_mutating_attempts_per_worktree",
                task_id=task_id,
                detail=(
                    f"{len(rows)} attempts claimed at once for task {task_id!r}; "
                    "its worktree has one writer by design"
                ),
                context={
                    "attempt_ids": sorted(str(r.get("attempt_id")) for r in rows),
                    "work_item_ids": sorted(str(r.get("work_item_id")) for r in rows),
                },
            )
        )
    return violations


def check_missing_heartbeat(
    attempts: list[dict[str, Any]],
    *,
    now: datetime,
    missed_beats: int = DEFAULT_HEARTBEAT_MISSED_BEATS,
    default_grace_seconds: float = DEFAULT_HEARTBEAT_GRACE_SECONDS,
) -> list[SloViolation]:
    """An attempt still `claimed` whose heartbeat has gone quiet longer than
    the daemon's own beat cadence allows.

    Rows need ``task_id``, ``attempt_id``, ``state`` (``work_attempt_public.state``
    — ``"active"`` is the live value; see :func:`check_duplicate_mutating_attempts`),
    ``heartbeat_at``, ``created_at`` and — to derive the real cadence rather
    than a fixed guess — ``visibility_seconds`` (``worker.daemon``'s beat
    interval is ``visibility_seconds / 3``, mirrored here).
    """
    violations: list[SloViolation] = []
    for row in attempts:
        if row.get("state") != "active":
            continue
        visibility_seconds = row.get("visibility_seconds")
        if isinstance(visibility_seconds, (int, float)) and visibility_seconds > 0:
            interval = max(visibility_seconds / 3.0, 1.0)
            grace_seconds = interval * missed_beats
        else:
            grace_seconds = default_grace_seconds

        heartbeat_at = _as_datetime(row.get("heartbeat_at"))
        reference = heartbeat_at or _as_datetime(row.get("created_at"))
        if reference is None:
            continue
        age = (now - reference).total_seconds()
        if age <= grace_seconds:
            continue
        violations.append(
            SloViolation(
                slo="no_active_attempt_without_heartbeat",
                task_id=(
                    str(row["task_id"]) if row.get("task_id") is not None else None
                ),
                detail=(
                    f"attempt {row.get('attempt_id')} has not "
                    f"{'heartbeat' if heartbeat_at else 'been claimed'} in "
                    f"{age:.0f}s (grace {grace_seconds:.0f}s)"
                ),
                context={
                    "attempt_id": row.get("attempt_id"),
                    "work_item_id": row.get("work_item_id"),
                    "heartbeat_at": row.get("heartbeat_at"),
                    "age_seconds": round(age, 1),
                    "grace_seconds": round(grace_seconds, 1),
                },
            )
        )
    return violations


def check_review_cycle_sla(
    tasks: list[dict[str, Any]],
    *,
    now: datetime,
    sla_seconds: float = DEFAULT_REVIEW_SLA_SECONDS,
) -> list[SloViolation]:
    """A task in `READY_TO_REVIEW` longer than the SLA without reaching a
    terminal outcome (`DONE`/`REJECTED`).

    Rows need ``task_id``, ``status`` and ``updated_at`` — `backlog_transition`
    stamps `updated_at` on every move, so for a task still in
    `READY_TO_REVIEW`, `now - updated_at` is exactly how long this review
    cycle has been open (0010_review_cycle_remediation.up.sql).
    """
    violations: list[SloViolation] = []
    for row in tasks:
        if row.get("status") != "READY_TO_REVIEW":
            continue
        updated_at = _as_datetime(row.get("updated_at"))
        if updated_at is None:
            continue
        age = (now - updated_at).total_seconds()
        if age <= sla_seconds:
            continue
        task_id = row.get("task_id")
        violations.append(
            SloViolation(
                slo="no_review_cycle_beyond_sla",
                task_id=str(task_id) if task_id is not None else None,
                detail=(
                    f"task {task_id!r} has been READY_TO_REVIEW for "
                    f"{age:.0f}s, past the {sla_seconds:.0f}s SLA"
                ),
                context={
                    "updated_at": row.get("updated_at"),
                    "age_seconds": round(age, 1),
                    "sla_seconds": sla_seconds,
                },
            )
        )
    return violations


def evaluate_slos(
    *,
    attempts: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    review_sla_seconds: float = DEFAULT_REVIEW_SLA_SECONDS,
    heartbeat_missed_beats: int = DEFAULT_HEARTBEAT_MISSED_BEATS,
) -> list[SloViolation]:
    """Run every invariant this module knows about and return every
    violation found — the one entry point ``slo-check`` (and tests) call."""
    clock = now or datetime.now(timezone.utc)
    attempts = attempts or []
    tasks = tasks or []
    violations: list[SloViolation] = []
    violations.extend(check_duplicate_mutating_attempts(attempts))
    violations.extend(
        check_missing_heartbeat(
            attempts, now=clock, missed_beats=heartbeat_missed_beats
        )
    )
    violations.extend(
        check_review_cycle_sla(tasks, now=clock, sla_seconds=review_sla_seconds)
    )
    return violations


def fire_alerts(
    violations: list[SloViolation], *, logger: logging.Logger | None = None
) -> None:
    """The alerting side effect: one ERROR-level structured log line per
    violation on the ``command_center.alerts`` logger — this deployment's
    only alert sink (no Prometheus Alertmanager/Grafana is wired up; see
    this module's docstring). Wire a systemd ``OnFailure=`` unit, a log
    shipper alert rule, or a textfile collector off of this logger/the
    `slo-check` CLI's exit code — deliberately not invented here."""
    target = logger or _ALERT_LOGGER
    for violation in violations:
        target.error(
            json.dumps(
                {
                    "alert": violation.slo,
                    "task_id": violation.task_id,
                    "detail": violation.detail,
                    **violation.context,
                },
                sort_keys=True,
                default=str,
            )
        )
