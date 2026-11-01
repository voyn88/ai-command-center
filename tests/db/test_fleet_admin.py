"""`FleetAdmin` — the fleet's single-panel view, proved against real PostgreSQL.

`tests/db/test_enrollment.py` proves the enrolment protocol itself: minting
and redeeming tickets, per-host roles, single-use tickets, tier violations.
What it does not prove is the read/lifecycle seam VOYN-MIN-FARM asks for — an
operator's "10 devices managed by one operational panel" — so this file seeds
a fleet of worker-host devices directly (bypassing the enrolment ceremony,
which is proved elsewhere) and drives `FleetAdmin` against it *as the roles
production runs as*: `aicc_app` for the panel read, `aicc_operator` for the
suspend decision. A unit test at this seam would mock the very SQL and grants
whose shape is in question, so this runs against a real server instead.

Skipped wholesale unless `AICC_TEST_PG_ADMIN_DSN` is set — see `conftest`.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.db import roles
from command_center.db.fleet_admin import FleetAdmin, UnknownDeviceError

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

DEVICE_COUNT = 10


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Ten worker-host devices, seeded through `identity_enroll_worker` — the
    same function `enroll_redeem_ticket()` calls — rather than
    `identity_bootstrap_principal`, so each device gets a REAL cluster LOGIN
    role. `identity_revoke_principal` disables that role on suspend
    (`identity_disable_role`), which errors on a role that was never
    created; the enrolment protocol itself is proved end to end by
    `tests/db/test_enrollment.py`, so this test only needs its output."""
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)
        with conn.cursor() as cur:
            for i in range(DEVICE_COUNT):
                cur.execute(
                    "SELECT identity_enroll_worker(%s, %s, %s)",
                    (
                        f"worker:edge-{i:02d}",
                        f"Edge device {i:02d}",
                        f"edge-{i:02d}.fleet.internal",
                    ),
                )


def _factory_for(psycopg, dsn: str):
    @contextmanager
    def factory():
        with psycopg.connect(dsn, autocommit=True) as conn:
            yield conn

    return factory


@pytest.fixture
def fleet(admin_conn, psycopg, test_dsn, role_passwords):
    """A ten-device fleet, plus an `aicc_app` and an `aicc_operator` admin —
    the two production identities `FleetAdmin` is actually driven as.

    Per-host roles are cluster objects (like `tests/db/test_enrollment.py`'s
    `_cluster`): dropping the per-test database does not remove them, so the
    teardown drops every `aicc_w_*` role this fixture's fleet created.
    """
    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    operator_dsn = _as_role(
        test_dsn, roles.OPERATOR_ROLE, role_passwords[roles.OPERATOR_ROLE]
    )
    try:
        yield (
            FleetAdmin(_factory_for(psycopg, app_dsn)),
            FleetAdmin(_factory_for(psycopg, operator_dsn)),
            admin_conn,
        )
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(r"SELECT rolname FROM pg_roles WHERE rolname LIKE 'aicc\_w\_%'")
            for (name,) in cur.fetchall():
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_the_whole_fleet_is_one_query(fleet) -> None:
    """The VOYN-MIN-FARM acceptance, literally: ten enrolled devices, and one
    `list_devices()` call surfaces all of them with what an operator needs to
    act on each — state and host."""
    app_admin, _operator_admin, _admin_conn = fleet

    devices = app_admin.list_devices()

    assert len(devices) == DEVICE_COUNT
    seen_ids = {device.principal_id for device in devices}
    assert seen_ids == {f"worker:edge-{i:02d}" for i in range(DEVICE_COUNT)}
    for device in devices:
        assert device.state == "active"
        assert device.host is not None and device.host.endswith(".fleet.internal")
        assert device.credential_expires_at is None  # none issued yet
        assert device.last_event_type == "enroll_worker"
        assert device.last_event_outcome == "granted"
        assert device.last_event_at is not None


def test_a_live_credential_surfaces_on_the_panel(fleet) -> None:
    """A device's live credential expiry is part of the panel row, joined from
    `principal_credential_public` — not a second call the operator has to
    make per device."""
    app_admin, _operator_admin, admin_conn = fleet
    principal_id = "worker:edge-00"
    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principal_credential (credential_id, principal_id,"
            " secret_hash, issued_at, expires_at, created_at, updated_at)"
            " VALUES (%s, %s, 'deadbeef', now(), now() + interval '1 hour',"
            " now(), now())",
            ("cred_test_1", principal_id),
        )

    devices = {d.principal_id: d for d in app_admin.list_devices()}

    assert devices[principal_id].credential_expires_at is not None


def test_state_filters_the_panel(fleet) -> None:
    app_admin, operator_admin, _admin_conn = fleet
    operator_admin.suspend("worker:edge-01", "incident drill")

    active = app_admin.list_devices(state="active")
    suspended = app_admin.list_devices(state="suspended")

    assert len(active) == DEVICE_COUNT - 1
    assert [d.principal_id for d in suspended] == ["worker:edge-01"]


def test_suspend_revokes_the_live_credential_and_is_audited(fleet) -> None:
    app_admin, operator_admin, admin_conn = fleet
    principal_id = "worker:edge-02"
    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principal_credential (credential_id, principal_id,"
            " secret_hash, issued_at, expires_at, created_at, updated_at)"
            " VALUES (%s, %s, 'deadbeef', now(), now() + interval '1 hour',"
            " now(), now())",
            ("cred_test_2", principal_id),
        )

    revoked = operator_admin.suspend(principal_id, "compromised host")

    assert revoked == 1
    devices = {d.principal_id: d for d in app_admin.list_devices()}
    assert devices[principal_id].state == "suspended"
    assert devices[principal_id].credential_expires_at is None
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, outcome, reason FROM principal_event"
            " WHERE principal_id = %s ORDER BY id DESC LIMIT 1",
            (principal_id,),
        )
        event_type, outcome, reason = cur.fetchone()
    assert (event_type, outcome, reason) == ("revoke", "granted", "compromised host")


def test_suspend_refuses_an_unknown_device(fleet) -> None:
    _app_admin, operator_admin, _admin_conn = fleet
    with pytest.raises(UnknownDeviceError):
        operator_admin.suspend("worker:does-not-exist", "typo test")


def test_suspend_is_operator_only(fleet) -> None:
    """The control plane may enrol new devices but must not be able to take
    the fleet offline — `identity_revoke_principal` is granted to
    `aicc_operator` alone, and this proves `FleetAdmin` neither works around
    that nor swallows the denial it produces."""
    import psycopg

    app_admin, _operator_admin, _admin_conn = fleet
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_admin.suspend("worker:edge-03", "should be refused")
