"""Fixtures for the PostgreSQL foundation tests.

The integration tests here need a real PostgreSQL server: the properties under
test — transactional DDL, advisory locks, `GRANT`/`REVOKE` enforcement, and a
pg_dump/pg_restore round trip — have no meaningful in-memory substitute, and a
mocked driver would only assert that the test's own fake behaves as the test
expects.

The server is supplied out of band via `AICC_TEST_PG_ADMIN_DSN` (a superuser
connection string). When it is unset every test in this package skips, so the
default `pytest -q` gate on a laptop without Docker stays green; CI provides a
PostgreSQL service container and therefore actually runs them.

Each test gets its own freshly created database. Sharing one database across
tests would let a migration test's `DROP TABLE` race a privilege test's
`SELECT`, and this suite runs under `pytest -n auto`.
"""

from __future__ import annotations

import hashlib
import os
import secrets

import pytest

from command_center.db import roles as _roles

ADMIN_DSN_ENV = "AICC_TEST_PG_ADMIN_DSN"

# Passwords for the login-enabled variants of the product roles. Derived from
# a per-RUN seed rather than plain `secrets.token_urlsafe()`: the roles are
# cluster objects (one `aicc_migrator` for the whole cluster), but this dict
# is a module-level constant computed once per xdist WORKER PROCESS, and each
# worker independently calling `secrets.token_urlsafe()` produced a different
# value -- the `role_passwords` fixture (session-scoped, so once per worker)
# then had every worker overwrite the SAME cluster-wide role's password with
# its own random value, leaving every worker except whichever ran last
# holding a stale password its own connections then failed to authenticate
# with (`password authentication failed for user aicc_migrator`, live in CI
# 2026-08-21, only visible once the same flake's other two bugs were fixed
# and stopped masking it). `PYTEST_XDIST_TESTRUNUID` is broadcast by the
# xdist controller to every worker of one test session, so deriving from it
# gives every worker the same passwords within a run while still varying
# run to run -- nothing in the suite works against a fixed credential that
# could accidentally be reused by a real deployment, which is the property
# the original random-per-session design was for.
#
# Derived from `ALL_ROLES` rather than listed: a role added to the inventory
# without a password here fails at fixture setup with a `KeyError`, which reads
# as a broken test rather than as the missing provisioning step it is.
_RUN_SEED = os.environ.get("PYTEST_XDIST_TESTRUNUID") or secrets.token_urlsafe(24)
_ROLE_PASSWORDS = {
    role: hashlib.sha256(f"{_RUN_SEED}:{role}".encode()).hexdigest()
    for role in _roles.ALL_ROLES
}


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    dsn = os.environ.get(ADMIN_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{ADMIN_DSN_ENV} is not set; PostgreSQL integration tests skipped")
    return dsn


@pytest.fixture(scope="session")
def psycopg(admin_dsn):  # noqa: ARG001 — depend on admin_dsn so we skip before importing
    return pytest.importorskip("psycopg")


@pytest.fixture(scope="session")
def role_passwords(admin_dsn, psycopg) -> dict[str, str]:
    """Give every product role LOGIN and a password, cluster-wide.

    Roles are cluster objects rather than per-database ones, so this runs once
    and the per-test databases reuse them. Production does the same: the roles
    are created by `render_grants()`, and the operator attaches credentials.

    "Session-scoped" is per xdist WORKER PROCESS, not cluster-wide, so N
    workers each run this concurrently against the same cluster roles.
    `render_role_creation()` guards its own CREATE with an advisory lock, but
    that lock releases as soon as that one statement's implicit transaction
    ends -- it does not cover the `ALTER ROLE ... PASSWORD` right after,
    which two workers can then run concurrently on the very same catalog
    row and get `tuple concurrently updated` from (found live, 2026-08-21,
    after fixing the CREATE-side race and a missing-dependency bug in the
    same flake: VOYN-W0-AICC-MIGRATOR-PASSWORD-FLAKE turned out to be three
    independent bugs under one error signature). Holding the lock for this
    fixture's whole body, in one explicit transaction, serializes CREATE and
    ALTER together across workers instead of only the CREATE half.
    """
    from psycopg import sql

    from command_center.db import roles as roles_module

    with psycopg.connect(admin_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7823649102);")
            for role in roles_module.ALL_ROLES:
                cur.execute(roles_module.render_role_creation(role))
                cur.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(_ROLE_PASSWORDS[role])
                    )
                )
    return dict(_ROLE_PASSWORDS)


@pytest.fixture
def pg_database(admin_dsn, psycopg) -> str:
    """Create an empty database for one test and drop it afterwards."""
    name = f"aicc_test_{secrets.token_hex(8)}"
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )


@pytest.fixture
def test_dsn(admin_dsn, pg_database) -> str:
    """The admin DSN pointed at this test's database."""
    return _override(admin_dsn, dbname=pg_database)


@pytest.fixture
def admin_conn(psycopg, test_dsn):
    """Superuser connection to the per-test database."""
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def connect_as(psycopg, test_dsn, role_passwords):
    """Factory opening a connection to the test database as a product role."""

    def _connect(role: str):
        return psycopg.connect(
            _override(test_dsn, user=role, password=role_passwords[role]),
            autocommit=True,
        )

    return _connect


def _override(dsn: str, **overrides: str) -> str:
    """Return `dsn` with the given libpq parameters replaced.

    Uses psycopg's own conninfo parser so both URL (`postgresql://…`) and
    keyword/value (`host=… dbname=…`) forms work — CI and a local Docker setup
    tend to disagree about which one they hand out.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(overrides)
    return make_conninfo(**params)


@pytest.fixture
def pg_connection_factory(admin_conn, psycopg, test_dsn, role_passwords):  # noqa: ARG001
    """A `connection()`-shaped factory over a migrated throwaway database.

    Shaped like `command_center.db.pool.connection` so the store under test
    talks to the real seam rather than to a fixture-specific interface — a
    store proved against a bespoke connection object is not proved against the
    one it runs on.

    Depends on `role_passwords` for its side effect, not its value: the
    migrations this fixture applies GRANT to `aicc_app`/`aicc_worker` (e.g.
    `backlog_triage()`), so those cluster-level roles must exist before
    `migrations.upgrade()` runs. Nothing enforced that ordering before this
    fix -- if the xdist worker running this test hadn't already resolved
    `role_passwords` via some other fixture first, the GRANT hit a role
    that plain didn't exist yet, independent of any concurrency race
    (VOYN-W0-AICC-MIGRATOR-PASSWORD-FLAKE: this, not the CREATE ROLE race
    the advisory lock guards, is why it kept reproducing even after that
    lock was correctly in place).
    """
    from contextlib import contextmanager

    from command_center.db import migrations

    migrations.upgrade(admin_conn)

    @contextmanager
    def factory():
        yield admin_conn

    return factory
