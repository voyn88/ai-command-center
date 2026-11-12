"""Operator CLI authority against real PostgreSQL login and SET ROLE semantics.

The shared fixtures provision standard product logins in a disposable cluster
and a fresh database per test. No extra membership grants authorize a caller.
"""
from __future__ import annotations

import pytest

from ops import aicc_delivery_team_operator as cli


# Product roles are cluster-wide, including the session-scoped login fixture.
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def test_authenticated_operator_login_is_allowed(connect_as):
    with connect_as("aicc_operator") as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (
            "aicc_operator", "aicc_operator",
        )
        cli._authority(conn)


@pytest.mark.parametrize("role", ["aicc_operator", "aicc_app", "aicc_worker"])
def test_dedicated_adapter_pool_preserves_authenticated_role(test_dsn, role_passwords, psycopg, role):
    dsn = psycopg.conninfo.make_conninfo(test_dsn, user=role, password=role_passwords[role])
    with cli._operator_connection(dsn, autocommit=False, connect_timeout=10) as conn:
        assert conn.execute("SELECT session_user").fetchone() == (role,)
        if role == "aicc_operator":
            cli._authority(conn)
        else:
            with pytest.raises(ValueError, match="^operator authority required$"):
                cli._authority(conn)


@pytest.mark.parametrize("role", ["aicc_app", "aicc_worker"])
def test_app_and_worker_logins_are_denied(connect_as, role):
    with connect_as(role) as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (role, role)
        with pytest.raises(ValueError, match="^operator authority required$"):
            cli._authority(conn)


def test_superuser_login_is_not_operator_authority(admin_conn):
    # Superusers pass pg_has_role checks regardless of explicit memberships;
    # the CLI must still refuse the real administrative login.
    assert admin_conn.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = session_user"
    ).fetchone() == (True,)
    with pytest.raises(ValueError, match="^operator authority required$"):
        cli._authority(admin_conn)


def test_set_role_operator_does_not_replace_authenticated_identity(admin_conn):
    session = admin_conn.execute("SELECT session_user").fetchone()[0]
    assert session != "aicc_operator"
    try:
        admin_conn.execute("SET ROLE aicc_operator")
        assert admin_conn.execute("SELECT session_user, current_user").fetchone() == (
            session, "aicc_operator",
        )
        with pytest.raises(ValueError, match="^operator authority required$"):
            cli._authority(admin_conn)
    finally:
        admin_conn.execute("RESET ROLE")
    assert admin_conn.execute("SELECT current_user").fetchone() == (session,)
