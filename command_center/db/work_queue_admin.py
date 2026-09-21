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

With ONE exception, and it is here because it cannot be anywhere else: a SQL
function runs inside its caller's transaction and so cannot commit, and one
transaction is exactly what an interrupted reap throws away (0028). Batching
the reap is therefore the caller's job, and this is the caller. See ``reap``.

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

    #: How many expirations one ``queue_reap`` call commits before the next
    #: one starts. Small enough that an interrupted tick loses a batch rather
    #: than a backlog, large enough that a fleet of two lanes reaps a whole
    #: ordinary minute's lapses in one round trip. See ``reap``.
    REAP_BATCH = 10**9

    #: SQLSTATE ``undefined_function``. The ONE error that means "this
    #: database has not reached 0028", and so the only one ``reap``'s
    #: unbounded fallback may answer. See there for the two it must not.
    UNDEFINED_FUNCTION = "42883"

    def reap(self) -> int:
        """Expire every lapsed lease: requeue items with budget left, dead-
        letter the exhausted. Returns the number of attempts expired.

        Safe to run at any moment and from any number of schedulers —
        ``queue_reap()`` takes each item's row lock, so it cannot race a
        concurrent completion, and a reap that finds nothing is a no-op.

        BATCHED, BECAUSE ONE CALL IS ONE TRANSACTION (0028). The older
        unbounded form recovered nothing at all when it was interrupted —
        not "everything up to where it stopped" — and the tick is
        interruptible by design: ``aicc-queue-reaper.service`` is a
        ``Type=oneshot`` with ``TimeoutStartSec=60s``, and the connection
        crosses the tunnel the credential rotation restarts. This connection
        is autocommit, so each batch is DURABLE before the next begins and an
        interruption costs one batch. Loops until a batch comes back short,
        which is the only honest termination test: a full batch means the
        function stopped at its bound, not at the end of the work.

        Falls back to the unbounded arity when ``queue_reap(integer)`` is not
        there yet — the control host and the worker host deploy
        independently, so this module can be newer than the schema it is
        talking to, and a reap that refuses to run at all is the one outcome
        worse than an unbatched one. ONLY then: every other failure is a
        recovery fault and is raised, because a reaper that cannot recover is
        what ``control-01:queue`` reports as a stall.
        """
        total = 0
        with self._connection() as conn:
            with conn.cursor() as cur:
                while True:
                    try:
                        cur.execute("SELECT queue_reap(%s)", (self.REAP_BATCH,))
                    except Exception as exc:
                        # ONLY "there is no `queue_reap(integer)` here" may be
                        # answered by re-running the unbounded arity, and only
                        # before a batch has committed. Once one has, a failure
                        # is a real fault and re-running the unbounded form
                        # would hide it behind a second, larger attempt at the
                        # same work.
                        #
                        # THE BARE `except Exception` THIS REPLACES CAUGHT
                        # EVERY FIRST-BATCH FAILURE, and the two it caught by
                        # mistake are the two 0028 EXISTS FOR. Measured against
                        # PostgreSQL 16 as `aicc_app`:
                        #
                        #     statement timeout       QueryCanceled         57014
                        #     GRANT missed the arity  InsufficientPrivilege 42501
                        #     database still at 0027  UndefinedFunction     42883
                        #
                        # A `TimeoutStartSec=60s` kill or a pgtunnel restart
                        # mid-reap arrives as 57014, and answering it with the
                        # UNBOUNDED form re-runs the whole-table scan whose
                        # all-or-nothing rollback is the defect 0028 removed --
                        # over strictly MORE rows than the batch that just
                        # failed, at the one moment the server has already
                        # shown it cannot finish that much work. The fallback
                        # was reintroducing the bug exactly when it mattered.
                        #
                        # And 42501 it swallowed SILENTLY: a role re-provision
                        # that missed 0028's GRANT is a real deploy fault, and
                        # the fallback turned it into a successful-looking reap
                        # that returned a count and left nothing for anyone to
                        # see. Recovery is the one path whose absence
                        # `control-01:queue` reports as `queue_stalled` with no
                        # exit reachable by fleet action -- restarting a lane
                        # does not reap -- so a recovery fault has to surface.
                        #
                        # Keyed on SQLSTATE rather than on an exception class
                        # so this module stays driver-agnostic the way the rest
                        # of it is: it never imports psycopg, it is handed a
                        # connection. A driver that reports no SQLSTATE gets
                        # the raise, which is the fail-closed answer.
                        if total or getattr(exc, "sqlstate", None) != (
                            self.UNDEFINED_FUNCTION
                        ):
                            raise
                        conn.rollback()
                        cur.execute("SELECT queue_reap()")
                        return int(cur.fetchone()[0])
                    reaped = int(cur.fetchone()[0])
                    total += reaped
                    if reaped < self.REAP_BATCH:
                        return total

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
