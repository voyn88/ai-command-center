"""Real-PostgreSQL contract for migration 0016 finalization fencing."""

from __future__ import annotations

import pytest

from command_center.db import migrations, roles

pytestmark = pytest.mark.usefixtures("role_passwords")

VERSION = 16


def _as_role(dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=role_passwords[role])
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> str:
    roles.apply_bootstrap(admin_conn)
    migrator_dsn = _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords)
    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        migrations.upgrade(conn, target=VERSION)
        roles.apply_table_grants(conn)
    return migrator_dsn


def _claim_shape(admin_conn) -> dict:
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'run_finalization_claim' ORDER BY ordinal_position"
        )
        columns = cur.fetchall()
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' "
            "AND tablename = 'run_finalization_claim' ORDER BY indexname"
        )
        indexes = cur.fetchall()
        cur.execute(
            "SELECT contype, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conrelid = 'public.run_finalization_claim'::regclass "
            "ORDER BY contype, pg_get_constraintdef(oid)"
        )
        constraints = cur.fetchall()
    return {"columns": columns, "indexes": indexes, "constraints": constraints}


def test_claim_migration_round_trip_has_no_residue(
    admin_conn, psycopg, test_dsn, role_passwords
):
    migrator_dsn = _provision(admin_conn, psycopg, test_dsn, role_passwords)
    after = _claim_shape(admin_conn)
    assert after["columns"]

    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        assert migrations.downgrade(conn, target=VERSION - 1) == (VERSION,)
        with admin_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.run_finalization_claim')")
            assert cur.fetchone() == (None,)
        assert migrations.upgrade(conn, target=VERSION) == (VERSION,)

    assert _claim_shape(admin_conn) == after


def test_claim_shape_constraints_index_and_cascade(
    admin_conn, psycopg, test_dsn, role_passwords
):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    shape = _claim_shape(admin_conn)
    assert shape["columns"] == [
        ("run_id", "text", "NO"),
        ("owner_token", "text", "NO"),
        ("owner_pid", "bigint", "NO"),
        ("owner_identity", "text", "NO"),
        ("claimed_at", "timestamp with time zone", "NO"),
        ("completed_at", "timestamp with time zone", "YES"),
    ]
    indexdefs = "\n".join(definition for _name, definition in shape["indexes"])
    assert "idx_run_finalization_claim_open" in indexdefs
    assert "WHERE (completed_at IS NULL)" in indexdefs
    constraints = "\n".join(definition for _kind, definition in shape["constraints"])
    assert "PRIMARY KEY (run_id)" in constraints
    assert "REFERENCES run(id) ON DELETE CASCADE" in constraints
    assert "owner_pid > 0" in constraints
    assert "owner_token <> ''" in constraints
    assert "owner_identity <> ''" in constraints
    assert "completed_at >= claimed_at" in constraints
    with admin_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT count(*) FROM run_finalization_claim "
            "WHERE completed_at IS NULL"
        )
        plan = "\n".join(row[0] for row in cur.fetchall())
    assert "idx_run_finalization_claim_open" in plan

    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO task "
            "(id, project, title, task_type, created_at, updated_at) "
            "VALUES ('t', 'AIOS', 't', 'implementation', now(), now())"
        )
        cur.execute(
            "INSERT INTO session "
            "(id, task_id, project, repository_path, created_at, updated_at) "
            "VALUES ('s', 't', 'AIOS', '/tmp/repo', now(), now())"
        )
        cur.execute(
            "INSERT INTO run "
            "(id, session_id, task_id, sequence, state, project, task_type, "
            "repository_path, prompt, created_at, updated_at) "
            "VALUES ('r', 's', 't', 1, 'COMPLETED', 'AIOS', "
            "'implementation', '/tmp/repo', 'p', now(), now())"
        )
        cur.execute(
            "INSERT INTO run_finalization_claim "
            "(run_id, owner_token, owner_pid, owner_identity, claimed_at) "
            "VALUES ('r', 'token', 1, 'identity', now())"
        )
        cur.execute("DELETE FROM run WHERE id = 'r'")
        cur.execute("SELECT count(*) FROM run_finalization_claim")
        assert cur.fetchone() == (0,)


def test_no_runtime_role_has_direct_claim_table_privileges(
    admin_conn, psycopg, test_dsn, role_passwords
):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        for role in (roles.APP_ROLE, roles.WORKER_ROLE, roles.OPERATOR_ROLE):
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            ):
                cur.execute(
                    "SELECT has_table_privilege(%s, "
                    "'public.run_finalization_claim', %s)",
                    (role, privilege),
                )
                assert cur.fetchone() == (False,), (role, privilege)
