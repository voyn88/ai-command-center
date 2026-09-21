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

    def measure(queue: str = QUEUE) -> infra_monitor.QueueSnapshot:
        with psycopg.connect(app_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(infra_monitor._QUEUE_SNAPSHOT_SQL, (queue, queue))
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
    # Both lanes took their work an hour ago, which is what makes the third
    # lane's idleness an hour long in the `claim_capacity=3` reading below.
    # Without this the claims are seconds old and the fleet clock -- rightly
    # -- says nothing has been ignored for any length of time at all.
    age("UPDATE work_attempt SET created_at = now() - interval '1 hour'")

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


def test_the_boundary_between_two_attempts_is_not_an_hour_old_stall(monitor) -> None:
    """THE THIRD REGRESSION (monitor_finding #2471), against the server that
    produces the shape.

    The fleet is full and an item has been due for an hour behind it --
    legitimate backpressure, green while both leases are live. Then one lane
    commits its result. For the moment before its next claim commits, the
    probe sees a free lane and an hour-old due item, and capacity no longer
    excuses it: that is the boundary every attempt ends at, sampled by a
    2-minute timer.

    The fleet clock is what closes it. `queue_complete` moved an attempt out
    of 'active' a moment ago, so the fleet handed work back a moment ago, and
    nothing here has been ignored for an hour."""
    measure, app, worker, age = monitor
    claims = []
    for key in ("lane-one", "lane-two"):
        app.enqueue(QUEUE, idempotency_key=key, payload={"kind": "agent_run"})
        claims.append(_claim(worker))
    app.enqueue(QUEUE, idempotency_key="queued", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '1 hour', "
        "available_at = now() - interval '1 hour' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '1 hour'")

    full_fleet = measure()
    assert (full_fleet.attended_claims, full_fleet.ready_due) == (2, 1)
    assert full_fleet.ready_due_age_seconds > MAX_STALLED
    assert infra_monitor.evaluate(
        {}, full_fleet, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    ).ok

    # The lane finishes. Its loop claims again immediately, but the result
    # commits first -- and this is that instant.
    assert worker.complete(claims[0], {"ok": True}) is True

    boundary = measure()
    assert boundary.attended_claims == 1
    # The item has still been due for an hour: the clock the old check read
    # is unchanged, and on its own it is still far past the window.
    assert boundary.ready_due_age_seconds > MAX_STALLED
    # What changed is that the fleet moved, and it moved just now.
    assert boundary.fleet_idle_seconds is not None
    assert boundary.fleet_idle_seconds < 60

    report = infra_monitor.evaluate(
        {}, boundary, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert report.ok, report.failures


def test_a_heartbeat_is_not_the_fleet_taking_work(monitor) -> None:
    """The fleet clock must not be renewable by the lane it is measuring.
    `queue_heartbeat` writes `updated_at = now()` on an attempt that stays
    'active', so a statement reading `updated_at` unconditionally would let
    one lane renewing one lease report a fleet claiming continuously -- and a
    clock a stalled fleet can wind forward excuses every stall there is.

    Here one lane has held its claim for an hour and beats right now, while a
    second item has been due for an hour with a lane free. The fleet has taken
    nothing and handed nothing back in that hour, and the probe says so."""
    measure, app, worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="long-run", payload={"kind": "agent_run"})
    claimed = _claim(worker)
    app.enqueue(QUEUE, idempotency_key="ignored", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '1 hour', "
        "available_at = now() - interval '1 hour' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '1 hour'")
    assert worker.heartbeat(claimed) is True

    snapshot = measure()
    assert (snapshot.attended_claims, snapshot.ready_due) == (1, 1)
    assert snapshot.fleet_idle_seconds is not None
    # An hour, not the fraction of a second since the heartbeat.
    assert snapshot.fleet_idle_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert "queue_stalled" in report.failures


def test_a_queue_no_lane_has_ever_claimed_from_has_no_fleet_clock(monitor) -> None:
    """`max()` over an empty `work_attempt` is NULL, and that is the honest
    answer for a fleet that never started: no lane has ever demonstrated it
    could take anything. `evaluate` then bounds nothing, so the ready item is
    timed from its own due age and
    `test_a_ready_item_nobody_claims_is_still_a_stall` keeps its verdict."""
    measure, app, _worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="never-claimed", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '30 minutes', "
        "available_at = now() - interval '30 minutes'"
    )

    snapshot = measure()
    assert snapshot.fleet_idle_seconds is None

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True,
    )
    assert "queue_stalled" in report.failures


