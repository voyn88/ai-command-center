"""Integration tests against a real PostgreSQL server.

Skipped unless `AICC_TEST_PG_ADMIN_DSN` is set (see conftest). Everything here
asserts a property the database itself enforces — schema shape, privilege
denial, dump/restore fidelity — so none of it can be faked out by a stub.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from command_center.db import migrations, roles

pytestmark = pytest.mark.usefixtures("role_passwords")

#: Derived, never hand-written. The version numbers below used to be literals,
#: which was correct while there was one migration; a second one turned every
#: such literal into a failing assertion about nothing in particular.
ALL_VERSIONS = tuple(m.version for m in migrations.discover())


def _provision(admin_conn, psycopg, test_dsn, role_passwords):
    """Bootstrap as superuser, migrate as the migrator — the production order."""
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        applied = migrations.upgrade(conn)
        roles.apply_table_grants(conn)
    return applied


def _as_role(dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=role_passwords[role])
    return make_conninfo(**params)


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------


def test_upgrade_creates_the_declared_schema(admin_conn, psycopg, test_dsn, role_passwords):
    applied = _provision(admin_conn, psycopg, test_dsn, role_passwords)
    assert applied == ALL_VERSIONS

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        tables = {row[0] for row in cur.fetchall()}
    assert tables == set(roles.ALL_TABLES)


def test_upgrade_is_idempotent(admin_conn, psycopg, test_dsn, role_passwords):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        assert migrations.upgrade(conn) == ()
        assert migrations.current_version(conn) == ALL_VERSIONS[-1]


def test_downgrade_removes_everything_and_upgrade_restores_it(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The acceptance criterion: forward *and* back, proven by execution."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    migrator_dsn = _as_role(test_dsn, "aicc_migrator", role_passwords)

    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        assert migrations.downgrade(conn, target=0) == ALL_VERSIONS[::-1]
        assert migrations.current_version(conn) == 0

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        # Only the migration ledger survives a full downgrade.
        assert cur.fetchone()[0] == 1

    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        assert migrations.upgrade(conn) == ALL_VERSIONS
        roles.apply_table_grants(conn)

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        assert {row[0] for row in cur.fetchall()} == set(roles.ALL_TABLES)


def test_editing_an_applied_migration_is_rejected(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Two environments must not report the same version for different schemas."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute("UPDATE schema_migration SET checksum = 'stale' WHERE version = 1")

    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        with pytest.raises(migrations.MigrationError, match="modified after it was applied"):
            migrations.upgrade(conn)


def test_typed_columns_reached_the_database(admin_conn, psycopg, test_dsn, role_passwords):
    """The decision to use real types is only real if the server agrees."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        types = {(t, c): d for t, c, d in cur.fetchall()}

    assert types[("run", "created_at")] == "timestamp with time zone"
    assert types[("run", "is_resume")] == "boolean"
    assert types[("run", "command_json")] == "jsonb"
    assert types[("run_event", "id")] == "bigint"
    assert types[("model_entry", "cost")] == "double precision"
    # No ISO-8601-in-TEXT timestamps survived the translation.
    assert not [
        key for key, value in types.items() if key[1].endswith("_at") and value == "text"
    ]


# --------------------------------------------------------------------------
# Privilege matrix, exercised as each role
# --------------------------------------------------------------------------


def test_public_cannot_create_objects(admin_conn, psycopg, test_dsn, role_passwords):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(_as_role(test_dsn, "aicc_app", role_passwords)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE smuggled (id text)")


def test_app_can_write_every_declared_table(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Exercise the actual privilege, not just readability.

    An earlier version of this test ran `SELECT count(*)` and asserted `>= 0`,
    which is true unconditionally and would have passed with every INSERT and
    UPDATE grant removed. Here each write is attempted and then rolled back, so
    a missing grant fails as `InsufficientPrivilege`.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, "aicc_app", role_passwords)

    for table in roles.ALL_TABLES:
        expected = roles.PRIVILEGES[roles.APP_ROLE][table]
        with psycopg.connect(app_dsn) as conn:
            with conn.cursor() as cur:
                # A table declared with no privileges at all must refuse even
                # the read. Asserted rather than skipped: `work_attempt` holds
                # `claim_token_hash`, and "the app happens not to select it" is
                # not the same claim as "the database refuses".
                if not expected:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        cur.execute(f"SELECT count(*) FROM {table}")
                    conn.rollback()
                    continue

                # A missing SELECT grant raises rather than returning a row.
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone() is not None

                # `WHERE false` reaches the privilege check without needing a
                # valid row for each of 34 different table shapes.
                if "UPDATE" in expected:
                    cur.execute(f"UPDATE {table} SET {_any_column(cur, table)} = NULL WHERE false")
                else:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        cur.execute(
                            f"UPDATE {table} SET {_any_column(cur, table)} = NULL WHERE false"
                        )
            conn.rollback()


def _any_column(cur, table: str) -> str:
    # Identity columns are excluded: `UPDATE ... SET id = NULL` on a GENERATED
    # ALWAYS column is rejected at parse time, before the privilege check that
    # is the point of the query.
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND is_identity = 'NO' "
        "ORDER BY ordinal_position LIMIT 1",
        (table,),
    )
    return cur.fetchone()[0]


