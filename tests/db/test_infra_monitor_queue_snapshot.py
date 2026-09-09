"""The queue monitor's measurement, proved against a real PostgreSQL server.

VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #481): `control-01:
queue` reported `queue_stalled` while the fleet was healthy. The stall clock
was `now() - min(work_item.updated_at)` over every ready or claimed row, and
for a claimed row that timestamp is the moment it was CLAIMED -- the heartbeat
renews `work_attempt.visible_until` and never touches the item. Every attempt
that outran `--max-stalled-seconds` (900s) therefore read as a stall, which
the deployment expects to be the ordinary case: `voyn-aicc-worker@.service`
gives one attempt `TimeoutStopSec=3660s`, and the handler provisions a
worktree (a 600s clone timeout) before the agent starts.

Excluding those claims was half the fix. The queue also holds work no lane can
attend yet -- `PlanLimits.wip_limit` is 4 against the 2 lanes of
`deploy/aicc/worker-lanes` -- so the surplus item sits `ready` and due for a
whole attempt and tripped the same clock on its own. The statement therefore
reports three disjoint classes (due-ready, lapsed claim, attended claim) and
leaves the starvation verdict to `evaluate`, which is where the fleet's claim
capacity is known.

Why this file needs a real server rather than a stub cursor:

* The distinction the fix rests on -- attended vs unattended -- is a JOIN from
  `work_item.current_attempt_id` to a lease in `work_attempt_public`. A fake
  cursor would return whatever the test author decided the join means, which
  is the one thing under test.
* The claims below are taken through `queue_claim` and expired the way the
  protocol expires them, so the states the query classifies are states the
  database actually produces -- not hand-INSERTed rows that no code path can
  create.
* It runs as `aicc_app`, the identity `voyn-queue-monitor.service` connects
  with. `work_attempt` itself is granted to NOBODY (it holds
  `claim_token_hash`); the monitor reaches the lease through
  `work_attempt_public`, and only running as the role proves that grant
  exists. As a superuser this file would pass over a query production cannot
  execute.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set -- see ``conftest``.
"""

from __future__ import annotations

import pytest

from command_center.db import roles
from command_center.db.work_queue_store import ClaimedWork, WorkQueueStore
from command_center.ops import infra_monitor

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

QUEUE = "execution"

#: The deployed queue probe's thresholds (`voyn-queue-monitor.service`).
MAX_STALLED = 900.0


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


@pytest.fixture
def monitor(admin_conn, psycopg, test_dsn, role_passwords):
    """``(measure, app_store, worker_store, age)`` over a migrated database.

    Three identities, because the production ones are three and the split is
    the point: ``aicc_app`` enqueues and is what the queue probe connects as;
    a per-host ``aicc_w_*`` role claims and heartbeats (``queue_claim`` is
    deliberately NOT an app privilege); the superuser only moves clocks.

    ``measure`` runs the production statement and maps it the production way
    (``snapshot_from_row``), so nothing here can drift from what the deployed
    probe reads.
    """
    import secrets
    from contextlib import contextmanager

    from psycopg import sql

    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)

    host_role = f"aicc_wh_mon_{secrets.token_hex(4)}"
    host_password = secrets.token_urlsafe(24)
    with admin_conn.cursor() as cur:
        for statement in roles.render_worker_host_role(host_role):
            cur.execute(statement)
        cur.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(host_role), sql.Literal(host_password)
            )
        )

    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    worker_dsn = _as_role(test_dsn, host_role, host_password)

    def factory_for(dsn: str):
        @contextmanager
        def factory():
            with psycopg.connect(dsn, autocommit=True) as conn:
                yield conn

        return factory

    def measure() -> infra_monitor.QueueSnapshot:
        with psycopg.connect(app_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(infra_monitor._QUEUE_SNAPSHOT_SQL)
            return infra_monitor.snapshot_from_row(cur.fetchone())

    def age(statement: str, params: tuple = ()) -> None:
        """Move timestamps into the past as the owner. The shapes under test
        take tens of minutes of wall time to arise naturally, and the monitor
        reads clocks -- not durations a fixture told it about."""
        with admin_conn.cursor() as cur:  # autocommit, per conftest
            cur.execute(statement, params)

    try:
        yield (
            measure,
            WorkQueueStore(factory_for(app_dsn)),
            WorkQueueStore(factory_for(worker_dsn)),
            age,
        )
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(host_role))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


def _claim(store: WorkQueueStore, visibility: int = 300) -> ClaimedWork:
    claimed = store.claim(QUEUE, visibility_seconds=visibility)
    assert isinstance(claimed, ClaimedWork), claimed
    return claimed


def test_a_live_lease_is_attended_and_the_monitor_stays_green(monitor) -> None:
    """THE REGRESSION. One lane 40 minutes into an attempt whose lease is
    being renewed: nothing is unattended, so the stall clock has nothing to
    measure and `queue_stalled` cannot fire."""
    measure, app, worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="long-run", payload={"kind": "agent_run"})
    claimed = _claim(worker)

    age(
        "UPDATE work_item SET updated_at = now() - interval '40 minutes' "
        "WHERE current_attempt_id = %s",
        (claimed.attempt_id,),
    )
    # The lease is live because the worker is alive and beating -- which is
    # exactly what `queue_heartbeat` writes.
    assert worker.heartbeat(claimed) is True

    snapshot = measure()
    assert (snapshot.ready, snapshot.claimed) == (0, 1)
    assert (snapshot.ready_due, snapshot.lapsed_claims) == (0, 0)
    assert snapshot.attended_claims == 1
    assert snapshot.live_claim_age_seconds is not None
    # The claim IS old -- 2400s, well past the 900s stall window. Before the
    # fix that number was the stall clock and turned the probe red.
    assert snapshot.live_claim_age_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert report.ok, report.failures


