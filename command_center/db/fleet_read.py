"""Read-only fleet observability over the canonical PostgreSQL store.

The Streamlit dashboard historically read only local ``data/`` and the desktop
Session Supervisor, so the autonomous contour (control-01 / worker-01) was
invisible. This module is the control-plane read surface for that contour:
tasks by status/wave, the work queue by state with attempt reasons, recent
attempts per executor, open monitor findings, and merged-PR throughput.

Read-only by construction: no INSERT/UPDATE/DELETE and no protocol function
calls. Queue attempt rows travel through ``work_attempt_public`` (the base
``work_attempt`` table holds ``claim_token_hash`` and is granted to nobody).
Connection lifecycle follows every other store: ``connection_factory`` or the
process-wide ``command_center.db.pool``.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "FleetReadStore",
    "ensure_pool",
    "filter_nav_for_postgres",
    "postgres_configured",
]

_ATTEMPT_COLUMNS = (
    "attempt_id, work_item_id, attempt_no, claimed_by_role, state, "
    "outcome_reason, created_at, updated_at"
)


def postgres_configured(env: dict[str, str] | None = None) -> bool:
    """True when the process has the AICC_PG_* host needed to open a pool.

    Absence of ``AICC_PG_HOST`` means a desktop/local install with no server
    database — the Fleet page must hide, not render an empty dashboard.
    """
    source = os.environ if env is None else env
    return bool((source.get("AICC_PG_HOST") or "").strip())


def filter_nav_for_postgres(
    nav: dict[str, tuple[str, str]],
    env: dict[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Drop the Fleet nav entry when PostgreSQL is not configured."""
    if postgres_configured(env):
        return nav
    return {key: value for key, value in nav.items() if key != "fleet"}


def ensure_pool() -> None:
    """Open the process pool when configured and not already open.

    Streamlit has no FastAPI lifespan hook; the Fleet page opens the pool on
    first render so a tunnelled preprod session works without a separate
    startup step.
    """
    if not postgres_configured():
        return
    from command_center.db import pool

    try:
        pool.get_pool()
    except pool.PoolNotOpenError:
        pool.open_pool()


def _rows_to_dicts(columns: str, rows: list[tuple]) -> list[dict[str, Any]]:
    names = [column.strip() for column in columns.split(",")]
    out = []
    for row in rows:
        entry = dict(zip(names, row, strict=True))
        for key, value in entry.items():
            if key.endswith("_at") or key in {"opened_at", "seen_at", "day"}:
                entry[key] = None if value is None else str(value)
        out.append(entry)
    return out


