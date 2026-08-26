"""The control-plane surface over the queue's recovery protocol (SRV-06).

Migration ``0002_queue_claim`` shipped recovery as PL/pgSQL — ``queue_reap()``
requeues or dead-letters every lapsed lease, ``work_dlq`` is the dead-letter
queue's interface, ``queue_redrive()`` is its audited exit — and granted all
three to ``aicc_app`` and nothing to workers. Until this module, nothing in
production called any of them: the reaper existed only for tests, which means
a worker host that lost power held its items hostage for exactly as long as
no human ran ``SELECT queue_reap()`` by hand.

This wrapper is deliberately thin, for the same reason ``work_queue_store.py``
is: the database owns the semantics (row locks against racing completions,
attempt budgets, backoff arithmetic, audit rows), and duplicating any of it
here would create a second authority. What Python adds is only the operator
seam — a callable the CLI and the reaper timer can reach.

Identity: these are ``aicc_app`` privileges by design. A worker may not reap
(a compromised host must not be able to expire the fleet's leases) and may
not redrive (the DLQ's exit is an operator decision). The connection's own
authenticated role is the authorisation; this module never sends one.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DeadLetter", "WorkQueueAdmin"]


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One dead-lettered item, as ``work_dlq`` presents it: the terminal
    state, the preserved cause, and the attempt trail's summary."""

    work_item_id: str
    queue: str
    task_id: str | None
    repository_id: str | None
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    dead_reason: str
    dead_at: str
    attempts_recorded: int
    last_attempt_reason: str | None


class WorkQueueAdmin:
    """Reap, list dead letters, redrive — over the shipped SQL protocol."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    # -- recovery -------------------------------------------------------------

    def reap(self) -> int:
        """Expire every lapsed lease: requeue items with budget left, dead-
        letter the exhausted. Returns the number of attempts expired.

        Safe to run at any moment and from any number of schedulers —
        ``queue_reap()`` takes each item's row lock, so it cannot race a
        concurrent completion, and a reap that finds nothing is a no-op.
        That idempotence is what makes it a timer's job rather than a
        daemon's: a missed tick delays recovery, it never corrupts it.
        """
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT queue_reap()")
                return int(cur.fetchone()[0])

    # -- the dead-letter queue ------------------------------------------------

    def dead_letters(
        self, queue: str | None = None, *, limit: int = 50
    ) -> list[DeadLetter]:
        """The DLQ, newest death first. ``queue=None`` lists every queue."""
        sql = (
            "SELECT work_item_id, queue, task_id, repository_id, idempotency_key,"
            " attempt_count, max_attempts, dead_reason, dead_at,"
            " attempts_recorded, last_attempt_reason FROM work_dlq"
        )
        params: tuple[Any, ...] = ()
        if queue is not None:
            sql += " WHERE queue = %s"
            params = (queue,)
        sql += " ORDER BY dead_at DESC LIMIT %s"
        params += (max(int(limit), 1),)
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [
            DeadLetter(
                work_item_id=row[0],
                queue=row[1],
                task_id=row[2],
                repository_id=row[3],
                idempotency_key=row[4],
                attempt_count=int(row[5]),
                max_attempts=int(row[6]),
                dead_reason=row[7],
                dead_at=str(row[8]),
                attempts_recorded=int(row[9]),
                last_attempt_reason=row[10],
            )
            for row in rows
        ]

    def redrive(self, work_item_id: str, *, extra_attempts: int = 1) -> bool:
        """Return a dead-lettered item to 'ready' with a raised budget.

        ``False`` is a refusal, not an error: the id is unknown, or the item
        is not dead — both audited by the function itself. Redelivery of a
        permanently failing payload is bounded again by the new budget, so a
        redrive can never reopen an unbounded retry loop.
        """
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT queue_redrive(%s, %s)",
                    (work_item_id, max(int(extra_attempts), 1)),
                )
                return bool(cur.fetchone()[0])
