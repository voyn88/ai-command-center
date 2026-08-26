"""The Python store over the claim protocol, proved against real PostgreSQL.

`tests/db/test_queue_claim.py` proves the SQL protocol; `tests/worker/` proves
the daemon's loop against a scripted store. What neither proves is the seam
between them — that `WorkQueueStore` speaks the protocol correctly: hashes the
token it generates, hands the plaintext only to the ownership functions,
decodes the verdict row, and treats refusals as data. That seam is exactly
where a unit test lies (it would mock the very SQL whose shape is in
question), so this file runs the store as a real worker role against a real
server.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import json
import secrets

import pytest

from command_center.db import roles
from command_center.db.work_queue_store import (
    ClaimedWork,
    QueueRefusal,
    WorkQueueStore,
)

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

QUEUE = "execution"


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@pytest.fixture
def worker_store(admin_conn, psycopg, test_dsn, role_passwords):
    """A store connected as a per-host worker role — the production identity.

    The claimant is ``session_user`` by trigger, so a store tested over a
    superuser connection would prove nothing about the grants a real worker
    holds. The factory yields autocommit connections, matching the pool's
    contract.
    """
    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    name = f"aicc_wh_store_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    with admin_conn.cursor() as cur:
        for statement in roles.render_worker_host_role(name):
            cur.execute(statement)
        cur.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    dsn = _as_role(test_dsn, name, password)

    from contextlib import contextmanager

    @contextmanager
    def factory():
        with psycopg.connect(dsn, autocommit=True) as conn:
            yield conn

    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    try:
        yield WorkQueueStore(factory), psycopg, app_dsn
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


def _enqueue(psycopg, app_dsn, key: str, payload: dict) -> None:
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT queue_enqueue(%s, %s, %s::jsonb, NULL, %s, %s, %s, %s, %s)",
                (QUEUE, key, json.dumps(payload), None, 3, 0, 0, 0),
            )


def test_claim_execute_complete_round_trip(worker_store) -> None:
    store, psycopg, app_dsn = worker_store
    _enqueue(psycopg, app_dsn, "rt-1", {"kind": "echo", "n": 7})

    claimed = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork), claimed
    assert claimed.payload == {"kind": "echo", "n": 7}

    assert store.heartbeat(claimed) is True
    assert store.complete(claimed, {"echoed": 7}) is True
    # A completed attempt is no longer ours: the protocol refuses the stale
    # owner rather than double-writing the result.
    assert store.complete(claimed, {"echoed": "again"}) is False


def test_empty_queue_is_a_refusal_not_an_error(worker_store) -> None:
    store, _psycopg, _app_dsn = worker_store
    verdict = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(verdict, QueueRefusal)
    assert verdict.reason == "no_work"


def test_fail_retryable_makes_the_item_claimable_again(worker_store) -> None:
    store, psycopg, app_dsn = worker_store
    _enqueue(psycopg, app_dsn, "retry-1", {"kind": "flaky"})

    first = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(first, ClaimedWork)
    assert store.fail(first, reason="transient", retryable=True) is True

    # Backoff was requested as 0 at enqueue, so the item re-readies at once.
    second = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(second, ClaimedWork)
    assert second.work_item_id == first.work_item_id
    assert second.attempt_no == first.attempt_no + 1
    assert second.claim_token != first.claim_token, "a fresh attempt, a fresh secret"


def test_two_stores_cannot_hold_one_item(worker_store, admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Exclusivity through the store, not only through raw SQL: one item, two
    claims, exactly one holder — the second sees no_work, not a shared claim."""
    store, pg, app_dsn = worker_store
    _enqueue(pg, app_dsn, "excl-1", {"kind": "solo"})

    first = store.claim(QUEUE, visibility_seconds=60)
    second = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(first, ClaimedWork)
    assert isinstance(second, QueueRefusal) and second.reason == "no_work"
