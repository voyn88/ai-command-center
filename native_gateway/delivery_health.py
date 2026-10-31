"""Pipeline health for the owner apps: queue, lanes, escalations, monitor.

The producer side of the snapshot's ``pipeline_health`` block. It answers the
owner's "is the autonomous pipeline alive, and does it need me?" from the
control-plane database — the one place those facts live — and nothing else:

- work queue state counts (``work_item_public``): claimable ready, claimed,
  dead, the oldest claimable item's age, and items finished in the last hour;
- active lanes: distinct ``claimed_by_role`` holding an *active* attempt whose
  visibility lease is still in the future (``work_attempt_public``);
- executor authentication failures in the last 30 minutes (attempt outcome
  reasons matching the agents' expired-login vocabulary) — a COUNT only; the
  reason text never leaves the database;
- backlog counts by status and the ``DEFER_TO_USER`` tasks as owner
  escalations (``backlog_task``: id, title, priority, updated_at only);
- the infra monitor's latest verdict from ``monitor_finding`` (open failure
  CODES only, never the measured detail), when that table is readable.

The reads go through `PipelineHealthSource`, so the producer is testable with
a fake source and the gateway itself never opens a database connection: the
block is written into the projection artifact like everything else, and the
gateway's allowlist mappers and redaction scan apply to it unchanged.

Every read uses the views/tables the control role ``aicc_app`` already holds
SELECT on (see `command_center/db/roles.py`); this module issues no write.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)

#: Attempt outcome reasons that mean "the executor's login is gone" — the
#: owner has to re-authenticate the agent; no retry will fix it.
AUTH_FAILURE_PATTERN = "failed to authenticate|oauth session expired|refresh token"
AUTH_FAILURE_WINDOW_MINUTES = 30
SUCCEEDED_WINDOW_MINUTES = 60

_ESCALATION_LIMIT = 50
_MONITOR_FAILURE_LIMIT = 20
_TITLE_LIMIT = 160
# Named, not inline: ruff-format under the py314 target rewrites an inline
# except-tuple to PEP 758 syntax, a SyntaxError on the 3.12/3.13 runtimes.
_NUMBER_ERRORS = (TypeError, ValueError)

# The backlog_task status vocabulary (0005_backlog_store CHECK constraint);
# anything else is not projected.
BACKLOG_STATUSES = frozenset(
    {
        "OPEN",
        "IN_PROGRESS",
        "READY_TO_REVIEW",
        "DONE",
        "UNTRIAGED",
        "DEFER_TO_USER",
        "SPLIT",
        "NEEDS_REFINEMENT",
        "DECIDED",
    }
)


@dataclass(frozen=True)
class PipelineRows:
    """Raw facts read from the database, before allowlist shaping."""

    queue: dict[str, Any]
    lanes: list[dict[str, Any]] = field(default_factory=list)
    auth_failures: int = 0
    backlog_counts: dict[str, int] = field(default_factory=dict)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    # None = the monitor's table is unavailable; the block then omits it.
    monitor: dict[str, Any] | None = None


class PipelineHealthSource(Protocol):
    def read(self) -> PipelineRows: ...


_QUEUE_SQL = (
    "SELECT"
    " count(*) FILTER (WHERE state = 'ready' AND available_at <= now()) AS ready,"
    " count(*) FILTER (WHERE state = 'claimed') AS claimed,"
    " count(*) FILTER (WHERE state = 'dead') AS dead,"
    " count(*) FILTER (WHERE state = 'succeeded'"
    f"   AND updated_at > now() - interval '{SUCCEEDED_WINDOW_MINUTES} minutes')"
    "   AS succeeded_last_hour,"
    " EXTRACT(EPOCH FROM (now() - min(available_at)"
    "   FILTER (WHERE state = 'ready' AND available_at <= now())))"
    "   AS oldest_ready_seconds"
    " FROM work_item_public"
)

_LANES_SQL = (
    "SELECT claimed_by_role, count(*) AS attempts,"
    " EXTRACT(EPOCH FROM (now() - min(coalesce(heartbeat_at, created_at))))"
    "   AS heartbeat_age_seconds"
    " FROM work_attempt_public"
    " WHERE state = 'active' AND visible_until > now()"
    " GROUP BY claimed_by_role"
    " ORDER BY claimed_by_role"
)

_AUTH_SQL = (
    "SELECT count(*) FROM work_attempt_public"
    f" WHERE updated_at > now() - interval '{AUTH_FAILURE_WINDOW_MINUTES} minutes'"
    " AND outcome_reason ~* %s"
)

_BACKLOG_SQL = "SELECT status, count(*) FROM backlog_task GROUP BY status"

_ESCALATIONS_SQL = (
    "SELECT task_id, title, priority, updated_at FROM backlog_task"
    " WHERE status = 'DEFER_TO_USER'"
    " ORDER BY priority ASC NULLS LAST, updated_at DESC"
    " LIMIT %s"
)

_MONITOR_OPEN_SQL = (
    "SELECT source, failure, opened_at, seen_at FROM monitor_finding"
    " WHERE state = 'open' ORDER BY seen_at DESC LIMIT %s"
)

_MONITOR_LAST_SQL = (
    "SELECT max(greatest(seen_at, coalesce(cleared_at, seen_at))) FROM monitor_finding"
)


def _stamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _seconds(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except _NUMBER_ERRORS:
        return None


class PostgresPipelineHealthSource:
    """`PipelineHealthSource` over the control-plane database (read-only)."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._factory = connection_factory

    def read(self) -> PipelineRows:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUEUE_SQL)
                ready, claimed, dead, succeeded, oldest = cur.fetchone()
                queue = {
                    "ready": int(ready or 0),
                    "claimed": int(claimed or 0),
                    "dead": int(dead or 0),
                    "succeeded_last_hour": int(succeeded or 0),
                    "oldest_ready_age_seconds": _seconds(oldest),
                }
                cur.execute(_LANES_SQL)
                lanes = [
                    {
                        "role": role,
                        "attempts": int(attempts),
                        "heartbeat_age_seconds": _seconds(age),
                    }
                    for role, attempts, age in cur.fetchall()
                ]
                cur.execute(_AUTH_SQL, (AUTH_FAILURE_PATTERN,))
                auth_failures = int(cur.fetchone()[0] or 0)
                cur.execute(_BACKLOG_SQL)
                backlog_counts = {str(s): int(n) for s, n in cur.fetchall()}
                cur.execute(_ESCALATIONS_SQL, (_ESCALATION_LIMIT,))
                escalations = [
                    {
                        "task_id": task_id,
                        "title": title,
                        "priority": priority,
                        "updated_at": _stamp(updated_at),
                    }
                    for task_id, title, priority, updated_at in cur.fetchall()
                ]
            monitor = self._read_monitor(conn)
        return PipelineRows(
            queue=queue,
            lanes=lanes,
            auth_failures=auth_failures,
            backlog_counts=backlog_counts,
            escalations=escalations,
            monitor=monitor,
        )

    @staticmethod
    def _read_monitor(conn: Any) -> dict[str, Any] | None:
        """The monitor table is optional context: absent or unreadable → None."""
        try:
            with conn.cursor() as cur:
                cur.execute(_MONITOR_OPEN_SQL, (_MONITOR_FAILURE_LIMIT,))
                open_rows = cur.fetchall()
                cur.execute(_MONITOR_LAST_SQL)
                last = cur.fetchone()[0]
        except Exception:  # noqa: BLE001 -- optional block; logged, never fatal
            _LOG.warning(
                "monitor_finding unavailable for pipeline health", exc_info=True
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
        return {
            "open": [
                {
                    "source": source,
                    "failure": failure,
                    "opened_at": _stamp(opened),
                    "seen_at": _stamp(seen),
                }
                for source, failure, opened, seen in open_rows
            ],
            "last_seen_at": _stamp(last),
        }


def _text(value: object, limit: int = _TITLE_LIMIT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _code(failure: object) -> str | None:
    """A monitor failure's stable code (before ':'), never its measurement."""
    if not isinstance(failure, str):
        return None
    code = failure.split(":", 1)[0]
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", code) else None


def build_pipeline_health(rows: PipelineRows, now: datetime | None = None) -> dict:
    """Shape raw rows into the projection's ``pipeline_health`` block.

    Allowlist only: every field below is named; free text is limited to task
    titles (whitespace-collapsed, length-bounded) and monitor failure codes.
    The gateway's mapper and redaction scan run over this again.
    """
    now = now or datetime.now(UTC)
    queue = rows.queue or {}
    block: dict[str, Any] = {
        "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "queue": {
            "ready": _count(queue.get("ready")),
            "claimed": _count(queue.get("claimed")),
            "dead": _count(queue.get("dead")),
            "succeeded_last_hour": _count(queue.get("succeeded_last_hour")),
            "oldest_ready_age_seconds": (
                queue.get("oldest_ready_age_seconds")
                if isinstance(queue.get("oldest_ready_age_seconds"), int)
                else None
            ),
        },
        "active_lanes": [
            {
                "role": role,
                "attempts": _count(lane.get("attempts")),
                "heartbeat_age_seconds": (
                    lane.get("heartbeat_age_seconds")
                    if isinstance(lane.get("heartbeat_age_seconds"), int)
                    else None
                ),
            }
            for lane in rows.lanes
            if (role := _text(lane.get("role"), 80))
        ],
        "executor_auth_failures_30m": _count(rows.auth_failures),
        "backlog_by_status": {
            status: _count(n)
            for status, n in sorted(rows.backlog_counts.items())
            if status in BACKLOG_STATUSES
        },
        "owner_escalations": [
            {
                "task_id": task_id,
                "title": _text(item.get("title")) or task_id,
                "priority": _text(item.get("priority"), 8),
                "updated_at": _text(item.get("updated_at"), 40),
            }
            for item in rows.escalations[:_ESCALATION_LIMIT]
            if (task_id := _text(item.get("task_id"), 120))
        ],
    }
    if rows.monitor is not None:
        open_rows = [r for r in rows.monitor.get("open") or [] if isinstance(r, dict)]
        failures = sorted(
            {code for r in open_rows if (code := _code(r.get("failure")))}
        )[:_MONITOR_FAILURE_LIMIT]
        block["monitor"] = {
            "verdict": "failing" if open_rows else "unknown",
            "open_findings": len(open_rows),
            "failures": failures,
            "last_seen_at": _text(rows.monitor.get("last_seen_at"), 40),
        }
    return block


def lanes_from_health(block: dict) -> list[dict]:
    """Active pipeline lanes in the projection's generic ``lanes`` shape."""
    return [
        {
            "id": lane["role"],
            "state": "active",
            "heartbeat_age_seconds": lane["heartbeat_age_seconds"],
        }
        for lane in block.get("active_lanes", [])
    ]


def postgres_source_from_env() -> tuple[
    PostgresPipelineHealthSource, Callable[[], None]
]:
    """Open the control DB pool from ``AICC_PG_*`` and return (source, close)."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    return PostgresPipelineHealthSource(pool.connection), pool.close_pool