class FleetReadStore:
    """Aggregates over backlog / queue / monitor tables for the Fleet page."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    def tasks_by_status_wave(self) -> list[dict[str, Any]]:
        """Counts of ``backlog_task`` rows grouped by status and wave."""
        sql = (
            "SELECT status, wave, count(*)::bigint AS count"
            " FROM backlog_task"
            " GROUP BY status, wave"
            " ORDER BY wave, status"
        )
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [
            {"status": status, "wave": wave, "count": int(count)}
            for status, wave, count in rows
        ]

    def queue_by_state(self, *, reason_limit: int = 20) -> dict[str, Any]:
        """Queue depth by state plus recent attempt / dead-letter reasons.

        ``counts`` is one row per ``work_item_public.state``. ``reasons`` lists
        the newest non-empty ``outcome_reason`` / ``dead_reason`` values so an
        operator can see *why* items sit in claimed/dead without opening each
        row.
        """
        bounded = min(max(int(reason_limit), 1), 100)
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, count(*)::bigint AS count"
                    " FROM work_item_public"
                    " GROUP BY state"
                    " ORDER BY state"
                )
                counts = [
                    {"state": state, "count": int(count)}
                    for state, count in cur.fetchall()
                ]
                cur.execute(
                    "SELECT work_item_id, state, dead_reason, updated_at"
                    " FROM work_item_public"
                    " WHERE dead_reason IS NOT NULL"
                    " ORDER BY updated_at DESC NULLS LAST"
                    " LIMIT %s",
                    (bounded,),
                )
                dead = _rows_to_dicts(
                    "work_item_id, state, dead_reason, updated_at",
                    cur.fetchall(),
                )
                cur.execute(
                    "SELECT attempt_id, work_item_id, claimed_by_role, state,"
                    " outcome_reason, updated_at"
                    " FROM work_attempt_public"
                    " WHERE outcome_reason IS NOT NULL"
                    " ORDER BY updated_at DESC NULLS LAST"
                    " LIMIT %s",
                    (bounded,),
                )
                attempts = _rows_to_dicts(
                    "attempt_id, work_item_id, claimed_by_role, state,"
                    " outcome_reason, updated_at",
                    cur.fetchall(),
                )
        return {"counts": counts, "dead_reasons": dead, "attempt_reasons": attempts}

    def recent_attempts_per_executor(
        self, *, per_executor: int = 10, executor_limit: int = 20
    ) -> list[dict[str, Any]]:
        """Newest attempts for each ``claimed_by_role`` (executor principal)."""
        per = min(max(int(per_executor), 1), 50)
        executors = min(max(int(executor_limit), 1), 100)
        sql = (
            "WITH ranked AS ("
            "  SELECT attempt_id, work_item_id, attempt_no, claimed_by_role,"
            "         state, outcome_reason, created_at, updated_at,"
            "         row_number() OVER ("
            "           PARTITION BY claimed_by_role"
            "           ORDER BY created_at DESC NULLS LAST"
            "         ) AS rn"
            "  FROM work_attempt_public"
            "), top_executors AS ("
            "  SELECT claimed_by_role"
            "  FROM work_attempt_public"
            "  GROUP BY claimed_by_role"
            "  ORDER BY max(created_at) DESC NULLS LAST"
            "  LIMIT %s"
            ")"
            " SELECT r.attempt_id, r.work_item_id, r.attempt_no,"
            "        r.claimed_by_role, r.state, r.outcome_reason,"
            "        r.created_at, r.updated_at"
            " FROM ranked r"
            " JOIN top_executors t ON t.claimed_by_role = r.claimed_by_role"
            " WHERE r.rn <= %s"
            " ORDER BY r.claimed_by_role, r.created_at DESC NULLS LAST"
        )
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (executors, per))
                return _rows_to_dicts(_ATTEMPT_COLUMNS, cur.fetchall())

    def open_monitor_findings(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Open ``monitor_finding`` rows, including any linked ``task_id``."""
        bounded = min(max(int(limit), 1), 500)
        sql = (
            "SELECT finding_id, source, failure, task_id, opened_at, seen_at"
            " FROM monitor_finding"
            " WHERE state = 'open'"
            " ORDER BY opened_at DESC"
            " LIMIT %s"
        )
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (bounded,))
                rows = cur.fetchall()
        out = []
        for finding_id, source, failure, task_id, opened_at, seen_at in rows:
            out.append(
                {
                    "finding_id": int(finding_id),
                    "source": source,
                    "failure": failure,
                    "task_id": task_id,
                    "opened_at": None if opened_at is None else str(opened_at),
                    "seen_at": None if seen_at is None else str(seen_at),
                }
            )
        return out

    def merged_prs_per_day(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Merge receipts (``backlog_evidence.kind = 'sha'``) counted per UTC day.

        Recording a merge SHA is the backlog's durable proof that a PR landed;
        grouping those rows by day is the throughput chart the Fleet page shows.
        """
        bounded_days = min(max(int(days), 1), 365)
        sql = (
            "SELECT (recorded_at AT TIME ZONE 'UTC')::date AS day,"
            "       count(*)::bigint AS merges"
            " FROM backlog_evidence"
            " WHERE kind = 'sha'"
            "   AND recorded_at >= (now() AT TIME ZONE 'UTC')::date"
            "                     - (%s::int - 1) * INTERVAL '1 day'"
            " GROUP BY 1"
            " ORDER BY 1 DESC"
        )
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (bounded_days,))
                rows = cur.fetchall()
        return [
            {"day": None if day is None else str(day), "merges": int(merges)}
            for day, merges in rows
        ]
