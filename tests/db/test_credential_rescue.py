"""Exercise rescue DDL and audit against the optional real PostgreSQL fixture."""

import secrets
from datetime import datetime, timedelta

import pytest

from command_center.ops.credential_rescue import AdminSession, RescueConfig, run


@pytest.mark.serial
def test_rescue_updates_role_ledger_and_audit(pg_connection_factory, admin_conn):
    from psycopg import sql

    # Include a quote to prove role names from the database are identifiers.
    role = 'rescue_test_"' + secrets.token_hex(8)
    principal = "wrk:" + secrets.token_hex(8)
    credential = secrets.token_hex(16)
    admin_conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    try:
        admin_conn.execute(
            "INSERT INTO principal (principal_id, kind, db_role, display_name, host, "
            "state, trust_tier, created_at, updated_at) "
            "VALUES (%s, 'worker_host', %s, 'Rescue test', 'example', "
            "'active', 2, now(), now())",
            (principal, role),
        )
        previous = admin_conn.execute(
            "INSERT INTO principal_credential (credential_id, principal_id, "
            "secret_hash, issued_at, expires_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, now() - interval '2 hours', "
            "now() - interval '1 hour', now(), now()) RETURNING expires_at",
            (credential, principal, secrets.token_hex(32)),
        ).fetchone()[0]
        admin_conn.execute(
            sql.SQL("ALTER ROLE {} VALID UNTIL {}").format(
                sql.Identifier(role),
                sql.Literal(previous.isoformat()),
            )
        )
        with pg_connection_factory() as connection:
            repository = AdminSession(connection)
            assert run(repository, RescueConfig(dry_run=True)) == 0
            assert (
                admin_conn.execute("SELECT count(*) FROM principal_event").fetchone()[0]
                == 0
            )
            assert run(repository, RescueConfig()) == 0
            assert run(repository, RescueConfig()) == 0  # healthy on the next tick
        expiry, role_expiry = admin_conn.execute(
            "SELECT c.expires_at, r.rolvaliduntil FROM principal_credential c "
            "JOIN pg_roles r ON r.rolname = %s WHERE c.credential_id = %s",
            (role, credential),
        ).fetchone()
        assert expiry == role_expiry
        events = admin_conn.execute(
            "SELECT event_type, outcome, reason, metadata_json, created_at "
            "FROM principal_event WHERE principal_id = %s",
            (principal,),
        ).fetchall()
        assert len(events) == 1
        action, outcome, reason, detail, created = events[0]
        assert (action, outcome, reason) == (
            "rescue",
            "granted",
            "expired_credential_rescued",
        )
        assert datetime.fromisoformat(detail["previous_expires_at"]) == previous
        assert datetime.fromisoformat(detail["new_expires_at"]) == expiry
        assert detail["grace_seconds"] == 2040
        assert expiry == created + timedelta(seconds=2040)
        # Re-expiry must hit the persisted audit bound, not grant again.
        admin_conn.execute(
            "UPDATE principal_credential SET expires_at = %s WHERE credential_id = %s",
            (previous, credential),
        )
        assert run(repository, RescueConfig()) == 3
    finally:
        admin_conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
