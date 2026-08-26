"""Read-only Python surface over the queue's status views (VOYN-W0-APP-CONTROL-S1).

`work_queue_store.py` is the WORKER'S surface (claim/heartbeat/complete/fail,
worker-role grants); `work_queue_admin.py` is RECOVERY (reap/redrive). What
neither offers is the control plane's ordinary question — "what is the queue
doing?" — asked over the read grants `aicc_app` already holds: `SELECT` on
`work_item_public`, `work_attempt_public` (the redacted view: `work_attempt`
itself holds `claim_token_hash` and is granted to nobody) and `work_result`.

Read-only by construction: this module contains no INSERT/UPDATE and no
protocol function calls, so handing it to an HTTP layer cannot widen the
mutation surface. Rows travel as plain dicts because their one consumer is a
JSON serializer; a dataclass here would be a second copy of the view's own
column contract.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["WorkQueueReadStore"]

_ITEM_COLUMNS = (
    "work_item_id, queue, idempotency_key, task_id, repository_id, priority, "
    "available_at, state, attempt_count, max_attempts, current_attempt_id, "
    "result_id, dead_reason, dead_at, created_at, updated_at"
)

_ATTEMPT_COLUMNS = (
    "attempt_id, work_item_id, attempt_no, claimed_by_role, visibility_seconds, "
    "visible_until, heartbeat_at, state, outcome_reason, result_id, "
    "created_at, updated_at"
)

_STATES = frozenset({"ready", "claimed", "succeeded", "dead"})


def _rows_to_dicts(columns: str, rows: list[tuple]) -> list[dict[str, Any]]:
    names = [column.strip() for column in columns.split(",")]
    out = []
    for row in rows:
        entry = dict(zip(names, row, strict=True))
        for key, value in entry.items():
            # timestamps and ids serialize as strings; jsonb may arrive as text
            if key.endswith("_at") or key == "visible_until":
                entry[key] = None if value is None else str(value)
        out.append(entry)
    return out


class WorkQueueReadStore:
    """Status reads over `work_item_public` / `work_attempt_public` / `work_result`."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    def list_items(
        self,
        *,
        queue: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Newest first. An unknown ``state`` filter returns an empty list
        rather than raising: the state vocabulary belongs to the schema, and
        an HTTP caller's typo is not a server error."""
        if state is not None and state not in _STATES:
            return []
        sql = f"SELECT {_ITEM_COLUMNS} FROM work_item_public"
        clauses, params = [], []
        if queue is not None:
            clauses.append("queue = %s")
            params.append(queue)
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(min(max(int(limit), 1), 500))
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return _rows_to_dicts(_ITEM_COLUMNS, cur.fetchall())

    def get_item(self, work_item_id: str) -> dict[str, Any] | None:
        """One item with its attempt trail and, when finished, its result.

        ``None`` for an unknown id — absence is the caller's 404, not an
        exception. The result body is read from ``work_result`` (an app-role
        grant recorded in roles.py): it is the coordination record the control
        plane exists to consume, bounded at write time by the worker's own
        tail limits.
        """
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM work_item_public WHERE work_item_id = %s",
                    (work_item_id,),
                )
                rows = cur.fetchall()
                if not rows:
                    return None
                item = _rows_to_dicts(_ITEM_COLUMNS, rows)[0]
                cur.execute(
                    f"SELECT {_ATTEMPT_COLUMNS} FROM work_attempt_public "
                    "WHERE work_item_id = %s ORDER BY attempt_no",
                    (work_item_id,),
                )
                item["attempts"] = _rows_to_dicts(_ATTEMPT_COLUMNS, cur.fetchall())
                item["result"] = None
                if item.get("result_id"):
                    cur.execute(
                        "SELECT payload FROM work_result WHERE result_id = %s",
                        (item["result_id"],),
                    )
                    found = cur.fetchall()
                    if found:
                        value = found[0][0]
                        item["result"] = (
                            json.loads(value) if isinstance(value, str) else value
                        )
        return item
