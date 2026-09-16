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


# ---------------------------------------------------------------------------
# The recovery path must make PROGRESS, not merely avoid corrupting anything
# (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, migration 0028).
#
# `queue_reap` is the only thing that clears a lapsed claim, and a lapsed
# claim is the one starvation class `infra_monitor.evaluate` neither gates on
# capacity nor bounds by the fleet clock -- so a reaper that stops recovering
# is a `queue_stalled` no lane restart and no redrive can reach. 0002 proved
# the reap could not RACE a completion and stopped there; these prove it also
# cannot be STOPPED by one, which is a different property and the one the
# monitor depends on.
# ---------------------------------------------------------------------------


def _lapse_every_lease(psycopg, admin_dsn_for_owner) -> None:
    with psycopg.connect(admin_dsn_for_owner, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_attempt SET visible_until = now() - interval '1 second' "
                "WHERE state = 'active'"
            )


def test_one_contended_item_cannot_stop_every_other_recovery(
    queue_actors, admin_conn, test_dsn
) -> None:
    """THE REGRESSION. Three zombie claims, one of whose rows another
    transaction is holding. Before 0028 the reap WAITED on that row -- so a
    single contended item stopped the recovery of every item behind it, and
    the tick died on `aicc-queue-reaper.service`'s `TimeoutStartSec=60s`
    having recovered nothing at all.

    Measured before the fix, with a 5s `statement_timeout` standing in for
    that 60s kill: `canceling statement due to statement timeout / CONTEXT:
    while locking tuple (0,4)`, `recovered by the interrupted reap: 0 of 3`.
    """
    store, admin, psycopg, app_dsn = queue_actors
    claims = []
    for index in range(3):
        _enqueue(psycopg, app_dsn, f"contended-{index}", {"kind": "echo"})
        claimed = store.claim(QUEUE, visibility_seconds=60)
        assert isinstance(claimed, ClaimedWork)
        claims.append(claimed)
    _lapse_every_lease(psycopg, test_dsn)
    held = claims[1]

    # Any of the parties that legitimately hold an item's row: a heartbeat or
    # a report through `_queue_owns`, a duplicate `queue_enqueue` since 0027,
    # a `queue_redrive`, a migration's ALTER, an operator at a psql prompt.
    with psycopg.connect(test_dsn, autocommit=False) as holder:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM work_item WHERE work_item_id = %s FOR UPDATE",
                (held.work_item_id,),
            )
        # The reaper must not be able to sit on that lock. A bound well under
        # the unit's own 60s proves it never waits at all, rather than proving
        # it happened to finish in time.
        with psycopg.connect(app_dsn, autocommit=True) as reaper:
            with reaper.cursor() as cur:
                cur.execute("SET lock_timeout = '2s'")
                cur.execute("SET statement_timeout = '10s'")
                cur.execute("SELECT queue_reap()")
                reaped = int(cur.fetchone()[0])
        holder.rollback()

    assert reaped == 2, (
        "the two uncontended items must be recovered while the third is held"
    )
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT work_item_id, state FROM work_item WHERE work_item_id = ANY(%s)",
            ([claim.work_item_id for claim in claims],),
        )
        states = dict(cur.fetchall())
    assert states[claims[0].work_item_id] == "ready"
    assert states[claims[2].work_item_id] == "ready"
    # Deferred, not lost: the next tick takes it now that nobody holds it.
    assert states[held.work_item_id] == "claimed"
    assert admin.reap() == 1
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM work_item WHERE work_item_id = %s",
            (held.work_item_id,),
        )
        assert cur.fetchone()[0] == "ready"


def test_a_deferred_item_is_audited_rather_than_silently_skipped(
    queue_actors, admin_conn, test_dsn
) -> None:
    """Stepping past a held row must leave a record. A reaper that skipped in
    silence would report the same number for "nothing was lapsed" and "an item
    has been contended every tick for an hour", and the second is the one an
    operator needs to see."""
    store, _admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "deferred-1", {"kind": "echo"})
    claimed = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    with psycopg.connect(test_dsn, autocommit=False) as holder:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM work_item WHERE work_item_id = %s FOR UPDATE",
                (claimed.work_item_id,),
            )
        with psycopg.connect(app_dsn, autocommit=True) as reaper:
            with reaper.cursor() as cur:
                cur.execute("SET lock_timeout = '2s'")
                assert int(cur.execute("SELECT queue_reap()").fetchone()[0]) == 0
        holder.rollback()

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT work_item_id, attempt_id, outcome, reason, detail FROM work_event "
            "WHERE event = 'expire' AND outcome = 'rejected'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1, rows
    item_id, attempt_id, _outcome, reason, detail = rows[0]
    assert reason == "item_locked"
    assert attempt_id == claimed.attempt_id
    # NULL item id with the id in `detail`: `_queue_audit`'s seq is only
    # collision-free for a caller holding the item's row lock (0027), and not
    # holding it is exactly why this branch ran.
    assert item_id is None
    assert detail["deferred_work_item_id"] == claimed.work_item_id


