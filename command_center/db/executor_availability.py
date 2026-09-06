"""Python surface over the fleet-wide executor availability protocol (0018,
VOYN-W0-AICC-EXECUTOR-QUOTA-VISIBILITY).

Migration ``0018_executor_availability`` shipped the whole protocol as
PL/pgSQL — ``executor_mark_unavailable``, ``executor_mark_available``, and the
``executor_availability`` / ``executor_availability_event`` tables — and until
this module, nothing in Python called any of it. This wrapper is deliberately
thin for the same reason ``work_queue_store.py`` is: the database owns the
cooldown/completeness semantics (the ``executor_availability_unavailable_is_
complete`` constraint), and duplicating them here would create a second
authority.

A short in-process cache sits in front of ``get()`` (module-level, shared by
every ``ExecutorAvailabilityStore`` instance in the process — mirroring
``runtime.providers._probe_cache``): the preflight path can ask about the same
executor several times within one dispatch's cascade-fallback loop, and this
is a fact every worker process on the fleet asks about on every single claim,
so an uncached read would turn one incident into a steady stream of round
trips. The TTL is short (seconds, not the availability window itself, which is
minutes-to-hours and lives in ``unavailable_until``) so a fresh
``executor_mark_available`` operator override is visible almost immediately.

Import purity: ``command_center.db`` promises that importing it pulls in
neither ``aios_db`` nor ``psycopg``. The pool is resolved on use, exactly as
``work_queue_store.py`` does, and the connection factory is injectable for
tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = ["ExecutorAvailability", "ExecutorAvailabilityStore", "clear_cache"]

_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, "ExecutorAvailability"]] = {}
_cache_lock = threading.Lock()

# A row past its own `unavailable_until` reads back as available -- the
# cooldown is a TTL, not a mark that only an explicit `executor_mark_available`
# or a fresh `executor_mark_unavailable` can move (see 0018's own docstring:
# "clear it early rather than waiting out a guess", which only makes sense if
# waiting the guess out is itself a way to become available again). The
# comparison runs in Postgres, against Postgres's own `now()`, both to avoid
# trusting a caller's clock and to keep the semantics in one place shared by
# every reader (`get()`, `list_all()`, and `work_queue_read.py`'s dashboard
# query) rather than duplicated as a Python-side time check in each.
_LIVE_STATUS_SQL = (
    "CASE WHEN status = 'unavailable' AND unavailable_until > now()"
    " THEN status ELSE 'available' END"
)
_LIVE_REASON_SQL = (
    "CASE WHEN status = 'unavailable' AND unavailable_until > now() THEN reason END"
)
_LIVE_UNTIL_SQL = (
    "CASE WHEN status = 'unavailable' AND unavailable_until > now()"
    " THEN unavailable_until END"
)


def clear_cache() -> None:
    """Drop every memoized verdict. Tests that mark/clear an executor mid-run
    call this so the next read observes the new state immediately."""
    with _cache_lock:
        _cache.clear()


@dataclass(frozen=True, slots=True)
class ExecutorAvailability:
    """One executor's fleet-wide verdict. ``reason``/``unavailable_until`` are
    ``None`` together exactly when ``status == "available"`` — the same
    completeness the database's own CHECK constraint enforces, mirrored here
    so a caller never has to special-case a half-filled row."""

    executor_id: str
    status: str  # "available" | "unavailable"
    reason: str | None
    unavailable_until: str | None

    @property
    def available(self) -> bool:
        return self.status == "available"


def _default_available(executor_id: str) -> ExecutorAvailability:
    # No row for `executor_id` means it has never been marked unavailable —
    # the table holds only the cases that need attention, not a seeded roster.
    return ExecutorAvailability(executor_id, "available", None, None)


class ExecutorAvailabilityStore:
    """Read the live verdict, or report one, over the shipped SQL protocol."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    def get(self, executor_id: str, *, use_cache: bool = True) -> ExecutorAvailability:
        """The live verdict for ``executor_id`` — the O(1) hot-path read a
        preflight makes before every dispatch."""
        if use_cache:
            now = time.monotonic()
            with _cache_lock:
                cached = _cache.get(executor_id)
                if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                    return cached[1]
        result = self._get_uncached(executor_id)
        with _cache_lock:
            _cache[executor_id] = (time.monotonic(), result)
        return result

    def _get_uncached(self, executor_id: str) -> ExecutorAvailability:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_LIVE_STATUS_SQL}, {_LIVE_REASON_SQL}, {_LIVE_UNTIL_SQL}"
                    " FROM executor_availability WHERE executor_id = %s",
                    (executor_id,),
                )
                row = cur.fetchone()
        if row is None:
            return _default_available(executor_id)
        status, reason, unavailable_until = row
        return ExecutorAvailability(
            executor_id,
            status,
            reason,
            None if unavailable_until is None else str(unavailable_until),
        )

    def list_all(self) -> list[ExecutorAvailability]:
        """Every executor that has ever been marked — the roster for a
        dashboard or metrics read, not the dispatch hot path (uncached, and
        deliberately not merged with the static executor registry: this
        module knows only what the database has recorded)."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT executor_id, {_LIVE_STATUS_SQL}, {_LIVE_REASON_SQL},"
                    f" {_LIVE_UNTIL_SQL} FROM executor_availability ORDER BY executor_id"
                )
                rows = cur.fetchall()
        return [
            ExecutorAvailability(
                executor_id,
                status,
                reason,
                None if unavailable_until is None else str(unavailable_until),
            )
            for executor_id, status, reason, unavailable_until in rows
        ]

    def mark_unavailable(self, executor_id: str, reason: str, ttl_seconds: int) -> None:
        """Report ``executor_id`` unavailable for ``ttl_seconds`` — a worker's
        own observation (quota, auth, ...), never a guess about the
        provider's true reset time (see the migration's own docstring)."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT executor_mark_unavailable(%s, %s, %s)",
                    (executor_id, reason, ttl_seconds),
                )
        with _cache_lock:
            _cache.pop(executor_id, None)

    def mark_available(self, executor_id: str) -> None:
        """Clear a mark early — an operator override, or a worker's own
        signal that a later run through the same executor actually
        succeeded."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT executor_mark_available(%s)", (executor_id,))
        with _cache_lock:
            _cache.pop(executor_id, None)
