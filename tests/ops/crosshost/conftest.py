"""Fixtures for the cross-host queue-claim proof (SRV-05 properties 12-13).

Two guarantees the deployed worker fleet depends on are cross-host claims by
construction and cannot be exercised honestly from one machine: that a lease
abandoned by a `SIGKILL`ed worker on one host is picked up by exactly one
other host, and that a worker cut off from the queue cannot out-argue the
queue's own lease-expiry decision. See
`docs/operations/SRV05_LINUX_SYSTEMD_VERIFICATION.md`.

Proving them needs three things a laptop or a CI runner does not have: a
second real host reachable over SSH, a real `systemd --user` session on that
host, and a Postgres server both hosts can reach (the shared arbiter the
protocol depends on). All three are supplied out of band, the same way
`tests/db/conftest.py` supplies a Postgres admin DSN for the single-host
queue-claim suite -- when any is missing, every test in this package skips,
so `pytest -q` stays green everywhere this specific pair of real hosts is not
the thing running it.
"""

from __future__ import annotations

import os
import secrets
import sys

import pytest

from tests.ops.crosshost._driver import (
    override_dsn,
    ssh_reachable_with_live_systemd_user,
)

ADMIN_DSN_ENV = "AICC_CROSSHOST_PG_ADMIN_DSN"
SSH_TARGET_ENV = "AICC_CROSSHOST_SSH_TARGET"


@pytest.fixture(scope="session")
def remote_host() -> str:
    target = os.environ.get(SSH_TARGET_ENV, "").strip()
    if not target:
        pytest.skip(f"{SSH_TARGET_ENV} is not set; cross-host queue-claim proof skipped")
    if not sys.platform.startswith("linux"):
        pytest.skip("cross-host proof assumes a Linux systemd --user session on both ends")
    if not ssh_reachable_with_live_systemd_user(target):
        pytest.skip(
            f"{target!r} is not reachable over passwordless SSH with a live "
            "systemd --user session (ssh ... systemd-run --user --wait -- "
            "/bin/true must succeed); this proves the mechanism against two "
            "real kernels rather than mocking the second host, so there is "
            "no fallback off that"
        )
    return target


@pytest.fixture(scope="session")
def admin_dsn(remote_host) -> str:  # noqa: ARG001 - depend on remote_host so SSH is checked first
    dsn = os.environ.get(ADMIN_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{ADMIN_DSN_ENV} is not set; cross-host queue-claim proof skipped")
    return dsn


@pytest.fixture(scope="session")
def psycopg(admin_dsn):  # noqa: ARG001 - depend on admin_dsn so we skip before importing
    return pytest.importorskip("psycopg")


@pytest.fixture
def pg_database(admin_dsn, psycopg) -> str:
    """A throwaway database, reachable by both hosts, migrated to the
    queue-claim schema for one test and dropped afterwards."""
    from psycopg import sql

    from command_center.db import migrations

    name = f"aicc_crosshost_{secrets.token_hex(8)}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(override_dsn(admin_dsn, dbname=name)) as conn:
            migrations.upgrade(conn)
            conn.commit()
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
def queue_dsn(admin_dsn, pg_database) -> str:
    """The admin DSN re-pointed at this test's throwaway database -- usable
    as-is by `psql` on either host, since both reach the same server."""
    return override_dsn(admin_dsn, dbname=pg_database)
