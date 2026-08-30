"""The read store and the enqueue seam, proved against real PostgreSQL.

`tests/db/test_queue_claim.py` proves the SQL protocol; this file proves the
two Python seams APP-CONTROL-S1 adds on top of it, under the real grants:

* `WorkQueueStore.enqueue` as ``aicc_app`` — the role the HTTP layer runs as
  (the worker role is deliberately NOT granted enqueue);
* `WorkQueueReadStore` as ``aicc_app`` — list/detail over the public views
  plus the `work_result` read grant.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import secrets
import time

import pytest

from command_center.db import roles
from command_center.db.work_queue_read import WorkQueueReadStore
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
def stores(admin_conn, psycopg, test_dsn, role_passwords):
    """(app_write, app_read, worker_store) — the three production identities."""
    from contextlib import contextmanager

    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    name = f"aicc_wh_read_{secrets.token_hex(4)}"
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
            WorkQueueStore(factory_for(app_dsn)),
            WorkQueueReadStore(factory_for(app_dsn)),
            WorkQueueStore(factory_for(worker_dsn)),
        )
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


def test_enqueue_then_list_then_detail_round_trip(stores) -> None:
    app_write, app_read, worker = stores
    payload = {"kind": "agent_run", "v": 1, "project_id": "p1"}
    item_id = app_write.enqueue(QUEUE, idempotency_key="audit-rt-1", payload=payload)
    assert item_id.startswith("wki")

    listed = app_read.list_items(queue=QUEUE, state="ready")
    assert [row["work_item_id"] for row in listed] == [item_id]
    assert listed[0]["attempt_count"] == 0 and listed[0]["max_attempts"] == 3

    claimed = worker.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork) and claimed.payload == payload
    assert worker.complete(claimed, {"verdict": "ok"}) is True

    detail = app_read.get_item(item_id)
    assert detail is not None and detail["state"] == "succeeded"
    assert [a["attempt_no"] for a in detail["attempts"]] == [1]
    assert detail["attempts"][0]["state"] == "succeeded"
    assert detail["result"] == {"verdict": "ok"}
    # The redacted view is the read path: no claim_token_hash anywhere.
    assert "claim_token_hash" not in detail["attempts"][0]


def test_enqueue_is_idempotent_on_the_key(stores) -> None:
    app_write, app_read, _worker = stores
    first = app_write.enqueue(QUEUE, idempotency_key="dup-1", payload={"kind": "x"})
    second = app_write.enqueue(
        QUEUE, idempotency_key="dup-1", payload={"kind": "ignored"}
    )
    assert first == second, (
        "a duplicate key returns the existing item, not a second run"
    )
    assert len(app_read.list_items(queue=QUEUE)) == 1


def test_unknown_item_and_unknown_state_are_absences_not_errors(stores) -> None:
    _app_write, app_read, _worker = stores
    assert app_read.get_item("wki_does_not_exist") is None
    assert app_read.list_items(state="martian") == []


def test_worker_role_cannot_enqueue(stores, psycopg) -> None:
    """The grant boundary the HTTP layer relies on: enqueue is an app
    privilege, and a worker connection is refused by PostgreSQL itself."""
    _app_write, _app_read, worker = stores
    with pytest.raises(psycopg.Error):
        worker.enqueue(QUEUE, idempotency_key="worker-try", payload={})


def test_queue_metrics_counts_states_and_backlog_age(stores) -> None:
    app_write, app_read, worker = stores
    app_write.enqueue(QUEUE, idempotency_key="metrics-ready-1", payload={"kind": "x"})
    claimed = worker.claim(QUEUE, visibility_seconds=60)
    assert claimed is not None
    app_write.enqueue(QUEUE, idempotency_key="metrics-ready-2", payload={"kind": "x"})

    rows = app_read.queue_metrics(queue=QUEUE)
    assert len(rows) == 1
    row = rows[0]
    assert row["queue"] == QUEUE
    assert row["ready"] == 1
    assert row["claimed"] == 1
    assert row["succeeded"] == 0
    assert row["dead"] == 0
    assert row["stale_claims"] == 0
    assert row["oldest_ready_seconds"] is not None and row["oldest_ready_seconds"] >= 0


def test_queue_metrics_counts_a_lease_the_reaper_has_not_yet_swept(stores) -> None:
    """`stale_claims` is the telemetry surface for the exact incident this
    task retries after: a claim whose lease has already expired but which
    nothing has reaped yet."""
    app_write, app_read, worker = stores
    app_write.enqueue(QUEUE, idempotency_key="metrics-stale-1", payload={"kind": "x"})
    claimed = worker.claim(QUEUE, visibility_seconds=1)
    assert claimed is not None
    time.sleep(1.2)

    rows = app_read.queue_metrics(queue=QUEUE)
    assert rows[0]["claimed"] == 1
    assert rows[0]["stale_claims"] == 1


def test_queue_metrics_empty_or_unknown_queue_returns_no_rows(stores) -> None:
    """No item has ever named a queue: there is no registry of queue names to
    report a zeroed row against, so absence of activity and a typo'd queue
    name are the same observable fact — matching `list_items`'s own
    absence-is-empty-not-error contract."""
    _app_write, app_read, _worker = stores
    assert app_read.queue_metrics(queue=QUEUE) == []
    assert app_read.queue_metrics(queue="does-not-exist") == []


def test_queue_metrics_ignores_delayed_ready_items_in_backlog_age(stores) -> None:
    """A `ready` item can carry a future `available_at` (delayed enqueue, or
    post-failure backoff) — it is not yet claimable, so it must not be
    counted as backlog. Before this filter, a queue holding only delayed work
    reported a *negative* `oldest_ready_seconds` (`now() - <future min>`)."""
    app_write, app_read, _worker = stores
    app_write.enqueue(
        QUEUE, idempotency_key="metrics-delayed-1", payload={"kind": "x"},
        delay_seconds=3600,
    )

    rows = app_read.queue_metrics(queue=QUEUE)
    assert rows[0]["ready"] == 1
    assert rows[0]["oldest_ready_seconds"] is None
