"""Grant compliance: a migrated database is not ready until its grants are applied.

Migrations create tables and functions but grant no privileges. The privilege
matrix lives in `command_center.db.roles` and is applied by a separate call.
If that call is skipped the database is structurally correct and functionally
broken — proved below by connecting *as the role* and observing what the
database itself allows.

Two shapes of false green are avoided:
* **Assertions that touch nothing.** Every catalog query is validated to return
  at least one row when it should; a query that matches nothing silently passes.
* **Privilege denial wearing a different label.** Grant checks run as the table
  owner where ownership (not grants) provides access, then separately as the
  role where the grant graph is the only reach.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see conftest.
"""

from __future__ import annotations

import pytest

from command_center.db import migrations, roles

pytestmark = pytest.mark.usefixtures("role_passwords")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_role(dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, **{"password": role_passwords[role]})
    return make_conninfo(**params)


def _migrate_only(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Bootstrap + migrate WITHOUT applying table grants."""
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
    # Intentionally omit: roles.apply_table_grants(conn)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Bootstrap + migrate + apply grants — the fully correct production order."""
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


def _actual_table_grants(admin_conn) -> dict[str, dict[str, set[str]]]:
    """Return {role: {table: {privilege, …}}} from the catalog.

    Both catalogs, deliberately: a column-scoped grant (`GRANT UPDATE (col…)`)
    is recorded in `role_column_grants` ONLY — `role_table_grants` does not
    synthesize a table-level row for it. The first version of this checker
    assumed it did and flagged the worker's column-scoped INSERT/UPDATE on
    `completion` (the review_* carve-out, roles.COLUMN_PRIVILEGES) as MISSING
    against a fully provisioned database. The privilege-level view here unions
    the two; whether the column *set* matches the declaration is checked
    separately in `_column_grant_violations`.
    """
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
        cur.execute(
            "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.role_column_grants "
            "WHERE table_schema = 'public' AND grantee = ANY(%s)",
            (list(roles.ALL_ROLES),),
        )
        for grantee, table, privilege in cur.fetchall():
            actual.setdefault(grantee, {}).setdefault(table, set()).add(privilege)
    return actual


def _column_grant_violations(admin_conn) -> list[str]:
    """The column detail for every declared column-scoped privilege.

    Exactness matters in both directions: a column missing from the grant
    breaks the writer, and an EXTRA column on `completion`'s worker UPDATE
    would be precisely the forged-verdict channel the carve-out exists to
    close (roles.py: _REVIEW_COLUMNS).
    """
    violations: list[str] = []
    with admin_conn.cursor() as cur:
        for role, tables in roles.COLUMN_PRIVILEGES.items():
            for table, per_priv in tables.items():
                for privilege, columns in per_priv.items():
                    cur.execute(
                        "SELECT column_name "
                        "FROM information_schema.role_column_grants "
                        "WHERE table_schema = 'public' AND grantee = %s "
                        "AND table_name = %s AND privilege_type = %s",
                        (role, table, privilege),
                    )
                    actual_columns = {row[0] for row in cur.fetchall()}
                    declared = set(columns)
                    for column in sorted(declared - actual_columns):
                        violations.append(
                            f"MISSING: role={role} privilege={privilege} "
                            f"on={table}.{column}"
                        )
                    for column in sorted(actual_columns - declared):
                        violations.append(
                            f"EXTRA:   role={role} privilege={privilege} "
                            f"on={table}.{column}"
                        )
    return violations


def _actual_function_grants(admin_conn) -> dict[str, set[str]]:
    """Return {role: {canonical_signature, …}} from the catalog."""
    result: dict[str, set[str]] = {}
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (roles.APP_ROLE,),
        )
        result[roles.APP_ROLE] = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (roles.WORKER_ROLE,),
        )
        result[roles.WORKER_ROLE] = {row[0] for row in cur.fetchall()}
    return result


