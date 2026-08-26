"""The control-plane recovery surface, proved against real PostgreSQL.

`tests/db/test_queue_claim.py` proves the SQL recovery protocol exhaustively —
reap semantics under racing completions, redrive refusals, audit rows. What it
cannot prove is the Python seam production actually uses (SRV-06): that
``WorkQueueAdmin`` speaks that protocol correctly *as ``aicc_app``*, the role
the reaper timer and the operator CLI authenticate as. As with the store, a
unit test at this seam would mock the very SQL whose shape is in question, so
this file runs the admin against a real server under the real grants.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest

from command_center.db import roles
from command_center.db.work_queue_admin import WorkQueueAdmin
from command_center.db.work_queue_store import ClaimedWork, WorkQueueStore

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
def queue_actors(admin_conn, psycopg, test_dsn, role_passwords):
    """A worker store and an app-role admin — the two production identities.

    The admin connects as ``aicc_app`` because that is who the grants name:
    reap and redrive tested over a superuser connection would prove nothing
    about the privileges the reaper timer actually holds.
    """
    from contextlib import contextmanager

    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    name = f"aicc_wh_admin_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    with admin_conn.cursor() as cur:
        for statement in roles.render_worker_host_role(name):
            cur.execute(statement)
        cur.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    worker_dsn = _as_role(test_dsn, name, password)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    def factory_for(dsn: str):
        @contextmanager
        def factory():
            with psycopg.connect(dsn, autocommit=True) as conn:
                yield conn

        return factory

    try:
        yield (
            WorkQueueStore(factory_for(worker_dsn)),
            WorkQueueAdmin(factory_for(app_dsn)),
            psycopg,
            app_dsn,
        )
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


def _enqueue(
    psycopg, app_dsn, key: str, payload: dict, *, max_attempts: int = 3
) -> None:
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT queue_enqueue(%s, %s, %s::jsonb, NULL, %s, %s, %s, %s, %s)",
                (QUEUE, key, json.dumps(payload), None, max_attempts, 0, 0, 0),
            )


def test_reap_is_a_noop_on_a_quiet_queue(queue_actors) -> None:
    _store, admin, _psycopg, _app_dsn = queue_actors
    assert admin.reap() == 0


def test_reap_resumes_a_lost_workers_item(queue_actors) -> None:
    """Worker-loss recovery, end to end through the production seam: a claim
    whose holder vanishes (no heartbeat, no report) is reaped back to ready
    and claimed again — attempt numbering continuing, not restarting."""
    store, admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "lost-worker-1", {"kind": "echo"})

    first = store.claim(QUEUE, visibility_seconds=1)
    assert isinstance(first, ClaimedWork)

    deadline = time.monotonic() + 10
    reaped = 0
    while time.monotonic() < deadline:
        reaped = (
            admin.reap()
        )  # idempotent: early no-op ticks are the timer's normal life
        if reaped:
            break
        time.sleep(0.2)
    assert reaped == 1, "the lapsed lease was never reaped"

    second = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(second, ClaimedWork)
    assert second.work_item_id == first.work_item_id
    assert second.attempt_no == first.attempt_no + 1


def test_dead_letter_listing_and_redrive_round_trip(queue_actors) -> None:
    store, admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "perm-1", {"kind": "doomed"}, max_attempts=1)

    claimed = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork)
    assert store.fail(
        claimed, reason="payload is a list, not an object", retryable=False
    )

    letters = admin.dead_letters(QUEUE)
    assert [letter.work_item_id for letter in letters] == [claimed.work_item_id]
    letter = letters[0]
    assert letter.idempotency_key == "perm-1"
    assert letter.attempt_count == 1 and letter.max_attempts == 1
    assert letter.dead_reason.startswith("non_retryable")
    assert letter.last_attempt_reason == "payload is a list, not an object"

    assert admin.redrive(claimed.work_item_id, extra_attempts=1) is True
    assert admin.dead_letters(QUEUE) == []
    # And the item is genuinely live again, not merely renamed.
    retaken = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(retaken, ClaimedWork)
    assert retaken.work_item_id == claimed.work_item_id


def test_redrive_refusals_are_data_not_exceptions(queue_actors) -> None:
    store, admin, psycopg, app_dsn = queue_actors
    assert admin.redrive("wki_does_not_exist") is False
    _enqueue(psycopg, app_dsn, "alive-1", {"kind": "echo"})
    claimed = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork)
    assert admin.redrive(claimed.work_item_id) is False, (
        "a live item is not redriveable"
    )
