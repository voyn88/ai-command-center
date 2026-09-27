"""The `_connection()` branch every PostgreSQL mirror falls to in production.

`PostgresQueueMirror`, `PostgresTableMirror` (and so `PostgresOwnerItemMirror`
and `PostgresConflictMirror`) all share the same two-branch `_connection()`:
an injected `connection_factory` when the caller supplies one, and
`command_center.db.pool.connection()` when it does not. Every mirror test in
this package — `test_queue_store.py`, `test_owner_item_store.py`,
`test_conflict_store.py` — builds its mirror with `connection_factory=
pg_connection_factory`, which always takes the first branch. Production takes
the second: `_mirror_owner_item` and `_mirror_conflict` both construct their
mirror with no factory at all, and nothing before this file ever ran that line.

Harmless while these mirrors stay non-load-bearing and their own failures stay
swallowed — but that is exactly the pairing (untested branch, swallowed
exception) that turns a merely-empty mirror into one indistinguishable from a
healthy one, until reconciliation runs. Two things are pinned here for all
three mirrors: that the pool branch really does reach a real PostgreSQL, and
that when the pool is unavailable the failure is swallowed at the write hook
and reported — not hidden — by `divergence`.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.db import pool
from command_center.db.config import PostgresConfig
from command_center.db.conflict_store import PostgresConflictMirror
from command_center.db.conflict_store import divergence as conflict_divergence
from command_center.db.mirror_support import MIRROR_UNAVAILABLE
from command_center.db.owner_item_store import PostgresOwnerItemMirror
from command_center.db.owner_item_store import divergence as owner_item_divergence
from command_center.db.pool import PoolNotOpenError
from command_center.db.queue_store import PostgresQueueMirror
from command_center.runtime.db import conflict as conflict_db
from command_center.runtime.db import wave1


def _queue_entry(entry_id: str) -> dict:
    return {
        "id": entry_id,
        "task_id": f"task-{entry_id}",
        "project": "demo",
        "state": "queued",
        "reason": None,
        "run_id": None,
        "added_at": "2026-08-13T00:00:00",  # naive local, what `models.iso_now()` emits
        "evaluated_at": None,
        "launched_at": None,
    }


def _owner_item(item_id: str) -> dict:
    return {
        "id": item_id,
        "title": f"item {item_id}",
        "detail": None,
        "due": None,
        "done": 0,
        "source_ref": None,
        "version": 0,
        "created_at": "2026-08-13T00:00:00",
        "updated_at": "2026-08-13T00:00:00",
        "project_ref": None,
    }


def _conflict(conflict_id: str) -> dict:
    return {
        "id": conflict_id,
        "kind": "merge",
        "source_ref": "incident:1",
        "severity": "sev3",
        "status": "open",
        "owner": None,
        "mitigation": None,
        "project_ref": None,
        "opened_at": "2026-08-13T00:00:00",
        "resolved_at": None,
        "version": 0,
        "created_at": "2026-08-13T00:00:00",
        "updated_at": "2026-08-13T00:00:00",
    }


def _dummy_config() -> PostgresConfig:
    # `_build_pool` is monkeypatched out in every test that opens the pool
    # here, so none of these values is ever dialled. A config is still built
    # so `pool.open_pool` is driven the way it is in production, not through a
    # shortcut that skips its own contract.
    return PostgresConfig(
        host="127.0.0.1",
        port=5432,
        dbname="aicc",
        user="aicc_worker",
        password="x" * 32,
        sslmode="disable",
        sslrootcert=None,
        connect_timeout=5,
        application_name="test",
        pool_min_size=1,
        pool_max_size=2,
        pool_timeout_seconds=5,
        statement_timeout_ms=30_000,
    )


class _FakeCheckoutPool:
    """Stands in for what `aios_db.open_pool` returns.

    Only the bottom layer — a real pooling implementation — is faked. The
    `command_center.db.pool` singleton around it (the lock, the generation
    token, the active-checkout bookkeeping `connection()` does on every
    checkout and return) is the real module, unpatched, and it hands out a
    connection to the already-migrated per-test database.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self.closed = False

    @contextmanager
    def connection(self):
        with self._factory() as conn:
            yield conn

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def open_real_pool(monkeypatch, pg_connection_factory):
    """Open the real `command_center.db.pool` singleton, so a mirror built
    with no `connection_factory` — exactly how `_mirror_owner_item` and
    `_mirror_conflict` construct theirs — resolves `pool.connection()` for
    real rather than through the injected-factory shortcut every other mirror
    test uses.
    """
    monkeypatch.setattr(
        pool, "_build_pool", lambda config: _FakeCheckoutPool(pg_connection_factory)
    )
    pool.close_pool()
    pool.open_pool(_dummy_config())
    try:
        yield
    finally:
        pool.close_pool()


@pytest.fixture
def pool_not_open():
    """Guarantee the pool-unavailable case regardless of pool state left
    behind by another test in the same worker process."""
    pool.close_pool()
    yield
    pool.close_pool()


# --- the pool branch is actually reached, on all three mirrors --------------


def test_queue_mirror_resolves_the_real_pool(open_real_pool) -> None:
    mirror = PostgresQueueMirror()
    entry = _queue_entry("a")

    mirror.replace_entries([entry])

    assert mirror.list_entries() == [entry]


def test_owner_item_mirror_resolves_the_real_pool(open_real_pool) -> None:
    mirror = PostgresOwnerItemMirror()
    row = _owner_item("a")

    mirror.upsert(row)

    assert mirror.list_records() == [row]


def test_conflict_mirror_resolves_the_real_pool(open_real_pool) -> None:
    mirror = PostgresConflictMirror()
    row = _conflict("a")

    mirror.upsert(row)

    assert mirror.list_records() == [row]


# --- pool unavailable: failure is swallowed, divergence reports it ----------


def test_queue_mirror_fails_closed_when_the_pool_is_not_open(pool_not_open) -> None:
    # Nothing wires `PostgresQueueMirror` into a write hook yet — unlike the
    # other two, its only caller today is this test package — so there is no
    # swallowing frame to prove around it. What was untested and is provable
    # here is that the branch reaches the real pool module and fails closed
    # (a raised, identifiable error) rather than doing something that could
    # read as success further up.
    mirror = PostgresQueueMirror()

    with pytest.raises(PoolNotOpenError):
        mirror.list_entries()


def test_owner_item_mirror_failure_is_swallowed_and_divergence_reports_it(
    pool_not_open, tmp_path
) -> None:
    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    # `_mirror_owner_item` builds `PostgresOwnerItemMirror()` with no factory,
    # exactly as production does. With the pool never opened this raises
    # `PoolNotOpenError`, and it must not escape the authoritative write.
    created = wave1.create_owner_item(db_path, title="survives without postgres")

    assert wave1.get_owner_item(db_path, created["id"])["title"] == "survives without postgres"

    reported = owner_item_divergence([created], PostgresOwnerItemMirror())
    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]
    assert "PoolNotOpenError" in reported[0]["detail"]


def test_conflict_mirror_failure_is_swallowed_and_divergence_reports_it(
    pool_not_open, tmp_path
) -> None:
    db_path = tmp_path / "runtime.db"
    conflict_db.db.migrate(db_path)

    created = conflict_db.create_conflict(db_path, kind="merge", source_ref="incident:1")

    assert conflict_db.get_conflict(db_path, created["id"])["status"] == "open"

    reported = conflict_divergence([created], PostgresConflictMirror())
    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]
    assert "PoolNotOpenError" in reported[0]["detail"]