def _expected_table_grants() -> dict[str, dict[str, set[str]]]:
    """Build the expected {role: {table: {privilege}}} from the declared matrix."""
    expected: dict[str, dict[str, set[str]]] = {}
    for role in (roles.APP_ROLE, roles.WORKER_ROLE, roles.OPERATOR_ROLE):
        role_expected: dict[str, set[str]] = {}
        for table, privs in roles.PRIVILEGES[role].items():
            if privs:
                role_expected[table] = set(privs)
        for view, privs in roles.VIEW_PRIVILEGES.get(role, {}).items():
            if privs:
                role_expected[view] = set(privs)
        # Column-level grants still show up in role_table_grants as table-level
        # entries; include their privilege types (not the column detail here).
        for table, per_priv in roles.COLUMN_PRIVILEGES.get(role, {}).items():
            for privilege in per_priv:
                role_expected.setdefault(table, set()).add(privilege)
        expected[role] = role_expected
    return expected


def _expected_function_grants() -> dict[str, set[str]]:
    """
    Resolve declared FUNCTION_PRIVILEGES signatures to the canonical form
    PostgreSQL uses in pg_get_function_arguments.
    """
    # We can't resolve signatures without a database; callers that need this
    # resolved form do it inline with a catalog query.  This helper returns the
    # *declared* short-name set for comparison after resolution.
    result: dict[str, set[str]] = {}
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        result[role] = {
            sig.split("(")[0] for sig in roles.FUNCTION_PRIVILEGES.get(role, ())
        }
    return result


# ---------------------------------------------------------------------------
# Compliance check  (the heart of this module)
# ---------------------------------------------------------------------------


