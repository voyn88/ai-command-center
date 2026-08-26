"""`0004_run_finalized_at` against a real PostgreSQL server.

VOYN-W0-AICC-SRV-09-FINALIZED-AT. Skipped unless `AICC_TEST_PG_ADMIN_DSN` is set
(see conftest).

The migration is four lines of DDL, and the reason it gets its own suite is the
partial index. A column that survives a downgrade is a visible mistake; an index
that survives one is not, because nothing selects it by name afterwards — the
schema simply carries a stray object that the next upgrade cannot recreate. So
the assertion is the same one 0002 makes about itself: the schema after a full
round trip is indistinguishable from the schema before, twice over.
"""

from __future__ import annotations

import pytest

from command_center.db import migrations, roles

pytestmark = pytest.mark.usefixtures("role_passwords")

#: This migration, named rather than taken as `discover()[-1]` — which stops
#: meaning anything the moment a fourth migration lands, and does so silently.
VERSION = 4


def _as_role(dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=role_passwords[role])
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Bootstrap as superuser, migrate as the migrator — the production order."""
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords), autocommit=True
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


def _run_shape(admin_conn) -> dict:
    """Everything 0003 touches, and nothing it does not.

    Scoped to `run` and its indexes on purpose: a whole-schema snapshot would
    also move whenever an unrelated migration lands, which turns a reversibility
    failure here into a diff nobody can read.
    """
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'run' ORDER BY ordinal_position"
        )
        columns = cur.fetchall()
        # `indexdef` rather than the index name: it carries the predicate, so a
        # partial index degraded into a full one on the same column is a
        # difference here rather than a silent equivalence.
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'run' ORDER BY indexname"
        )
        indexes = cur.fetchall()
    return {"columns": columns, "indexes": indexes}


def test_the_migration_is_reversible_without_residue(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """up -> down -> up -> down, with the schema equal at every matching point."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    migrator_dsn = _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords)
    above = tuple(m.version for m in migrations.discover() if m.version > VERSION)

    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        assert migrations.downgrade(conn, target=VERSION - 1) == (*above[::-1], VERSION)
        before = _run_shape(admin_conn)

        assert migrations.upgrade(conn, target=VERSION) == (VERSION,)
        after = _run_shape(admin_conn)
        assert after != before, "the snapshot notices nothing; it would pass on anything"

        assert migrations.downgrade(conn, target=VERSION - 1) == (VERSION,)
        assert _run_shape(admin_conn) == before

        assert migrations.upgrade(conn, target=VERSION) == (VERSION,)
        assert _run_shape(admin_conn) == after

        assert migrations.downgrade(conn, target=VERSION - 1) == (VERSION,)
        assert _run_shape(admin_conn) == before


def test_the_column_is_nullable_and_the_index_is_partial(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The two properties the rest of the design rests on.

    Nullable, because every run that already exists finalized under a supervisor
    that recorded nothing, and a `NOT NULL DEFAULT now()` would manufacture
    evidence that their reports were durable.

    Partial, because the predicate is only ever asked about rows where the
    marker is empty. A full index on `(state)` would answer it by carrying every
    run that ever finished; this one holds an entry only while a run is
    unfinalized, so a successful finalization removes its own row from it.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'run' "
            "AND column_name = 'finalized_at'"
        )
        assert cur.fetchone() == ("timestamp with time zone", "YES")

        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = 'idx_run_unfinalized'"
        )
        (indexdef,) = cur.fetchone()
    assert "WHERE (finalized_at IS NULL)" in indexdef, indexdef
    assert "(state)" in indexdef, indexdef


def test_the_predicate_uses_the_partial_index(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """An index nothing plans against is an index that is not there.

    Asserted because the cutover's whole reason for a partial index is that the
    question stays cheap on an install with a long run history — and a planner
    that seq-scans `run` instead would make the drain gate slower the longer the
    system has been useful.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        # The planner prefers a sequential scan on an empty table regardless of
        # what indexes exist, so give it a reason to consider one.
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT count(*) FROM run "
            "WHERE finalized_at IS NULL AND state IN ('COMPLETED', 'FAILED', 'CANCELLED')"
        )
        plan = "\n".join(row[0] for row in cur.fetchall())
    assert "idx_run_unfinalized" in plan, plan
