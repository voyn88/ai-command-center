"""The control-plane reconciler (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE) on
real PostgreSQL: `control_plane_heartbeat`/`control_plane_unit_state`/
`control_plane_event` are the substance under test (upserts, the circuit
breaker's stateful backoff, staleness math), so this suite runs them for
real rather than mocking the very tables in question. `systemctl` is faked
at the module seam -- `tests/orchestrator/test_self_deploy.py` already
establishes that pattern for the sibling self-deploy tick.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set -- see
``tests/db/conftest.py``.
"""

from __future__ import annotations

import datetime as dt

import pytest

from command_center.db import migrations, roles
from command_center.orchestrator.control_plane_reconciler import (
    DeclaredTimer,
    ReconcileConfig,
    check_heartbeats_once,
    reconcile_once,
    record_heartbeat,
)


class FakeSystemctl:
    """Records every call; `active`/`enabled` are the fake host's state,
    `start_ok` overrides whether `start()` succeeds per unit (default: yes,
    and succeeding flips the unit active -- the same effect a real
    `systemctl start` on a real timer/service has)."""

    def __init__(self, *, active=None, enabled=None, start_ok=None):
        self.active = dict(active or {})
        self.enabled = dict(enabled or {})
        self.start_ok = dict(start_ok or {})
        self.calls: list[tuple[str, str]] = []

    def is_active(self, unit: str) -> bool:
        self.calls.append(("is-active", unit))
        return self.active.get(unit, False)

    def is_enabled(self, unit: str) -> bool:
        self.calls.append(("is-enabled", unit))
        return self.enabled.get(unit, False)

    def start(self, unit: str) -> bool:
        self.calls.append(("start", unit))
        ok = self.start_ok.get(unit, True)
        if ok:
            self.active[unit] = True
        return ok

    def stop(self, unit: str) -> bool:
        self.calls.append(("stop", unit))
        self.active[unit] = False
        return True

    def disable(self, unit: str) -> bool:
        self.calls.append(("disable", unit))
        self.enabled[unit] = False
        return True


UTC = dt.UTC


def test_reconcile_restarts_an_inactive_timer(pg_connection_factory):
    fake = FakeSystemctl(active={"fake-a.timer": False})
    timer = DeclaredTimer("fake-a.timer", "fake-a.service", None, 60)

    report = reconcile_once(
        fake, pg_connection_factory, declared=(timer,), quarantine=()
    )

    assert report.restarted == ["fake-a.timer"]
    assert report.healthy
    assert fake.active["fake-a.timer"] is True
    assert ("start", "fake-a.timer") in fake.calls


def test_reconcile_leaves_an_active_timer_with_no_tick_alone(pg_connection_factory):
    fake = FakeSystemctl(active={"fake-b.timer": True})
    timer = DeclaredTimer("fake-b.timer", "fake-b.service", None, 60)

    report = reconcile_once(
        fake, pg_connection_factory, declared=(timer,), quarantine=()
    )

    assert report.ok == ["fake-b.timer"]
    assert report.restarted == []
    assert ("start", "fake-b.service") not in fake.calls


def test_reconcile_opens_circuit_after_repeated_failures_and_escalates_once(
    pg_connection_factory,
):
    fake = FakeSystemctl(active={"fake-c.timer": False}, start_ok={"fake-c.timer": False})
    timer = DeclaredTimer("fake-c.timer", "fake-c.service", None, 60)
    config = ReconcileConfig(
        circuit_failure_threshold=2, base_cooldown_seconds=1, max_cooldown_seconds=1
    )
    now0 = dt.datetime(2026, 1, 1, tzinfo=UTC)

    first = reconcile_once(
        fake,
        pg_connection_factory,
        declared=(timer,),
        quarantine=(),
        config=config,
        now=now0,
    )
    assert first.healthy  # one failure, threshold is 2 -- not escalated yet
    assert first.escalated == []

    # Advance past the 1s cooldown so the second attempt is not skipped as
    # still-in-cooldown -- it must count as a second CONSECUTIVE failure.
    now1 = now0 + dt.timedelta(seconds=5)
    second = reconcile_once(
        fake,
        pg_connection_factory,
        declared=(timer,),
        quarantine=(),
        config=config,
        now=now1,
    )
    assert not second.healthy
    assert second.escalated and second.escalated[0][0] == "fake-c.timer"

    # A third attempt inside the fresh cooldown window must not retry again
    # -- "escalate once", not hammer a unit that cannot be fixed by
    # restarting it again.
    now2 = now1 + dt.timedelta(seconds=0.1)
    third = reconcile_once(
        fake,
        pg_connection_factory,
        declared=(timer,),
        quarantine=(),
        config=config,
        now=now2,
    )
    assert third.circuit_open_skipped == ["fake-c.timer"]
    assert third.escalated == []


def test_reconcile_quarantines_a_sabotaging_unit(pg_connection_factory):
    fake = FakeSystemctl(active={"evil.timer": True}, enabled={"evil.timer": True})

    report = reconcile_once(
        fake, pg_connection_factory, declared=(), quarantine=("evil.timer",)
    )

    assert report.quarantined == ["evil.timer"]
    assert report.healthy
    assert fake.active["evil.timer"] is False
    assert fake.enabled["evil.timer"] is False
    assert ("stop", "evil.timer") in fake.calls
    assert ("disable", "evil.timer") in fake.calls