def test_an_interrupted_reap_keeps_the_batches_it_already_committed(
    queue_actors, admin_conn, test_dsn
) -> None:
    """The second half of 0028. One `queue_reap` call is one transaction, so
    an interrupted call recovers NOTHING -- not everything up to where it
    stopped. `WorkQueueAdmin.reap` therefore commits bounded batches, and an
    interruption costs one batch instead of the whole backlog."""
    store, admin, psycopg, app_dsn = queue_actors
    total = WorkQueueAdmin.REAP_BATCH + 3
    for index in range(total):
        _enqueue(psycopg, app_dsn, f"batched-{index}", {"kind": "echo"})
        claimed = store.claim(QUEUE, visibility_seconds=60)
        assert isinstance(claimed, ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    # One bounded call stops at its bound and commits exactly that much.
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT queue_reap(%s)", (WorkQueueAdmin.REAP_BATCH,))
            assert int(cur.fetchone()[0]) == WorkQueueAdmin.REAP_BATCH
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE state = 'ready'")
        committed = int(cur.fetchone()[0])
    assert committed == WorkQueueAdmin.REAP_BATCH, (
        "a bounded call must commit its batch, not hold it hostage to the rest"
    )

    # And the seam loops until a batch comes back short, so nothing is left.
    assert admin.reap() == total - WorkQueueAdmin.REAP_BATCH
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE state = 'claimed'")
        assert int(cur.fetchone()[0]) == 0


def test_a_non_positive_bound_still_makes_progress(queue_actors, test_dsn) -> None:
    """A caller error must not mean "reap nothing for ever" -- the one
    outcome that turns a bounded reaper into no reaper at all."""
    store, _admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "clamped-1", {"kind": "echo"})
    assert isinstance(store.claim(QUEUE, visibility_seconds=60), ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT queue_reap(0)")
            assert int(cur.fetchone()[0]) == 1


def test_the_unbounded_arity_survives_for_a_host_that_has_not_redeployed(
    queue_actors, test_dsn
) -> None:
    """`queue_reap()` keeps its exact signature -- and with it 0002's EXECUTE
    grant -- so the reaper timer running from an older checkout keeps working
    AND inherits the liveness fix, because the fix is in the body both
    arities share. 0024's lesson: a `DEFAULT` on the new parameter would have
    made this call ambiguous instead."""
    store, _admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "old-caller-1", {"kind": "echo"})
    assert isinstance(store.claim(QUEUE, visibility_seconds=60), ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT queue_reap()")
            assert int(cur.fetchone()[0]) == 1


def test_reap_falls_back_when_the_schema_has_not_reached_0028(
    queue_actors, admin_conn, test_dsn
) -> None:
    """The control host and the worker host deploy independently, so this
    module can be newer than the database it is talking to. A reaper that
    refused to run at all against a 0027 schema would be the one outcome
    worse than an unbatched one -- recovery is the thing whose absence the
    monitor reports as a stall."""
    store, admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "skew-1", {"kind": "echo"})
    assert isinstance(store.claim(QUEUE, visibility_seconds=60), ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    # The database as it genuinely was before 0028 -- 0002's blocking,
    # unbounded body and no second arity -- reached the way a rolled-back
    # control host reaches it, not by hand-dropping half of the pair.
    from command_center.db import migrations

    migrations.downgrade(admin_conn, target=27)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'queue_reap' "
            "AND pg_get_function_identity_arguments(oid) <> ''"
        )
        assert cur.fetchone()[0] == 0

    assert admin.reap() == 1