def test_worker_cannot_forge_the_review_verdict(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Column-level grants, proven where a table-level grant would have leaked.

    A worker writes its own completion row, so it needs UPDATE on `completion`.
    Table-wide, that would also let a compromised execution host stamp
    `review_verdict = 'approved'` on any run — forging the decision the review
    gate exists to make.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    run_id = _seed_run(admin_conn)
    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO completion (run_id, task_id, project, repository_path, "
            "completion_state, created_at, updated_at) "
            "SELECT id, task_id, project, repository_path, 'running', now(), now() "
            "FROM run WHERE id = %s",
            (run_id,),
        )

    worker_dsn = _as_role(test_dsn, "aicc_worker", role_passwords)

    with psycopg.connect(worker_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE completion SET completion_state = 'merged' WHERE run_id = %s",
                (run_id,),
            )
            assert cur.rowcount == 1

    for column in ("review_verdict", "review_summary", "review_run_id"):
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE completion SET {column} = 'approved' WHERE run_id = %s",
                        (run_id,),
                    )


def test_app_cannot_rewrite_the_migration_ledger(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The ledger checksum is the only guard against silent schema divergence."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(_as_role(test_dsn, "aicc_app", role_passwords)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("UPDATE schema_migration SET checksum = 'forged'")


def test_app_cannot_drop_or_alter_tables(admin_conn, psycopg, test_dsn, role_passwords):
    """A SQL-injection foothold in the web layer must not reach DDL."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, "aicc_app", role_passwords)
    for statement in ("DROP TABLE task", "ALTER TABLE task ADD COLUMN x text"):
        with psycopg.connect(app_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(statement)


def test_no_role_may_delete_rows(admin_conn, psycopg, test_dsn, role_passwords):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    for role in ("aicc_app", "aicc_worker"):
        with psycopg.connect(_as_role(test_dsn, role, role_passwords)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM run")


@pytest.mark.parametrize(
    "table",
    ["proposal", "provenance_evidence", "motion", "council_vote", "audit_finding"],
)
def test_worker_cannot_read_governance_tables(
    admin_conn, psycopg, test_dsn, role_passwords, table
):
    """The core reason the worker role exists — proven, not asserted in a comment."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(_as_role(test_dsn, "aicc_worker", role_passwords)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table}")