def test_a_claim_whose_lease_lapsed_is_unattended_and_red(monitor) -> None:
    """The zombie the check exists for. `aicc-queue-reaper.timer` runs every
    minute, so a lease lapsed for longer than the stall window means recovery
    itself is broken -- and that is still a stall."""
    measure, app, worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="zombie", payload={"kind": "agent_run"})
    claimed = _claim(worker)

    age(
        "UPDATE work_attempt SET visible_until = now() - interval '20 minutes' "
        "WHERE attempt_id = %s",
        (claimed.attempt_id,),
    )

    snapshot = measure()
    assert snapshot.claimed == 1
    assert snapshot.lapsed_claims == 1
    assert snapshot.attended_claims == 0
    assert snapshot.live_claim_age_seconds is None
    # The wait is timed from the LAPSE, not from the claim: how long the item
    # has been nobody's.
    assert snapshot.lapsed_claim_age_seconds is not None
    assert 1100 < snapshot.lapsed_claim_age_seconds < 1300

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert "queue_stalled" in report.failures


def test_a_ready_item_nobody_claims_is_still_a_stall(monitor) -> None:
    measure, app, _worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="ignored", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '30 minutes', "
        "available_at = now() - interval '30 minutes'"
    )

    snapshot = measure()
    # Nothing is claimed at all, so every lane was free to take it.
    assert (snapshot.ready_due, snapshot.attended_claims) == (1, 0)
    assert snapshot.ready_due_age_seconds is not None
    assert snapshot.ready_due_age_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert "queue_stalled" in report.failures


def test_a_ready_item_inside_its_backoff_is_waiting_by_design(monitor) -> None:
    """`queue_fail` and `queue_reap` both push `available_at` forward, and
    `queue_enqueue` takes a delay. An item no claimer is ALLOWED to take yet
    is not one a claimer failed to take: counting it made the queue's own
    backoff read as a stall."""
    measure, app, _worker, age = monitor
    app.enqueue(
        QUEUE,
        idempotency_key="delayed",
        payload={"kind": "agent_run"},
        delay_seconds=600,
    )
    age("UPDATE work_item SET updated_at = now() - interval '2 hours'")

    snapshot = measure()
    assert snapshot.ready == 1
    assert snapshot.ready_due == 0
    assert snapshot.ready_due_age_seconds is None

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert report.ok, report.failures


def test_one_lapsed_claim_is_not_hidden_by_a_healthy_neighbour(monitor) -> None:
    """The measurement is a MINIMUM over the lapsed set, so a busy lane
    beside a zombie cannot average it away -- the property
    `test_unrelated_success_does_not_hide_a_zombie_claim` pins at the
    `evaluate` level, here proved through the SQL."""
    measure, app, worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="healthy", payload={"kind": "agent_run"})
    healthy = _claim(worker)
    app.enqueue(QUEUE, idempotency_key="lapsed", payload={"kind": "agent_run"})
    lapsed = _claim(worker)

    age(
        "UPDATE work_attempt SET visible_until = now() - interval '20 minutes' "
        "WHERE attempt_id = %s",
        (lapsed.attempt_id,),
    )
    assert worker.heartbeat(healthy) is True

    snapshot = measure()
    assert snapshot.claimed == 2
    assert (snapshot.lapsed_claims, snapshot.attended_claims) == (1, 1)
    assert snapshot.lapsed_claim_age_seconds is not None
    assert snapshot.lapsed_claim_age_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert "queue_stalled" in report.failures


def test_due_work_behind_a_full_fleet_is_queued_not_stalled(monitor) -> None:
    """THE SECOND REGRESSION, proved against the server that produces the
    shape. `PlanLimits.wip_limit` is 4 against 2 lanes, so the queue holds a
    dispatched item no lane can attend yet: two claims under live leases and a
    third item ready, due and an hour old behind them.

    The database reports the three classes; only `evaluate` knows the lane
    count, so the same snapshot is green at the fleet's capacity and red at a
    capacity that says a third lane was sitting idle."""
    measure, app, worker, age = monitor
    for key in ("lane-one", "lane-two"):
        app.enqueue(QUEUE, idempotency_key=key, payload={"kind": "agent_run"})
        assert worker.heartbeat(_claim(worker)) is True
    app.enqueue(QUEUE, idempotency_key="queued", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '1 hour', "
        "available_at = now() - interval '1 hour' WHERE state = 'ready'"
    )

    snapshot = measure()
    assert (snapshot.attended_claims, snapshot.lapsed_claims) == (2, 0)
    assert snapshot.ready_due == 1
    # The queued item IS old enough to trip the stall clock on its own.
    assert snapshot.ready_due_age_seconds > MAX_STALLED

    at_capacity = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert at_capacity.ok, at_capacity.failures

    with_a_free_lane = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=3,
    )
    assert "queue_stalled" in with_a_free_lane.failures


def test_the_monitor_reads_the_lease_through_the_redacted_view(monitor) -> None:
    """`work_attempt` is granted to nobody -- it holds `claim_token_hash`. The
    snapshot's join must go through `work_attempt_public`, and running as
    `aicc_app` is what proves it: the statement above already ran as that role,
    so this asserts the boundary it depended on rather than restating it."""
    _measure, app, worker, _age = monitor
    app.enqueue(QUEUE, idempotency_key="grant", payload={"kind": "agent_run"})
    _claim(worker)

    assert "work_attempt_public" in infra_monitor._QUEUE_SNAPSHOT_SQL
    assert roles.PRIVILEGES[roles.APP_ROLE]["work_attempt"] == frozenset()
    assert roles.VIEW_PRIVILEGES[roles.APP_ROLE]["work_attempt_public"] == frozenset(
        {"SELECT"}
    )