# ---------------------------------------------------------------------------
# THE FOURTH REGRESSION (monitor_finding #2766): one fleet, one queue.
#
# `queue_claim(p_queue, ...)` serves exactly the queue it is given, and the
# lanes give it exactly one (`WorkerConfig.queue`). The statement measured
# every queue in the table and was judged against that single fleet's
# `--claim-capacity`, which is wrong in both directions -- and the two below
# are those directions, taken through the protocol rather than hand-INSERTed.
# ---------------------------------------------------------------------------


def test_a_lease_wait_refund_leaves_the_queue_claimable(monitor) -> None:
    """THE ROOT CAUSE THIS FINDING WAS ACTUALLY REPORTING (monitor_finding
    #2840), measured end to end: the probe's verdict against the fleet's
    ability to act on it.

    Every earlier fix here made the MEASUREMENT honest. This one is the thing
    it was honestly measuring. `queue_fail_lease_wait` refunds `attempt_count`
    while the refunded `work_attempt` row keeps its `attempt_no`, and
    `queue_claim` derived the next number from the refunded budget -- so the
    item's next claim raised `UniqueViolation` instead of claiming. Since
    `queue_claim` takes the oldest due row, that item was head-of-line: no lane
    could claim ANYTHING, the fleet clock froze because no attempt was ever
    created, and the probe reported due ready work nobody was attending.

    That is a true `queue_stalled` with no exit reachable by the fleet, which
    is exactly the finding that kept reopening. Here the same shape drains:
    three claims, three completions, and a green probe."""
    measure, app, worker, age = monitor
    # Oldest first, so the lease-wait item is the one `queue_claim` selects.
    app.enqueue(QUEUE, idempotency_key="contended", payload={"kind": "agent_run"})
    contended = _claim(worker)
    assert worker.fail_lease_wait(contended, reason="lease_unavailable") is True
    for n in range(2):
        app.enqueue(QUEUE, idempotency_key=f"behind-{n}", payload={"kind": "agent_run"})
    # The lease-wait backoff elapses, and the whole queue has been due for an
    # hour -- long past the stall window, so nothing here is excused by youth.
    age(
        "UPDATE work_item SET available_at = now() - interval '1 hour', "
        "updated_at = now() - interval '1 hour' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '1 hour', "
        "updated_at = now() - interval '1 hour'")

    stalled = measure()
    assert (stalled.ready_due, stalled.attended_claims) == (3, 0)
    assert stalled.ready_due_age_seconds > MAX_STALLED
    # The fleet clock is frozen at the refunded attempt: nothing has been
    # claimed since, because nothing COULD be.
    assert stalled.fleet_idle_seconds > MAX_STALLED
    assert "queue_stalled" in infra_monitor.evaluate(
        {}, stalled, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    ).failures

    # THE FLEET CAN NOW ACT. Before the fix the first of these raised
    # UniqueViolation out of `queue_claim` -- and out of `run_forever`, which
    # handles refusals and not exceptions.
    for _ in range(3):
        claimed = _claim(worker)
        assert worker.complete(claimed, {"ok": True}) is True

    drained = measure()
    assert (drained.ready, drained.claimed, drained.succeeded) == (0, 0, 3)
    report = infra_monitor.evaluate(
        {}, drained, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert report.ok, report.failures


def test_another_queues_ready_work_is_not_this_fleets_stall(monitor) -> None:
    """An item on a queue no lane claims from must not redden this probe.

    This is the shape with no way out: `queue_claim('execution', ...)` will
    never look at a `staging` row, so no amount of healthy fleet behaviour can
    retire the finding it used to open. A `queue_stalled` that the fleet
    cannot clear by working is a task the planner mints forever.
    """
    measure, app, _worker, age = monitor
    app.enqueue("staging", idempotency_key="foreign-1", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET created_at = now() - interval '4 hours',"
        " updated_at = now() - interval '4 hours',"
        " available_at = now() - interval '4 hours'"
    )

    snapshot = measure()
    assert snapshot.ready_due == 0
    assert snapshot.ready_due_age_seconds is None

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert report.failures == ()

    # And it is measured -- by the probe that names it.
    staging = measure("staging")
    assert staging.ready_due == 1
    assert staging.ready_due_age_seconds > MAX_STALLED


def test_another_queues_claims_never_excuse_this_queues_stall(monitor) -> None:
    """The fail-OPEN half, which is the serious one.

    Capacity is the question "was a lane free to take this?", and a lane busy
    on ANOTHER queue was never a lane that could have taken this. Two attended
    claims on `staging` used to fill `execution`'s capacity of 2, making
    `spare_capacity` false and excusing a four-hour-old unclaimed item as
    backpressure behind a fleet that was not serving it at all. A fail-closed
    monitor silent through a real stall is the one outcome it exists to
    prevent.
    """
    measure, app, worker, age = monitor
    for i in range(2):
        app.enqueue("staging", idempotency_key=f"foreign-{i}", payload={"kind": "run"})
    for _ in range(2):
        claimed = worker.claim("staging", visibility_seconds=300)
        assert isinstance(claimed, ClaimedWork), claimed

    # The real work: due for four hours on the queue the lanes do serve, with
    # no claim against it and no lane that has ever moved on its behalf.
    app.enqueue(QUEUE, idempotency_key="starved", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET created_at = now() - interval '4 hours',"
        " updated_at = now() - interval '4 hours',"
        " available_at = now() - interval '4 hours'"
        " WHERE queue = %s",
        (QUEUE,),
    )

    snapshot = measure()
    assert snapshot.attended_claims == 0, "another queue's lanes are not this fleet's"
    assert snapshot.ready_due == 1
    assert snapshot.fleet_idle_seconds is None, (
        "no lane has ever taken or returned an item of THIS queue"
    )

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert "queue_stalled" in report.failures


def test_another_queues_attempts_do_not_wind_this_queues_fleet_clock(monitor) -> None:
    """The bound added for #2471 is scoped too.

    `fleet_idle_seconds` answers "have the lanes serving this queue moved?".
    An attempt taken this second on `staging` is another fleet's lane doing
    another fleet's work; if it reset this clock, `evaluate` would take
    `min(due_age, ~0)` and excuse an `execution` stall of any age on the
    strength of progress somewhere else -- reintroducing the fail-open above
    through the fix for the one before it.
    """
    measure, app, worker, age = monitor
    # This queue HAS a fleet history, so the clock is a number rather than
    # NULL: one item claimed and completed, then aged well past the window.
    app.enqueue(QUEUE, idempotency_key="done", payload={"kind": "agent_run"})
    finished = _claim(worker)
    assert worker.complete(finished, {"ok": True})
    app.enqueue(QUEUE, idempotency_key="waiting", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET created_at = created_at - interval '4 hours',"
        " updated_at = updated_at - interval '4 hours',"
        " available_at = available_at - interval '4 hours'"
    )
    age(
        "UPDATE work_attempt SET created_at = created_at - interval '4 hours',"
        " updated_at = updated_at - interval '4 hours',"
        " visible_until = visible_until - interval '4 hours',"
        " heartbeat_at = heartbeat_at - interval '4 hours'"
    )

    # Now another queue's lane takes an item, right now.
    app.enqueue("staging", idempotency_key="foreign-now", payload={"kind": "run"})
    claimed = worker.claim("staging", visibility_seconds=300)
    assert isinstance(claimed, ClaimedWork), claimed

    snapshot = measure()
    assert snapshot.fleet_idle_seconds > MAX_STALLED, snapshot.fleet_idle_seconds

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert "queue_stalled" in report.failures


def test_a_lane_whose_lease_slipped_is_still_holding_its_lane(monitor) -> None:
    """THE REGRESSION (monitor_finding #13366), against the server that
    produces the shape.

    Capacity asks "was a lane free to take this?". It used to be answered
    with `attended_claims` -- how many lanes hold a LIVE lease -- and that is
    a different question. A lane whose lease slipped is still HOLDING its
    item: the row stays `claimed` until the reaper takes it back, and
    `queue_claim` will not hand that lane a second one meanwhile.

    So here: two lanes 40 minutes into legitimate attempts
    (`voyn-aicc-worker@.service` allows one 3660s), the surplus dispatched
    item ready and due behind them the whole time -- `PlanLimits.wip_limit` 4
    against 2 lanes, the shape the queue is DESIGNED to hold. Then lane two's
    lease lapses by 30 seconds -- an outage past what the beat cadence
    absorbs, which 0029 names the occasion for: a database blip, or the
    `voyn-aicc-pgtunnel.service` restart the credential rotation cycles.
    (Two failed beats used to be enough, because the beat ran at
    `visibility_seconds / 3` and the third landed on the deadline; see
    `test_a_tunnel_restart_of_the_tolerated_length_keeps_the_lane_attended`
    at the foot of this file. Rarer is not never, and this rule is what the
    measurement needs either way.)

    Nothing about the fleet changed. No lane is free, no lane stopped, and
    `aicc-queue-reaper.timer` will clear the lapse on its next minute. The
    probe samples every two, so the old reading was not a race that might
    happen: it was `queue_stalled` against a fleet at full stretch.

    AND THE FLEET CLOCK CANNOT CATCH THIS ONE, which is why it outlived the
    bound added for exactly this family (#2471, the test above). `fleet_idle`
    is an hour here PRECISELY BECAUSE both lanes have been busy that hour --
    no attempt changed state, which is what two healthy long runs look like.
    That bound excuses the instant a lane frees; it has nothing to say about
    a fleet that never freed one."""
    measure, app, worker, age = monitor
    claims = []
    for key in ("lane-one", "lane-two"):
        app.enqueue(QUEUE, idempotency_key=key, payload={"kind": "agent_run"})
        claims.append(_claim(worker))
    app.enqueue(QUEUE, idempotency_key="queued", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '40 minutes', "
        "available_at = now() - interval '40 minutes' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '40 minutes'")

    full_fleet = measure()
    assert (full_fleet.claimed, full_fleet.attended_claims) == (2, 2)
    assert infra_monitor.evaluate(
        {}, full_fleet, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    ).ok

    # Two beats missed. The lane is alive and still running its handler; only
    # the lease is late, and only by 30 seconds.
    age(
        "UPDATE work_attempt SET visible_until = now() - interval '30 seconds' "
        "WHERE attempt_id = %s",
        (claims[1].attempt_id,),
    )

    slipped = measure()
    # The fleet still holds both items -- which is the whole point.
    assert slipped.claimed == 2
    assert (slipped.attended_claims, slipped.lapsed_claims) == (1, 1)
    # The lapse itself is nowhere near the window; it is not what the old
    # verdict was made of.
    assert slipped.lapsed_claim_age_seconds < 120
    # The due item's own clock is unchanged and far past the window, and the
    # fleet clock is too -- because both lanes have been busy the whole time.
    assert slipped.ready_due == 1
    assert slipped.ready_due_age_seconds > MAX_STALLED
    assert slipped.fleet_idle_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, slipped, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert report.ok, report.failures


def test_a_lapse_the_reaper_never_clears_is_still_a_stall(monitor) -> None:
    """The fail-closed half, and the reason the fix above gives nothing away.

    A lapsed claim can also mean the LANE IS GONE, in which case a lane
    really is free. Weighing capacity by occupancy excuses the due work for
    as long as that claim sits unreaped -- and nothing is lost, because
    `lapsed_claim_age_seconds` is weighed UNCONDITIONALLY: not gated by
    capacity, not bounded by the fleet clock, and fired at this same window.

    Same full-fleet shape as above, so the capacity test is excusing the due
    item exactly as it did there. The only change is that the lapse is now
    older than the stall window, which means `aicc-queue-reaper.timer` -- a
    minute's cadence -- has missed fifteen ticks."""
    measure, app, worker, age = monitor
    claims = []
    for key in ("lane-one", "lane-two"):
        app.enqueue(QUEUE, idempotency_key=key, payload={"kind": "agent_run"})
        claims.append(_claim(worker))
    app.enqueue(QUEUE, idempotency_key="queued", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '40 minutes', "
        "available_at = now() - interval '40 minutes' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '40 minutes'")
    age(
        "UPDATE work_attempt SET visible_until = now() - interval '20 minutes' "
        "WHERE attempt_id = %s",
        (claims[1].attempt_id,),
    )

    snapshot = measure()
    # The fleet is still holding two items, so capacity excuses the due one.
    assert snapshot.claimed == 2
    assert snapshot.lapsed_claim_age_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert "queue_stalled" in report.failures


def test_the_reaper_hands_the_lane_back_and_the_due_clock_resumes(monitor) -> None:
    """The other exit, and the one that bounds how long the fix can excuse
    anything: the reaper moves the item OUT of `claimed`, so the occupancy
    count drops on its own and the due-ready clock is live again against a
    fleet that has been standing still.

    This is what makes "a lapsed claim occupies a lane" safe to assume. It
    cannot outlast `aicc-queue-reaper.timer`, and a reaper that stops is
    precisely what the unconditional clock above measures."""
    measure, app, worker, age = monitor
    from command_center.db.work_queue_admin import WorkQueueAdmin

    claims = []
    for key in ("lane-one", "lane-two"):
        app.enqueue(QUEUE, idempotency_key=key, payload={"kind": "agent_run"})
        claims.append(_claim(worker))
    app.enqueue(QUEUE, idempotency_key="queued", payload={"kind": "agent_run"})
    age(
        "UPDATE work_item SET updated_at = now() - interval '40 minutes', "
        "available_at = now() - interval '40 minutes' WHERE state = 'ready'"
    )
    age("UPDATE work_attempt SET created_at = now() - interval '40 minutes'")
    # The whole fleet is gone: both leases lapsed, inside the stall window so
    # the unconditional clock is not what decides this one.
    age("UPDATE work_attempt SET visible_until = now() - interval '30 seconds'")

    held = measure()
    assert held.claimed == 2
    assert infra_monitor.evaluate(
        {}, held, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    ).ok

    # The reaper's next tick. It runs as `aicc_app`, which is the identity
    # `aicc-queue-reaper.service` carries.
    assert WorkQueueAdmin(app._factory).reap() == 2

    # The requeued items are not due yet (`_queue_backoff`), but the one that
    # was waiting behind the fleet still is -- and now nothing is holding a
    # lane, so nothing excuses it.
    age("UPDATE work_attempt SET updated_at = now() - interval '40 minutes'")
    recovered = measure()
    assert recovered.claimed == 0
    assert recovered.ready_due >= 1
    assert recovered.fleet_idle_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, recovered, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert "queue_stalled" in report.failures


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #13420): the fleet
# was MANUFACTURING the lapsed-claim class the probe weighs unconditionally.
#
# Everything above this line taught the MEASUREMENT to read a fleet whose
# leases slip -- #13366 stopped a slipped lease freeing the lane that still
# holds the item, 0029 stopped the reaper expiring a lease that had been
# renewed. Both rest on the same premise, stated in both of their headers:
# "the beat runs at `visibility_seconds / 3` ... so ANY TWO CONSECUTIVE FAILED
# BEATS lapse the lease", treated as an unavoidable fact of the fleet.
#
# It was not a fact about the fleet. It was an off-by-one beat in
# `WorkerDaemon._heartbeat_loop`, under a comment promising the opposite
# ("two consecutive beats may fail ... before the lease actually lapses").
# Three beats at a third of the window fall at V/3, 2V/3 and V EXACTLY, so
# after two failures the beat that has to succeed arrives ON the deadline and
# `_queue_owns` refuses it (`claim_expired` is `visible_until <= now()`).
#
# These run the REAL beat loop against the REAL protocol, because the claim
# under test is about a clock: the lease is kept by the server, renewed by
# `queue_heartbeat` from the attempt's own row, and judged by `now()`. A fake
# store would be asserting that the test's own idea of a deadline agrees with
# itself. They wait real seconds for the same reason -- the cadence IS the
# subject -- so the window is the smallest one the clamps leave meaningful.


def _blipping(store: WorkQueueStore, failures: int) -> list[str]:
    """Make the next ``failures`` beats raise, the way a restarting
    `voyn-aicc-pgtunnel.service` does to a lane, and record every beat."""
    beats: list[str] = []
    real = store.heartbeat

    def heartbeat(work: ClaimedWork) -> bool:
        if len(beats) < failures:
            beats.append("raised")
            raise ConnectionError("tunnel restarting")
        alive = real(work)
        beats.append("renewed" if alive else "refused")
        return alive

    store.heartbeat = heartbeat  # type: ignore[method-assign]
    return beats


def _beat_through(store: WorkQueueStore, work: ClaimedWork, window: float):
    """Run the production beat loop over one claim for a whole window plus a
    beat, and report whether it gave the lease up."""
    import threading

    from command_center.worker.daemon import WorkerConfig, WorkerDaemon, beat_interval_seconds

    daemon = WorkerDaemon(
        store, {}, WorkerConfig(queue=QUEUE, visibility_seconds=int(window))
    )
    lease_lost = threading.Event()
    beat_stop = threading.Event()
    thread = threading.Thread(
        target=daemon._heartbeat_loop,
        args=(work, lease_lost, beat_stop),
        daemon=True,
    )
    thread.start()
    lease_lost.wait(window + beat_interval_seconds(window) + 1.0)
    beat_stop.set()
    thread.join(timeout=5)
    return lease_lost.is_set()


#: Small enough that a test waits seconds rather than minutes, large enough
#: that `beat_interval_seconds`' one-second floor does not swallow the
#: cadence under test. The RATIO is what is being measured, and it is the
#: deployed one.
BLIP_WINDOW = 8


def test_a_tunnel_restart_of_the_tolerated_length_keeps_the_lane_attended(
    monitor,
) -> None:
    """THE REGRESSION. A lane 40 minutes into a legitimate attempt loses the
    two consecutive beats a `voyn-aicc-pgtunnel.service` restart costs it --
    the unit declares `Requires=voyn-aicc-pgtunnel.service` and the credential
    rotation restarts that tunnel on its own timer.

    Afterwards nothing has happened to the queue: the lease is live, the
    reaper has nothing to recover, and the probe reads an ATTENDED claim. The
    lapsed-claim class -- the one starvation clock `evaluate` neither excuses
    by capacity nor bounds by the fleet clock, and whose only exit is the
    reaper -- is never entered at all.

    Before the fix the third beat landed on the deadline: the lease was lost,
    `daemon._execute` discarded up to a whole `TimeoutStopSec=3660s` run, the
    reaper handed the item to another lane, the side effects re-ran and
    `queue_claim` had already charged `attempt_count` for the delivery that
    was taken away."""
    from command_center.db.work_queue_admin import WorkQueueAdmin
    from command_center.worker.daemon import TOLERATED_FAILED_BEATS

    measure, app, worker, age = monitor
    app.enqueue(QUEUE, idempotency_key="long-run", payload={"kind": "agent_run"})
    claimed = _claim(worker, visibility=BLIP_WINDOW)
    age(
        "UPDATE work_item SET updated_at = now() - interval '40 minutes' "
        "WHERE current_attempt_id = %s",
        (claimed.attempt_id,),
    )

    beats = _blipping(worker, TOLERATED_FAILED_BEATS)
    lost = _beat_through(worker, claimed, BLIP_WINDOW)

    assert beats[:TOLERATED_FAILED_BEATS] == ["raised"] * TOLERATED_FAILED_BEATS
    assert "refused" not in beats, f"the recovery beat arrived too late: {beats}"
    assert not lost, f"{TOLERATED_FAILED_BEATS} failed beats lost the lease: {beats}"

    # The reaper's tick, run as the identity `aicc-queue-reaper.service`
    # carries. A live lane's item is not its business.
    assert WorkQueueAdmin(app._factory).reap() == 0

    snapshot = measure()
    assert (snapshot.claimed, snapshot.attended_claims) == (1, 1)
    assert snapshot.lapsed_claims == 0
    assert snapshot.lapsed_claim_age_seconds is None
    # The claim IS older than the stall window -- it is a long run, which is
    # what this fleet is for -- and that is bounded by --max-claim-seconds,
    # never by the stall clock.
    assert snapshot.live_claim_age_seconds > MAX_STALLED

    report = infra_monitor.evaluate(
        {}, snapshot, minimum_active_workers=0, max_stalled_seconds=MAX_STALLED,
        prometheus_ready=True, claim_capacity=2,
    )
    assert report.ok, report.failures


def test_a_blip_past_the_tolerance_still_hands_the_item_back(monitor) -> None:
    """The other direction, so the fix is a MARGIN and not a weakening of the
    protocol. One beat past the tolerance and the lease is gone exactly as
    before: the server refuses the late beat, the daemon stops the work, and
    `aicc-queue-reaper.timer` recovers the item on its next tick.

    This is what keeps the recovery path honest -- an outage longer than the
    fleet is built to absorb must still reach the reaper, which is the only
    exit from the class `lapsed_claim_age_seconds` measures."""
    from command_center.db.work_queue_admin import WorkQueueAdmin
    from command_center.worker.daemon import TOLERATED_FAILED_BEATS

    measure, app, worker, _age = monitor
    app.enqueue(QUEUE, idempotency_key="long-blip", payload={"kind": "agent_run"})
    claimed = _claim(worker, visibility=BLIP_WINDOW)

    beats = _blipping(worker, TOLERATED_FAILED_BEATS + 1)
    lost = _beat_through(worker, claimed, BLIP_WINDOW)

    assert lost, f"a blip past the tolerance must stop the work: {beats}"

    lapsed = measure()
    assert (lapsed.claimed, lapsed.attended_claims) == (1, 0)
    assert lapsed.lapsed_claims == 1

    assert WorkQueueAdmin(app._factory).reap() == 1
    recovered = measure()
    assert recovered.claimed == 0
    assert recovered.lapsed_claims == 0