class _RecordingConnection:
    """A real connection that remembers which statements were sent.

    The distinction under test is *which arity `reap` falls back to*, and no
    observable state separates them: both recover the same rows. So the test
    reads the wire rather than the outcome. Everything else delegates, so the
    server, the role and the grants are still the real ones.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.statements: list[str] = []

    class _Cursor:
        def __init__(self, inner, log) -> None:
            self.inner, self.log = inner, log

        def execute(self, query, params=None):
            self.log.append(query)
            return self.inner.execute(query, params)

        def fetchone(self):
            return self.inner.fetchone()

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    def cursor(self):
        return self._Cursor(self.inner.cursor(), self.statements)

    def rollback(self):
        return self.inner.rollback()


def _admin_over(psycopg, dsn, recorder_box) -> WorkQueueAdmin:
    from contextlib import contextmanager

    @contextmanager
    def factory():
        with psycopg.connect(dsn, autocommit=True) as conn:
            recorder = _RecordingConnection(conn)
            recorder_box.append(recorder)
            yield recorder

    return WorkQueueAdmin(factory)


def test_a_grant_that_missed_the_new_arity_is_raised_not_papered_over(
    queue_actors, admin_conn, test_dsn
) -> None:
    """THE REGRESSION for the fallback's reach. It was a bare
    `except Exception` on the first batch, so ANY first-call failure re-ran
    the UNBOUNDED arity -- and the fallback is only correct for one of them.

    A role re-provision that missed 0028's `GRANT EXECUTE ... queue_reap(integer)`
    leaves the no-argument arity working (it carries 0002's grant) and the
    bounded one refused: `InsufficientPrivilege`, SQLSTATE 42501, measured.
    The old code answered that with the unbounded whole-table scan and
    returned a count, so a real deploy fault read as a healthy reap and
    nothing on the host ever said otherwise -- on the one path whose absence
    `control-01:queue` reports as `queue_stalled` with no exit a lane restart
    can reach.
    """
    store, _admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "ungranted-1", {"kind": "echo"})
    assert isinstance(store.claim(QUEUE, visibility_seconds=60), ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    with admin_conn.cursor() as cur:
        cur.execute(
            f"REVOKE EXECUTE ON FUNCTION queue_reap(integer) FROM {roles.APP_ROLE}"
        )

    seen: list[_RecordingConnection] = []
    admin = _admin_over(psycopg, app_dsn, seen)
    with pytest.raises(Exception) as caught:
        admin.reap()
    assert getattr(caught.value, "sqlstate", None) == "42501", caught.value

    # And it did NOT reach for the unbounded arity on the way out.
    sent = [statement for recorder in seen for statement in recorder.statements]
    assert not any("queue_reap()" in statement for statement in sent), sent

    # The lapsed claim is therefore still lapsed -- which is the honest state,
    # and the state the probe's `lapsed_claim_age_seconds` will report. A
    # silent fallback would have cleared it and hidden the broken grant.
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE state = 'claimed'")
        assert int(cur.fetchone()[0]) == 1


def test_an_interrupted_batch_is_never_retried_unbounded(
    queue_actors, admin_conn, test_dsn
) -> None:
    """The other error the bare `except` caught by mistake, and the worse one.

    An interruption -- `aicc-queue-reaper.service` is a `Type=oneshot` with
    `TimeoutStartSec=60s`, and the connection crosses the tunnel the
    credential rotation restarts -- arrives as `QueryCanceled`, SQLSTATE
    57014. Answering THAT by re-running the unbounded arity re-runs the
    whole-table scan whose all-or-nothing rollback is the defect 0028 removed,
    over strictly more rows than the batch that just failed, at the one moment
    the server has already shown it cannot finish that much work.

    `statement_timeout` is how the test interrupts it, because it is the same
    cancellation systemd's kill and the tunnel's restart produce.

    A FULL batch against a 1ms timeout, not a token few rows, so the margin is
    not a bet on the machine's speed: measured on PostgreSQL 16, five
    expirations and their audit rows already take ~5ms server-side, so a
    hundred of them run two orders of magnitude past the bound.
    """
    store, _admin, psycopg, app_dsn = queue_actors
    total = WorkQueueAdmin.REAP_BATCH + 3
    for index in range(total):
        _enqueue(psycopg, app_dsn, f"interrupted-{index}", {"kind": "echo"})
        assert isinstance(store.claim(QUEUE, visibility_seconds=60), ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    from contextlib import contextmanager

    seen: list[_RecordingConnection] = []

    @contextmanager
    def factory():
        with psycopg.connect(app_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '1ms'")
            recorder = _RecordingConnection(conn)
            seen.append(recorder)
            yield recorder

    with pytest.raises(Exception) as caught:
        WorkQueueAdmin(factory).reap()
    assert getattr(caught.value, "sqlstate", None) == "57014", caught.value

    sent = [statement for recorder in seen for statement in recorder.statements]
    assert not any("queue_reap()" in statement for statement in sent), sent

    # Nothing was recovered and nothing was lost: the next tick starts from
    # the same place, with its bound intact.
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE state = 'claimed'")
        assert int(cur.fetchone()[0]) == total


def test_the_reaper_timers_own_entrypoint_recovers_a_lapsed_claim(
    queue_actors, test_dsn, monkeypatch, capsys
) -> None:
    """What `aicc-queue-reaper.service` actually execs, run end to end.

    Everything above proves `WorkQueueAdmin.reap` against a real server as
    `aicc_app`. None of it proves the thing systemd starts every minute:

        ExecStart=/opt/aicc/.venv/bin/python -m command_center.db queue-reap

    and between that line and the method under test sits a seam nothing
    exercised -- `tests/db/test_cli_queue.py` reaches `queue-reap` at the
    argparse layer and stops there. The seam is not incidental plumbing;
    every property 0028's fix rests on lives in it:

      * THE CONNECTION IS AUTOCOMMIT. `reap`'s batching is only a fix
        because each `queue_reap(REAP_BATCH)` is durable before the next one
        starts -- "an interruption costs one batch". That is a fact about
        `pool.connection()` (`autocommit=True`), not about the loop, and the
        loop cannot tell the difference. Handed a transactional connection it
        would still return the same count while an interrupted tick threw
        away the whole backlog again, which is the exact defect 0028 removed.
      * THE IDENTITY IS `aicc_app`. Recovery is deliberately not a worker
        privilege, and 0028's `GRANT EXECUTE ... queue_reap(integer)` names
        that role; the CLI gets it from `AICC_PG_*`, not from a DSN a test
        constructed.
      * AND IT IS THE ONLY EXIT FROM THE ONE STARVATION CLASS THE PROBE
        WEIGHS UNCONDITIONALLY. `infra_monitor.evaluate` excuses due ready
        work behind a full fleet and bounds it by the fleet clock, but
        `lapsed_claim_age_seconds` is neither excused nor bounded -- and no
        lane restart and no `queue_redrive` reaches a lapsed claim. A
        regression anywhere in this seam is `queue_stalled` on
        `control-01:queue` with no exit reachable by fleet action, and every
        test above stays green while it happens.

    Mirrors `test_readyz_is_200_against_a_real_database`, which exists for the
    same reason on the serving side: the unit tests there monkeypatched
    `pool.connection` and so stayed green while the served process never
    opened the pool at all.
    """
    from pathlib import Path

    from psycopg.conninfo import conninfo_to_dict

    from command_center.db import cli, pool
    from command_center.db.config import load_config

    # The two halves of that ExecStart this repository controls. Driving
    # `cli.main(["queue-reap"])` proves the CLI recovers a lapsed claim and
    # proves nothing about the timer if the unit execs something else.
    unit = (
        Path(__file__).resolve().parents[2] / "deploy/systemd/aicc-queue-reaper.service"
    ).read_text(encoding="utf-8")
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert "-m command_center.db" in exec_start
    assert exec_start.rstrip().endswith("queue-reap")

    store, _admin, psycopg, app_dsn = queue_actors
    _enqueue(psycopg, app_dsn, "reaper-entrypoint-1", {"kind": "echo"})
    claimed = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork)
    _lapse_every_lease(psycopg, test_dsn)

    params = conninfo_to_dict(app_dsn)
    monkeypatch.setenv("AICC_PG_HOST", params.get("host", "127.0.0.1"))
    monkeypatch.setenv("AICC_PG_PORT", str(params.get("port", 5432)))
    monkeypatch.setenv("AICC_PG_DB", params["dbname"])
    monkeypatch.setenv("AICC_PG_USER", params["user"])
    monkeypatch.setenv("AICC_PG_PASSWORD", params["password"])
    monkeypatch.setenv("AICC_PG_SSLMODE", "prefer")  # loopback, per config policy

    pool.close_pool()
    # The batching's whole claim, asserted where it actually lives. `reap`
    # loops on whatever connection it is handed and cannot tell a
    # transactional one from an autocommit one -- it would return the same
    # count either way, with the same interruption throwing away every batch.
    try:
        pool.open_pool(load_config())
        with pool.connection() as conn:
            assert conn.autocommit, (
                "the reaper's connection must be autocommit: without it a "
                "batched reap is one transaction again, and an interrupted "
                "tick recovers nothing (0028)"
            )
    finally:
        pool.close_pool()

    try:
        assert cli.main(["queue-reap"]) == 0
    finally:
        pool.close_pool()
    assert "reaped 1 lapsed attempt(s)" in capsys.readouterr().out

    # The exit code and the count are what the operator sees; this is what the
    # fleet sees. The item is claimable again, so the probe's lapsed-claim
    # clock has something to stop measuring.
    retaken = store.claim(QUEUE, visibility_seconds=60)
    assert isinstance(retaken, ClaimedWork)
    assert retaken.work_item_id == claimed.work_item_id
    assert retaken.attempt_no == claimed.attempt_no + 1