def test_reconcile_escalates_a_quarantine_that_does_not_take(pg_connection_factory):
    """A unit that reports active even AFTER stop/disable (e.g. respawned by
    something else) must not be reported healthy -- silently believing the
    quarantine worked would be exactly the kind of invisible failure this
    task exists to close."""

    class StubbornSystemctl(FakeSystemctl):
        def stop(self, unit):
            super().stop(unit)
            self.active[unit] = True  # refuses to actually stop
            return True

    fake = StubbornSystemctl(active={"evil.timer": True})
    report = reconcile_once(
        fake, pg_connection_factory, declared=(), quarantine=("evil.timer",)
    )

    assert report.quarantined == ["evil.timer"]
    assert not report.healthy
    assert report.escalated[0][0] == "evil.timer"


def test_reconcile_restarts_service_when_heartbeat_is_stale_despite_active_timer(
    pg_connection_factory,
):
    """The exact 2026-08-29 shape: the timer is active, but its tick's
    heartbeat never arrived (or went stale) -- `is-active` alone cannot see
    this, and the reconciler must restart the SERVICE, not the (already
    active) timer."""
    fake = FakeSystemctl(active={"fake-d.timer": True})
    timer = DeclaredTimer(
        "fake-d.timer", "fake-d.service", "fake-d-tick", 60, max_missed_intervals=2
    )

    report = reconcile_once(
        fake, pg_connection_factory, declared=(timer,), quarantine=()
    )

    assert "fake-d.timer#heartbeat" in report.restarted
    assert ("start", "fake-d.service") in fake.calls
    # The timer itself was never restarted -- only the service behind it.
    assert ("start", "fake-d.timer") not in fake.calls


def test_reconcile_leaves_a_fresh_heartbeat_alone(pg_connection_factory):
    fake = FakeSystemctl(active={"fake-e.timer": True})
    timer = DeclaredTimer(
        "fake-e.timer", "fake-e.service", "fake-e-tick", 60, max_missed_intervals=2
    )
    now = dt.datetime(2026, 1, 1, tzinfo=UTC)
    record_heartbeat(pg_connection_factory, "fake-e-tick", now=now)

    report = reconcile_once(
        fake,
        pg_connection_factory,
        declared=(timer,),
        quarantine=(),
        now=now + dt.timedelta(seconds=30),
    )

    assert report.ok == ["fake-e.timer"]
    assert ("start", "fake-e.service") not in fake.calls


def test_reconcile_writes_its_own_heartbeat_every_tick(pg_connection_factory):
    fake = FakeSystemctl()
    reconcile_once(fake, pg_connection_factory, declared=(), quarantine=())

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_ok_at FROM control_plane_heartbeat "
                "WHERE tick_name = 'control-reconcile'"
            )
            row = cur.fetchone()
    assert row is not None


def test_check_heartbeats_flags_stale_and_missing_but_not_fresh(pg_connection_factory):
    now = dt.datetime(2026, 1, 1, tzinfo=UTC)
    fresh = DeclaredTimer("f.timer", "f.service", "f-tick", 60, max_missed_intervals=2)
    stale = DeclaredTimer("s.timer", "s.service", "s-tick", 60, max_missed_intervals=2)
    missing = DeclaredTimer("m.timer", "m.service", "m-tick", 60, max_missed_intervals=2)

    record_heartbeat(pg_connection_factory, "f-tick", now=now)
    record_heartbeat(pg_connection_factory, "s-tick", now=now - dt.timedelta(seconds=1000))

    report = check_heartbeats_once(
        pg_connection_factory,
        declared=(fresh, stale, missing),
        now=now + dt.timedelta(seconds=30),
    )

    assert report.ok == ["f-tick"]
    stale_names = {name for name, _age in report.stale}
    assert stale_names == {"s-tick", "m-tick"}
    assert not report.healthy


def test_check_heartbeats_is_healthy_when_everything_is_fresh(pg_connection_factory):
    now = dt.datetime(2026, 1, 1, tzinfo=UTC)
    timer = DeclaredTimer("f2.timer", "f2.service", "f2-tick", 60, max_missed_intervals=2)
    record_heartbeat(pg_connection_factory, "f2-tick", now=now)

    report = check_heartbeats_once(
        pg_connection_factory, declared=(timer,), now=now + dt.timedelta(seconds=10)
    )

    assert report.healthy
    assert report.ok == ["f2-tick"]


# ---------------------------------------------------------------------------
# Privilege boundary: worker-01's cross-check must not be able to forge its
# own evidence (the SECOND acceptance clause -- "a reconciler that dies the
# same way is no reconciler" only holds if the checker cannot fake health).
# ---------------------------------------------------------------------------


def _as_role(dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=role_passwords[role])
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@pytest.mark.usefixtures("role_passwords")
def test_worker_reads_heartbeat_but_cannot_write_it_or_read_other_control_plane_tables(
    admin_conn, psycopg, test_dsn, role_passwords
):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_heartbeat (tick_name, last_ok_at, detail) "
            "VALUES ('seed-tick', now(), '')"
        )
        cur.execute(
            "INSERT INTO control_plane_event (unit_name, action, outcome, detail) "
            "VALUES ('seed.timer', 'restart_timer', 'ok', '')"
        )

    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tick_name FROM control_plane_heartbeat")
            assert cur.fetchone() is not None

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="permission denied"):
                cur.execute(
                    "INSERT INTO control_plane_heartbeat "
                    "(tick_name, last_ok_at, detail) VALUES ('forged', now(), '')"
                )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="permission denied"):
                cur.execute("SELECT * FROM control_plane_unit_state")

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="permission denied"):
                cur.execute("SELECT * FROM control_plane_event")