def test_worker_cannot_write_the_queue_mirror(
    admin_conn, psycopg, test_dsn, role_passwords
):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    worker_dsn = _as_role(test_dsn, "aicc_worker", role_passwords)

    with psycopg.connect(worker_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO queue_entry (id, state) VALUES ('q1', 'pending')"
                )

    # Nor may it claim *here* any more. `queue_entry` is a mirror rebuilt
    # wholesale by `queue_store.replace_entries()`, so a claim written to it is
    # destroyed by the next sync — silently, since the worker has no `DELETE`
    # with which to notice. Claims live in `work_item` and are taken through
    # `queue_claim()`; see `tests/db/test_queue_claim.py`.
    with psycopg.connect(worker_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("UPDATE queue_entry SET state = 'claimed' WHERE id = 'absent'")

    with psycopg.connect(worker_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM queue_entry")  # reading is still allowed
            assert cur.fetchone()[0] == 0


def test_worker_can_append_run_events(admin_conn, psycopg, test_dsn, role_passwords):
    """Covers the identity-sequence grant: INSERT alone would fail here."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    run_id = _seed_run(admin_conn)

    with psycopg.connect(
        _as_role(test_dsn, "aicc_worker", role_passwords), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at) "
                "VALUES (%s, 1, 'started', %s, now()) RETURNING id",
                (run_id, "{}"),
            )
            assert cur.fetchone()[0] >= 1


def test_matrix_matches_the_catalog(admin_conn, psycopg, test_dsn, role_passwords):
    """Compare the declared matrix against `information_schema` row by row.

    The per-role tests above cover the cases that matter most; this one closes
    the gap between them, so a grant added to `PRIVILEGES` without a matching
    test still cannot widen access unnoticed.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    actual: dict[str, dict[str, set[str]]] = {}
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND grantee = ANY(%s)",
            (list(roles.ALL_ROLES),),
        )
        for grantee, table, privilege in cur.fetchall():
            actual.setdefault(grantee, {}).setdefault(table, set()).add(privilege)

        # Column-level grants do not appear in role_table_grants, so a matrix
        # entry narrowed to columns would look like a *missing* grant without
        # this second query.
        cur.execute(
            "SELECT grantee, table_name, privilege_type, column_name "
            "FROM information_schema.role_column_grants "
            "WHERE table_schema = 'public' AND grantee = ANY(%s)",
            (list(roles.ALL_ROLES),),
        )
        column_grants: dict[str, dict[str, dict[str, set[str]]]] = {}
        for grantee, table, privilege, column in cur.fetchall():
            column_grants.setdefault(grantee, {}).setdefault(table, {}).setdefault(
                privilege, set()
            ).add(column)

    for role in (roles.APP_ROLE, roles.WORKER_ROLE, roles.OPERATOR_ROLE):
        # An empty privilege set is a declared refusal, not a grant of nothing:
        # it must be absent from the catalog entirely. Views are folded in here
        # because `role_table_grants` reports them through the same catalog, so
        # leaving them out would read as three unexplained extra grants.
        expected = {t: set(p) for t, p in roles.PRIVILEGES[role].items() if p}
        expected.update(
            {v: set(p) for v, p in roles.VIEW_PRIVILEGES.get(role, {}).items() if p}
        )
        narrowed = roles.COLUMN_PRIVILEGES.get(role, {})
        seen = actual.get(role, {})
        for table, per_privilege in narrowed.items():
            for privilege, columns in per_privilege.items():
                granted = column_grants.get(role, {}).get(table, {}).get(privilege, set())
                assert granted == set(columns), f"{role}.{table}.{privilege}"
                seen.setdefault(table, set()).add(privilege)
        assert seen == expected, role

        # And the refusals, stated as their own claim rather than implied by the
        # equality above — a table that vanished from `PRIVILEGES` would satisfy
        # that equality while granting nothing, which reads the same and is not.
        refused = {t for t, p in roles.PRIVILEGES[role].items() if not p}
        assert refused, role
        assert not (refused & set(seen)), f"{role} reaches a table declared unreachable"


def test_execute_grants_match_the_declared_protocol_steps(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Functions are the worker's only reach, so they need their own catalog check.

    `role_table_grants` does not report `EXECUTE`, so the matrix test above is
    blind to exactly the grants that carry the claim protocol: every function
    could be granted to everyone and it would still pass.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'"
        )
        all_functions = [row[0] for row in cur.fetchall()]

    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        expected = set()
        for signature in roles.FUNCTION_PRIVILEGES.get(role, ()):
            name = signature.split("(")[0]
            matches = [f for f in all_functions if f.startswith(f"{name}(")]
            assert len(matches) == 1, f"{signature} does not identify one function"
            expected.add(matches[0])

        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' "
                "AND has_function_privilege(%s, p.oid, 'EXECUTE')",
                (role,),
            )
            granted = {row[0] for row in cur.fetchall()}
        assert granted == expected, role

    # The helpers the protocol is built out of are SECURITY DEFINER over every
    # table in it. PUBLIC gets EXECUTE on new functions by default, so this is
    # the assertion that the blanket revoke actually reached them.
    for helper in ("_queue_audit", "_queue_owns", "_queue_new_id"):
        assert any(f.startswith(f"{helper}(") for f in all_functions), helper


# --------------------------------------------------------------------------
# Backup / restore drill
# --------------------------------------------------------------------------


def _seed_run(conn) -> str:
    """Insert a minimal task→session→run chain and return the run id."""
    ident = uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO task (id, project, title, task_type, created_at, updated_at) "
            "VALUES (%s, 'p', 't', 'feature', now(), now())",
            (f"task-{ident}",),
        )
        cur.execute(
            "INSERT INTO session (id, task_id, project, repository_path, created_at, updated_at) "
            "VALUES (%s, %s, 'p', '/tmp/repo', now(), now())",
            (f"sess-{ident}", f"task-{ident}"),
        )
        cur.execute(
            "INSERT INTO run (id, session_id, task_id, sequence, state, project, task_type, "
            "repository_path, prompt, created_at, updated_at) "
            "VALUES (%s, %s, %s, 1, 'running', 'p', 'feature', '/tmp/repo', 'go', now(), now())",
            (f"run-{ident}", f"sess-{ident}", f"task-{ident}"),
        )
    return f"run-{ident}"


