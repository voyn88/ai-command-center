"""The denial audit outlives the denial, in every layer that writes one.

VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS, measured rather than argued: two probes that
differ only in how a refusal is reported. Rows in the audit trail after a
refusal that ``RAISE``s — 0. After a refusal that ``RETURN``s — 1. PostgreSQL
cannot keep a row written by a transaction that then aborts, so a function that
raises its own refusal erases its own record of it. Refusing a one-time
enrolment ticket *is* the theft signal; a signal that rolls itself back is not
a signal.

There are three tables in this schema that hold that record —
``principal_event`` (identity), ``work_event`` (queue) and ``backlog_event``
(backlog) — and this file asserts the property in each of them, because the
defect was found one layer at a time and fixed one layer at a time. The
static gate that keeps NEW functions inside the rule is
``tests/architecture/test_refusal_audit_survives_fitness.py``; the last test
here ties that gate's model of the schema to what PostgreSQL actually deployed,
so the two cannot drift.

Every probe here commits and then reads the row back on a SECOND connection:
"the row is visible to the caller that wrote it" would also be true of a row
that never survives the commit.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.usefixtures("role_passwords")]


@pytest.fixture
def migrated(admin_conn, psycopg, test_dsn, role_passwords):  # noqa: ARG001
    """A migrated database, plus a factory for further connections to it."""
    from command_center.db import migrations

    migrations.upgrade(admin_conn)

    def connect(*, autocommit: bool = True):
        return psycopg.connect(test_dsn, autocommit=autocommit)

    return connect


def _count(connect, sql: str, params: tuple = ()) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


def test_the_two_probes_that_differ_only_in_how_the_refusal_is_reported(
    migrated,
) -> None:
    """The defect, reproduced on demand, so the assertions below can fail.

    A test that cannot fail is not a test: this is the control that shows the
    audit table really does lose the row when the refusal raises, and really
    does keep it when the refusal returns.
    """
    with migrated(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            DO $probe$
            BEGIN
                BEGIN
                    PERFORM _backlog_audit(NULL, 'probe', 'rejected', 'raised');
                    RAISE EXCEPTION 'the refusal aborts the record of itself';
                EXCEPTION WHEN others THEN
                    NULL;
                END;
                PERFORM _backlog_audit(NULL, 'probe', 'rejected', 'returned');
            END
            $probe$;
            """
        )
        conn.commit()

    assert _count(
        migrated,
        "SELECT count(*) FROM backlog_event WHERE event = 'probe' AND reason = 'raised'",
    ) == 0
    assert _count(
        migrated,
        "SELECT count(*) FROM backlog_event WHERE event = 'probe' AND reason = 'returned'",
    ) == 1


# ---------------------------------------------------------------------------
# Layer 1 — identity (`principal_event`)
# ---------------------------------------------------------------------------


def test_a_refused_enrolment_ticket_is_still_recorded_after_the_commit(
    migrated,
) -> None:
    """A ticket nobody minted is the strongest signal this schema can emit: a
    host presenting one either stole it or invented it. The refusal returns a
    reason, so the transaction that made it is free to commit."""
    with migrated(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT refuse_reason FROM enroll_redeem_ticket(%s, %s, %s, %s::jsonb)",
            (
                "no-such-ticket",
                "0" * 64,
                "scram-verifier-placeholder",
                '{"machine_id": "probe", "os": "linux", "arch": "x86_64"}',
            ),
        )
        assert cur.fetchone()[0] == "unknown_ticket"
        # The connection is still usable -- nothing was aborted.
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        conn.commit()

    assert _count(
        migrated,
        "SELECT count(*) FROM principal_event "
        "WHERE event_type = 'enroll' AND outcome = 'rejected' AND reason = %s",
        ("unknown_ticket",),
    ) == 1


def test_a_refused_identity_assert_is_still_recorded_after_the_commit(
    migrated,
) -> None:
    """`identity_assert` is the statement-level gate every privileged operation
    calls. It returns a verdict precisely so the caller can commit the denial;
    a raising wrapper over it would erase this row at every call site."""
    with migrated(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT ok, reason FROM identity_assert(%s)", ("no-such-credential",))
        assert cur.fetchone() == (False, "unknown_credential")
        conn.commit()

    assert _count(
        migrated,
        "SELECT count(*) FROM principal_event "
        "WHERE event_type = 'assert' AND outcome = 'rejected' AND reason = %s",
        ("unknown_credential",),
    ) == 1


# ---------------------------------------------------------------------------
# Layer 2 — queue (`work_event`)
# ---------------------------------------------------------------------------


def test_a_refused_queue_completion_is_still_recorded_after_the_commit(
    migrated,
) -> None:
    """The stale-owner refusal: a worker whose visibility timeout elapsed and
    whose item was re-claimed. Losing that row loses the evidence that a
    superseded worker was still writing."""
    with migrated(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
            ("wka_does_not_exist", "not-the-claim-token", "{}"),
        )
        ok, reason = cur.fetchone()
        assert not ok and reason == "unknown_attempt"
        conn.commit()

    assert _count(
        migrated,
        "SELECT count(*) FROM work_event "
        "WHERE event = 'complete' AND outcome = 'rejected' AND reason = %s",
        ("unknown_attempt",),
    ) == 1


# ---------------------------------------------------------------------------
# Layer 3 — backlog (`backlog_event`)
# ---------------------------------------------------------------------------


def test_a_refused_dispatch_is_still_recorded_after_the_commit(migrated) -> None:
    with migrated(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ok, reason FROM backlog_dispatch(%s, %s, 60, 4, %s::jsonb, 3)",
            ("VOYN-W0-NO-SUCH-TASK", "planner-probe", "{}"),
        )
        ok, reason = cur.fetchone()
        assert not ok and reason == "unknown_task"
        conn.commit()

    assert _count(
        migrated,
        "SELECT count(*) FROM backlog_event "
        "WHERE event = 'dispatch' AND outcome = 'rejected' AND reason = %s",
        ("unknown_task",),
    ) == 1


# ---------------------------------------------------------------------------
# The static gate's model of the schema == what PostgreSQL deployed
# ---------------------------------------------------------------------------


def test_the_static_function_model_matches_the_deployed_catalog(migrated) -> None:
    """The architecture gate reads the migration files rather than a database,
    so it runs in every CI job instead of only the ones with PostgreSQL. That
    is only safe while its parse of those files equals what the server made of
    them -- an overload it failed to split, or a definition it attributed to
    the wrong signature, would leave the gate reading a schema nobody runs.
    """
    from tests.architecture import refusal_audit

    model = refusal_audit.build_model()
    parsed = {
        f"{name}({', '.join(args)})" for name, args in model.functions
    }

    with migrated() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind = 'f'"
        )
        deployed = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )
        deployed_tables = {row[0] for row in cur.fetchall()}

    assert parsed == deployed
    # `schema_migration` is created by the migration RUNNER, not by a migration.
    assert model.tables == deployed_tables - {"schema_migration"}
    assert model.audit_tables == {
        table for table in deployed_tables if table.endswith("_event")
    }
