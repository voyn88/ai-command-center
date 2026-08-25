"""Connection pooling for the AICC server database.

A pool rather than per-request connections because PostgreSQL forks a backend
process per connection: at the request rates the dispatcher and the worker
fleet generate, connect-per-query would spend more time forking backends than
running queries, and an unbounded connection count is the standard way to take
a PostgreSQL server down.

The pool itself — opening it, proving the open, counting it — is generic
PostgreSQL machinery and belongs to `aios-db` (VOYN-W0-AIOS-DB-01), reached
through `command_center.db.adapter`. What stays here is the part that is about
*this* service: which configuration the pool is built from, and the fact that
there is exactly one of it per process.

That singleton is why this module exists at all rather than callers holding
their own pool. It is opened explicitly (`open_pool`) and closed explicitly
(`close_pool`) rather than lazily on first use, so a process that cannot reach
the database fails at startup — where an operator sees it — instead of on the
first request that happens to need data.

`connection()` yields a connection with autocommit on. Transactions are opened
deliberately via `conn.transaction()` at the call site; an implicit transaction
per checkout is how a long-lived pooled connection ends up holding an idle
transaction open and blocking VACUUM. Autocommit is also what the migration
runner and the advisory-lock primitives require.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from command_center.db.config import PostgresConfig, load_config

__all__ = [
    "PoolNotOpenError",
    "close_pool",
    "PoolReplacedError",
    "connection",
    "get_pool",
    "open_pool",
    "pool_stats",
    "replace_pool",
]

_LOG = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: Any = None
# Monotonic generation token identifying each pool object for the checkout
# bookkeeping below. id() is NOT usable as that key: after a pool is closed
# and garbage-collected, CPython can hand the same address to a NEW pool,
# and a still-unwinding checkout of the dead pool would then decrement the
# new pool's counter (ABA; independent-review finding on d6fa8be).
_pool_token: int = 0
_config: PostgresConfig | None = None
_active: dict[int, int] = {}
_retired: dict[int, Any] = {}


class PoolNotOpenError(RuntimeError):
    """Raised when the pool is used before `open_pool()` or after `close_pool()`."""


class PoolReplacedError(RuntimeError):
    """Raised when a replacement lost its race: the pool state moved (another
    replace or a shutdown) while the new pool was being built. Distinct from
    PoolNotOpenError -- the pool may well be open, just not the state the
    caller observed (review note on 3a845a3)."""


def open_pool(config: PostgresConfig | None = None):
    """Open the process-wide pool and verify connectivity. Idempotent."""
    global _pool, _config

    with _lock:
        if _pool is not None:
            return _pool[1]
        resolved = config or load_config()
        pool = _build_pool(resolved)
        globals()["_pool_token"] += 1
        _pool = (globals()["_pool_token"], pool)
        _config = resolved
        _LOG.info("postgres pool open: %s", resolved.redacted())
        return pool


def _build_pool(config: PostgresConfig):
    from command_center.db import adapter

    # `aios_db.open_pool` waits for the first connections and closes the
    # half-built pool if they fail, so a bad DSN, an unreachable host or a
    # rejected certificate surfaces before it can replace the working pool.
    return adapter.open_pool(
        config.conninfo(),
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        timeout=config.pool_timeout_seconds,
        checkout_timeout=config.pool_timeout_seconds,
        autocommit=True,
        name="aicc",
    )


def replace_pool(config: PostgresConfig):
    """Atomically replace the process pool after credential rotation.

    The replacement is opened and connectivity-checked *before* the old pool
    is detached. Existing checkouts stay valid against PostgreSQL's established
    sessions; the old pool is retired only after its last checkout returns.
    New heartbeats/queue calls immediately use the replacement. This is what
    makes a credential reload safe in the middle of a 3600-second agent job.
    """

    global _pool, _config

    with _lock:
        if _pool is None:
            raise PoolNotOpenError(
                "PostgreSQL pool is not open. Call open_pool() during startup."
            )
        expected_token = _pool[0]
    replacement = _build_pool(config)
    close_now: Any | None = None
    with _lock:
        if _pool is None or _pool[0] != expected_token:
            # close_pool() (or another replace) won the race while the
            # replacement was being built. Installing it anyway would
            # resurrect a live database pool after shutdown -- new work could
            # start during process teardown (independent-review finding on
            # 0e3dad6). Fail closed: discard the replacement.
            orphaned = replacement
            replacement = None
        else:
            previous = _pool
            globals()["_pool_token"] += 1
            _pool = (globals()["_pool_token"], replacement)
            _config = config
            previous_token, previous_pool = previous
            if _active.get(previous_token, 0) == 0:
                close_now = previous_pool
            else:
                _retired[previous_token] = previous_pool
    if replacement is None:
        orphaned.close()
        raise PoolReplacedError(
            "PostgreSQL pool state changed during replacement; not installed."
        )
    if close_now is not None:
        close_now.close()
    _LOG.info("postgres pool replaced: %s", config.redacted())
    return replacement


def get_pool():
    """Return the open pool, or raise `PoolNotOpenError`.

    Checkouts taken through this handle bypass the `_active` bookkeeping:
    during `replace_pool` they are NOT protected by the retire-after-last-
    return contract -- use `connection()` for anything that outlives a
    moment (review note on 3a845a3).
    """
    with _lock:
        current = _pool
        if current is None:
            raise PoolNotOpenError(
                "PostgreSQL pool is not open. Call open_pool() during startup."
            )
        return current[1]


def close_pool() -> None:
    """Close the pool and drop the cached config. Safe to call when not open."""
    global _pool, _config

    closing: list[Any]
    with _lock:
        closing = ([] if _pool is None else [_pool[1]]) + list(_retired.values())
        _pool = None
        _config = None
        _retired.clear()
        _active.clear()
    for item in closing:
        item.close()


@contextmanager
def connection() -> Iterator:
    """Check a connection out of the pool for the duration of the block."""
    with _lock:
        if _pool is None:
            raise PoolNotOpenError(
                "PostgreSQL pool is not open. Call open_pool() during startup."
            )
        selected_id, selected = _pool
        _active[selected_id] = _active.get(selected_id, 0) + 1
    try:
        with selected.connection() as conn:
            yield conn
    finally:
        close_retired: Any | None = None
        with _lock:
            count = _active.get(selected_id)
            if count is None:
                # close_pool() ran while this checkout was in flight and
                # already cleared the bookkeeping and closed every pool --
                # including this one. There is nothing left to decrement or
                # close here; raising KeyError would crash the caller's
                # thread during shutdown and mask its real exception
                # (independent-review finding on 2d5687c).
                pass
            elif count > 1:
                _active[selected_id] = count - 1
            else:
                _active.pop(selected_id, None)
                close_retired = _retired.pop(selected_id, None)
        if close_retired is not None:
            close_retired.close()


def pool_stats() -> dict[str, int]:
    """Pool counters for the readiness probe and metrics."""
    from command_center.db import adapter

    return adapter.pool_stats(get_pool())
