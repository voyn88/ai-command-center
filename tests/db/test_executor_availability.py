"""The Python store over the executor-availability protocol (0018,
VOYN-W0-AICC-EXECUTOR-QUOTA-VISIBILITY), proved against real PostgreSQL.

Mirrors `tests/db/test_work_queue_store.py`'s shape: a unit test at this seam
would mock the very SQL/grants whose correctness is in question (a worker
marking its own observation, an app-role read, an operator's override), so
this runs each identity against a real server instead.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager

import pytest

from command_center.db import roles
from command_center.db.executor_availability import ExecutorAvailabilityStore
from command_center.db.work_queue_read import WorkQueueReadStore

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


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


def _factory_for(psycopg, dsn: str):
    @contextmanager
    def factory():
        with psycopg.connect(dsn, autocommit=True) as conn:
            yield conn

    return factory


@pytest.fixture
def stores(admin_conn, psycopg, test_dsn, role_passwords):
    """(worker, app, operator, read) — the three writing/observing
    identities this protocol's grants distinguish (0018_executor_
    availability.up.sql's own docstring): the worker reports its own
    observation, the app role only ever reads, and only the operator may
    clear a mark it did not set. `read` is the SAME `aicc_app` identity,
    reached through `WorkQueueReadStore` — the metrics surface delivery
    dashboards already use (`queue_metrics`) — proving the availability
    read lands in that same PostgreSQL path, not a second one."""
    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    name = f"aicc_wh_exec_{secrets.token_hex(4)}"
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
    operator_dsn = _as_role(
        test_dsn, roles.OPERATOR_ROLE, role_passwords[roles.OPERATOR_ROLE]
    )

    try:
        yield (
            ExecutorAvailabilityStore(_factory_for(psycopg, worker_dsn)),
            ExecutorAvailabilityStore(_factory_for(psycopg, app_dsn)),
            ExecutorAvailabilityStore(_factory_for(psycopg, operator_dsn)),
            WorkQueueReadStore(_factory_for(psycopg, app_dsn)),
        )
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


def test_unmarked_executor_defaults_to_available(stores) -> None:
    _worker, app, _operator, _read = stores
    verdict = app.get("codex", use_cache=False)
    assert verdict.available
    assert verdict.reason is None and verdict.unavailable_until is None


def test_worker_mark_is_visible_fleet_wide_through_the_app_role(stores) -> None:
    worker, app, _operator, _read = stores
    worker.mark_unavailable("codex", "quota_limit", 3600)

    verdict = app.get("codex", use_cache=False)
    assert not verdict.available
    assert verdict.reason == "quota_limit"
    assert verdict.unavailable_until is not None


def test_a_second_mark_inside_the_window_slides_the_deadline(stores) -> None:
    """Idempotent by design (the migration's own docstring): a second
    failure inside the cooldown refreshes it rather than queuing a second
    expiry to reconcile."""
    worker, app, _operator, _read = stores
    worker.mark_unavailable("codex", "quota_limit", 60)
    first = app.get("codex", use_cache=False)
    worker.mark_unavailable("codex", "authentication_failed", 3600)
    second = app.get("codex", use_cache=False)

    assert second.reason == "authentication_failed"
    assert second.unavailable_until > first.unavailable_until


def test_mark_available_clears_the_mark(stores) -> None:
    worker, app, _operator, _read = stores
    worker.mark_unavailable("codex", "quota_limit", 3600)
    worker.mark_available("codex")

    verdict = app.get("codex", use_cache=False)
    assert verdict.available
    assert verdict.reason is None and verdict.unavailable_until is None


def test_operator_may_clear_a_mark_it_did_not_set(stores) -> None:
    """The administrative override this table exists to allow: an incident
    response can restore an executor early without impersonating the
    worker that observed the outage."""
    worker, app, operator, _read = stores
    worker.mark_unavailable("copilot_cli", "quota_limit", 3600)
    operator.mark_available("copilot_cli")

    verdict = app.get("copilot_cli", use_cache=False)
    assert verdict.available


def test_app_role_cannot_mark_unavailable(stores, psycopg) -> None:
    """Mutation is worker-only (queue-claim idiom): the control plane reads
    through the plain grant and never claims to know an executor's live
    state first-hand."""
    _worker, app, _operator, _read = stores
    with pytest.raises(psycopg.Error):
        app.mark_unavailable("codex", "quota_limit", 60)


def test_app_role_cannot_mark_available(stores, psycopg) -> None:
    """Only a worker (its own successful run) or an operator (an incident
    override) may clear a mark — never the control plane."""
    _worker, app, _operator, _read = stores
    with pytest.raises(psycopg.Error):
        app.mark_available("codex")


def test_list_all_reports_every_marked_executor(stores) -> None:
    worker, app, _operator, _read = stores
    worker.mark_unavailable("codex", "quota_limit", 3600)
    worker.mark_unavailable("copilot_cli", "authentication_failed", 900)

    verdicts = {v.executor_id: v for v in app.list_all()}
    assert set(verdicts) == {"codex", "copilot_cli"}
    assert verdicts["codex"].reason == "quota_limit"
    assert verdicts["copilot_cli"].reason == "authentication_failed"


def test_expired_cooldown_reports_unavailable_until_expiry_confirmed_live(
    stores,
) -> None:
    """The TTL is a cooldown a caller chooses, not a self-clearing timer —
    `executor_availability`'s row stays `unavailable` past `unavailable_until`
    until something calls `executor_mark_available` (an operator, or a later
    successful run). This is the honest half of the migration's own
    docstring: nothing on this side of the CLI knows when to clear itself."""
    worker, app, _operator, _read = stores
    worker.mark_unavailable("codex", "quota_limit", 1)
    time.sleep(1.2)

    verdict = app.get("codex", use_cache=False)
    assert verdict.status == "unavailable"  # still true until explicitly cleared
    assert verdict.reason == "quota_limit"