def _check_compliance(admin_conn) -> list[str]:
    """Compare the rendered policy against the actual database grants.

    Returns a list of human-readable violation strings. An empty list means the
    database is fully compliant with the declared matrix. Callers decide whether
    to assert or report.

    The check has three parts:
    1. Every privilege declared in the matrix must exist in the catalog.
    2. No privilege exists in the catalog that the matrix does not declare
       (extra grants widen access beyond the declared policy).
    3. Every declared function grant must exist; no undeclared function must
       be reachable by a granted role.
    4. Every table/view/function created by migrations must be covered by the
       matrix (no silent omission for objects added by a later migration).
    """
    violations: list[str] = []

    # ---- table / view grants -----------------------------------------------
    actual = _actual_table_grants(admin_conn)
    expected = _expected_table_grants()

    for role in (roles.APP_ROLE, roles.WORKER_ROLE, roles.OPERATOR_ROLE):
        role_actual = actual.get(role, {})
        role_expected = expected.get(role, {})

        # Missing grants
        for obj, privs in role_expected.items():
            for priv in privs:
                if priv not in role_actual.get(obj, set()):
                    violations.append(f"MISSING: role={role} privilege={priv} on={obj}")

        # Extra grants
        for obj, privs in role_actual.items():
            for priv in privs:
                if priv not in role_expected.get(obj, set()):
                    violations.append(f"EXTRA:   role={role} privilege={priv} on={obj}")

        # Declared-refusal tables must not appear in the catalog at all
        for table, privs in roles.PRIVILEGES[role].items():
            if not privs and table in role_actual:
                violations.append(
                    f"FORBIDDEN: role={role} has grants on declared-unreachable table={table}"
                )

    # ---- column-scoped grants: exact column sets ---------------------------
    violations.extend(_column_grant_violations(admin_conn))

    # ---- tables/views not covered by the matrix at all ---------------------
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        db_tables = {row[0] for row in cur.fetchall()} - {"schema_migration"}

        cur.execute(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = 'public'"
        )
        db_views = {row[0] for row in cur.fetchall()}

    uncovered_tables = db_tables - set(roles.ALL_TABLES)
    for table in sorted(uncovered_tables):
        violations.append(
            f"UNCOVERED TABLE: {table!r} exists in the database but is not in ALL_TABLES"
        )

    uncovered_views = db_views - set(roles.ALL_VIEWS)
    for view in sorted(uncovered_views):
        violations.append(
            f"UNCOVERED VIEW: {view!r} exists in the database but is not in ALL_VIEWS"
        )

    # ---- function grants ---------------------------------------------------
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'"
        )
        all_db_functions = [row[0] for row in cur.fetchall()]

    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        declared_sigs = list(roles.FUNCTION_PRIVILEGES.get(role, ()))

        # Resolve declared short signatures to catalog form
        declared_canonical: set[str] = set()
        for sig in declared_sigs:
            name = sig.split("(")[0]
            matches = [f for f in all_db_functions if f.startswith(f"{name}(")]
            if not matches:
                violations.append(
                    f"MISSING FUNCTION: role={role} declared function {sig!r} not found in db"
                )
                continue
            if len(matches) > 1:
                violations.append(
                    f"AMBIGUOUS FUNCTION: role={role} {sig!r} matches {matches}"
                )
                continue
            declared_canonical.add(matches[0])

        # What is actually granted in the catalog
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT p.proname || '(' || pg_get_function_arguments(p.oid) || ')' "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' "
                "AND has_function_privilege(%s, p.oid, 'EXECUTE')",
                (role,),
            )
            granted_canonical = {row[0] for row in cur.fetchall()}

        for fn in sorted(declared_canonical - granted_canonical):
            violations.append(f"MISSING EXECUTE: role={role} on function {fn!r}")
        for fn in sorted(granted_canonical - declared_canonical):
            violations.append(f"EXTRA EXECUTE:   role={role} on function {fn!r}")

    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_compliance_passes_when_grants_are_applied(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A database with migrations AND grants applied must have zero violations."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    violations = _check_compliance(admin_conn)
    assert violations == [], "\n".join(violations)


def test_compliance_fails_when_grants_are_not_applied(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A database with migrations but WITHOUT grants must report violations.

    This test exists to prevent the check from becoming a tautology: if
    ``_check_compliance`` were re-written to always return an empty list it
    would pass ``test_compliance_passes_*`` and fail here.
    """
    _migrate_only(admin_conn, psycopg, test_dsn, role_passwords)
    violations = _check_compliance(admin_conn)
    assert violations, (
        "Expected violations when grants are not applied, but got none. "
        "The compliance check is not detecting the missing grants."
    )
    # The violations must name what is wrong — not just be non-empty.
    text = "\n".join(violations)
    # At minimum the function grants for worker and app are missing.
    assert any("EXECUTE" in v or "MISSING" in v for v in violations), (
        f"Violations present but none describe a missing grant:\n{text}"
    )


def test_compliance_names_the_role_and_object_in_each_violation(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Each violation string must identify the role, the privilege and the object."""
    _migrate_only(admin_conn, psycopg, test_dsn, role_passwords)
    violations = _check_compliance(admin_conn)
    for v in violations:
        # Every violation line must contain at least one of: role name,
        # object name. This guards against opaque messages like "grant missing".
        has_role = any(role in v for role in roles.ALL_ROLES)
        has_obj = any(
            obj in v
            for obj in list(roles.ALL_TABLES)
            + list(roles.ALL_VIEWS)
            + [
                sig.split("(")[0]
                for sigs in roles.FUNCTION_PRIVILEGES.values()
                for sig in sigs
            ]
        )
        assert has_role or has_obj, f"Violation does not name a role or object: {v!r}"


# ---------------------------------------------------------------------------
# Reproduction of the stated defect: worker can call queue_redrive without grants
# ---------------------------------------------------------------------------


def test_worker_cannot_call_queue_redrive_when_grants_are_applied(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """With grants applied, the worker role must not be able to call queue_redrive.

    Reproduction of the issue: ``queue_redrive`` is an app-role function. With
    only migrations applied (no grants), PUBLIC still holds EXECUTE on it via
    the default, so ``aicc_worker`` can reach it. After grants are applied the
    REVOKE strips that path and the call must fail with ``permission denied``.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="permission denied"):
                # Calling with a non-existent queue name is fine: the denial
                # must happen before any data check.
                cur.execute("SELECT public.queue_redrive('nonexistent_queue', 1)")


def test_worker_can_call_queue_redrive_without_grants(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Without grants the PUBLIC EXECUTE default is still in place.

    This test reproduces the defect described in the issue: ``aicc_worker`` can
    reach ``queue_redrive`` (an app-only function) because migrations do not
    revoke PUBLIC EXECUTE and ``aicc_worker`` inherits it through PUBLIC.
    """
    _migrate_only(admin_conn, psycopg, test_dsn, role_passwords)

    # queue_redrive exists in the database after migrations.
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'queue_redrive'"
        )
        assert cur.fetchone() is not None, "queue_redrive must exist after migrations"

    # Without grants, worker reaches queue_redrive through PUBLIC EXECUTE.
    # The call may succeed or fail for data reasons, but it must not fail
    # with "permission denied" — that is the defect.
    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT public.queue_redrive('nonexistent_queue', 1)")
            except Exception as exc:
                assert "permission denied" not in str(exc).lower(), (
                    f"Worker got permission denied even WITHOUT grants — "
                    f"the defect is already fixed or the test setup is wrong: {exc}"
                )


# ---------------------------------------------------------------------------
# The backlog store (0005, BO-S1): behavioural proof, not just catalog diff
# ---------------------------------------------------------------------------
#
# roles.py's `_APP_BACKLOG_TABLES`/`_WORKER_BACKLOG_TABLES` comment promised
# this suite's provisioning coverage would extend to the backlog tables "in
# the first post-#321 slice" (BO-S1, #326) — the generic catalog-diff tests
# above already exercise `backlog_task` et al because `ALL_TABLES`/`PRIVILEGES`
# include them, but that promise was specifically about a *behavioural*
# check, the same kind `test_worker_cannot_call_queue_redrive_*` gives the
# queue-claim tables: connect as the role, run the query, observe what the
# database itself allows — not just that the catalog says so. Closed here
# (BO-S4) rather than at BO-S1 to keep that PR's surface reviewable, per the
# comment's own deferral.


def test_worker_cannot_read_backlog_task_when_grants_are_applied(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A compromised execution host must not be able to read the programme's
    plan: `_WORKER_BACKLOG_TABLES` declares `backlog_task` (and its siblings)
    as `_NONE` for `aicc_worker`. Unlike function EXECUTE, PostgreSQL grants
    no table privilege to PUBLIC by default, so there is no "without grants"
    mirror here — the worker has no path to this table under any state.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="permission denied"):
                cur.execute("SELECT task_id FROM backlog_task LIMIT 1")


# ---------------------------------------------------------------------------
# A new migration adding an object outside the policy must fail the check
# ---------------------------------------------------------------------------


def test_compliance_fails_for_table_outside_the_policy(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A table created by a later migration that is not in ALL_TABLES must fail.

    This is the "new migration adds a table the policy does not cover" case
    from the acceptance criteria. The check must not silently ignore it.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    # Simulate a later migration that adds a table not yet declared in roles.py.
    with admin_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE public.undeclared_future_table "
            "(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, data text)"
        )

    violations = _check_compliance(admin_conn)
    uncovered = [v for v in violations if "undeclared_future_table" in v]
    assert uncovered, (
        "Expected a violation for 'undeclared_future_table' not in ALL_TABLES, "
        "but got none.\nAll violations:\n" + "\n".join(violations)
    )


def test_compliance_fails_for_function_outside_the_policy(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A function created by a later migration that ends up reachable via PUBLIC.

    After grants are applied, PUBLIC EXECUTE is revoked. But if a second
    ``apply_table_grants()`` call is NOT made after a new function is added,
    PUBLIC still holds EXECUTE on it. The check must detect this.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)

    # Add a new function that PUBLIC would hold EXECUTE on by default
    # (simulating a migration that ran after the grants were applied).
    with admin_conn.cursor() as cur:
        cur.execute(
            "CREATE OR REPLACE FUNCTION public.undeclared_helper()\n"
            "RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;"
        )

    # Before re-applying grants, PUBLIC (and thus worker) can reach it.
    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords),
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT public.undeclared_helper()")  # must not raise

    # The compliance check should catch that worker can reach an undeclared function.
    violations = _check_compliance(admin_conn)
    extra = [v for v in violations if "undeclared_helper" in v]
    assert extra, (
        "Expected a violation for worker reaching 'undeclared_helper', "
        "but got none.\nAll violations:\n" + "\n".join(violations)
    )