def _client_major() -> int | None:
    """Major version of the local `pg_dump`, or None if it is absent."""
    if not shutil.which("pg_dump"):
        return None
    out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    for token in out.split():
        if token[0].isdigit():
            return int(token.split(".")[0])
    return None


def _server_major(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        return int(cur.fetchone()[0]) // 10000


@pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("pg_restore") and shutil.which("psql")),
    reason="PostgreSQL client binaries are not installed",
)
def test_backup_restore_drill_round_trips_data(
    admin_conn, psycopg, test_dsn, role_passwords, pg_database, tmp_path
):
    """Run the real scripts end to end and verify the restored rows.

    Proving the *scripts* work, not just that pg_dump works: the acceptance
    criterion is a demonstrated restore, and the scripts are what an operator
    will actually run at 3am.
    """
    from psycopg.conninfo import conninfo_to_dict

    # pg_dump refuses to talk to a newer server outright. Skipping keeps that
    # environment mismatch from being reported as a broken backup script — the
    # deploy image and CI both pin a client at or above the server version.
    client, server = _client_major(), _server_major(admin_conn)
    if client is not None and client < server:
        pytest.skip(f"pg_dump {client} cannot dump a PostgreSQL {server} server")

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    run_id = _seed_run(admin_conn)

    params = conninfo_to_dict(test_dsn)
    env = {
        **os.environ,
        "AICC_PG_HOST": params.get("host", "127.0.0.1"),
        "AICC_PG_PORT": str(params.get("port", 5432)),
        "AICC_PG_DB": pg_database,
        "AICC_PG_USER": params["user"],
        "AICC_PG_PASSWORD": params["password"],
    }
    repo_root = _repo_root()
    backup_dir = tmp_path / "backups"

    subprocess.run(
        [
            str(repo_root / "scripts" / "aicc_pg_backup.sh"),
            "--out-dir", str(backup_dir),
            "--verify",
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    from pathlib import Path

    archives = sorted(backup_dir.glob("*.dump"))
    assert len(archives) == 1
    assert Path(f"{archives[0]}.sha256").exists()

    restored_db = f"{pg_database}_restored"
    rto_out = tmp_path / "rto.json"
    try:
        subprocess.run(
            [
                str(repo_root / "scripts" / "aicc_pg_restore.sh"),
                "--archive", str(archives[0]),
                "--target-db", restored_db,
                "--measure-out", str(rto_out),
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

        # The RTO claim this drill exists to back up is only as good as the
        # artifact it leaves behind — assert the script actually wrote one.
        import json

        measurement = json.loads(rto_out.read_text())
        assert measurement["target_db"] == restored_db
        assert measurement["tables_restored"] == len(roles.ALL_TABLES) + len(roles.ALL_VIEWS)
        assert isinstance(measurement["elapsed_seconds"], int)
        assert measurement["elapsed_seconds"] >= 0

        from psycopg.conninfo import make_conninfo

        with psycopg.connect(make_conninfo(**dict(params, dbname=restored_db))) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state, project FROM run WHERE id = %s", (run_id,))
                assert cur.fetchone() == ("running", "p")
                cur.execute("SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_type = 'BASE TABLE'")
                assert cur.fetchone()[0] == len(roles.ALL_TABLES)
                cur.execute("SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_type = 'VIEW'")
                assert cur.fetchone()[0] == len(roles.ALL_VIEWS)
    finally:
        # The restored database is created by the script, so the `pg_database`
        # fixture does not know to drop it.
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        with psycopg.connect(
            make_conninfo(**dict(params, dbname="postgres")), autocommit=True
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(restored_db)
                    )
                )


def test_restore_refuses_to_overwrite_the_live_database(tmp_path):
    """Guard rail, checked without touching a server."""
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    result = subprocess.run(
        [
            str(_repo_root() / "scripts" / "aicc_pg_restore.sh"),
            "--archive", str(archive),
            "--target-db", "aicc_live",
        ],
        env={
            **os.environ,
            "AICC_PG_HOST": "127.0.0.1",
            "AICC_PG_DB": "aicc_live",
            "AICC_PG_USER": "aicc_migrator",
            "AICC_PG_PASSWORD": "irrelevant-for-this-check",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert "refusing to restore over the live database" in result.stderr


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def test_concurrent_upgrades_are_serialized(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The advisory lock's whole reason to exist, exercised concurrently.

    Two processes starting at once is the normal case for a rolling deploy.
    Without the lock both read "0001 not applied" and both run the DDL; the
    loser fails mid-migration. With it, exactly one applies and the other finds
    the work already done.
    """
    import threading

    roles.apply_bootstrap(admin_conn)
    migrator_dsn = _as_role(test_dsn, "aicc_migrator", role_passwords)
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def run(index: int) -> None:
        try:
            with psycopg.connect(migrator_dsn, autocommit=True) as conn:
                barrier.wait(timeout=30)
                results[index] = migrations.upgrade(conn)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted below
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(isinstance(r, Exception) for r in results), results
    # Exactly one run applied the migration; the other found nothing to do.
    assert sorted([results[0], results[1]], key=len) == [(), ALL_VERSIONS]

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migration")
        assert cur.fetchone()[0] == len(ALL_VERSIONS)


def test_new_tables_are_unreachable_until_granted(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Default privileges must not hand a future migration's table to anyone.

    This is the property `ALTER DEFAULT PRIVILEGES ... FOR ROLE aicc_migrator`
    is there to guarantee; asserting on the rendered SQL alone would not catch
    getting the `FOR ROLE` clause wrong.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE future_table (id text PRIMARY KEY)")

    for role in ("aicc_app", "aicc_worker"):
        with psycopg.connect(_as_role(test_dsn, role, role_passwords)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM future_table")


def test_public_execute_on_new_functions_is_revoked(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """PUBLIC gets EXECUTE on new functions by default — the worker's way out.

    A later migration adding a SECURITY DEFINER helper over a governance table
    would otherwise be callable by `aicc_worker` through PUBLIC, reaching
    exactly the data the matrix excludes.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION leak_proposals() RETURNS bigint "
                "LANGUAGE sql SECURITY DEFINER AS $$ SELECT count(*) FROM proposal $$"
            )
        # Exactly what `python -m command_center.db upgrade` does after applying
        # a migration; the revocation is not a one-off at bootstrap.
        roles.apply_table_grants(conn)

    with psycopg.connect(_as_role(test_dsn, "aicc_worker", role_passwords)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT leak_proposals()")


def test_migration_runner_rejects_a_non_autocommit_connection(psycopg, test_dsn):
    """Per-migration atomicity and lock release both depend on autocommit."""
    with psycopg.connect(test_dsn) as conn:  # psycopg's default: autocommit off
        with pytest.raises(migrations.MigrationError, match="autocommit"):
            migrations.upgrade(conn)
        with pytest.raises(migrations.MigrationError, match="autocommit"):
            migrations.downgrade(conn, target=0)


def test_database_ahead_of_this_build_is_reported(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """An old deploy against a newer schema is a rollback, not "up to date"."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migration (version, slug, checksum) "
            "VALUES (99, 'from_the_future', 'x')"
        )

    with psycopg.connect(
        _as_role(test_dsn, "aicc_migrator", role_passwords), autocommit=True
    ) as conn:
        with pytest.raises(migrations.MigrationError, match=r"\[99\]"):
            migrations.upgrade(conn)


def test_readyz_is_200_against_a_real_database(
    admin_conn, psycopg, test_dsn, role_passwords, monkeypatch
):
    """End to end, with no fakes: app startup opens the pool and /readyz passes.

    The unit tests for `/readyz` monkeypatch `pool.connection`, which is exactly
    why they stayed green while `create_app()` had no lifespan hook and the pool
    was therefore never opened in a served process — the probe would have
    returned 503 forever in production. This test would have caught it.
    """
    from fastapi.testclient import TestClient
    from psycopg.conninfo import conninfo_to_dict

    from command_center.db import pool
    from command_center.webapi.app import create_app

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    params = conninfo_to_dict(_as_role(test_dsn, "aicc_app", role_passwords))

    monkeypatch.setenv("AICC_PG_HOST", params.get("host", "127.0.0.1"))
    monkeypatch.setenv("AICC_PG_PORT", str(params.get("port", 5432)))
    monkeypatch.setenv("AICC_PG_DB", params["dbname"])
    monkeypatch.setenv("AICC_PG_USER", params["user"])
    monkeypatch.setenv("AICC_PG_PASSWORD", params["password"])
    monkeypatch.setenv("AICC_PG_SSLMODE", "prefer")  # loopback, per config policy

    pool.close_pool()
    try:
        # The context manager is what runs the lifespan hook.
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "ok"
            assert body["checks"]["schema_version"] == len(migrations.discover())
            assert body["checks"]["database"] == "ok"

            assert client.get("/healthz").status_code == 200
    finally:
        pool.close_pool()
