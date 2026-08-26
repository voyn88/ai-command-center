"""The class guard: nothing that audits a refusal may then raise.

VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS. The episode was one raising wrapper; the
CLASS is that an exception aborts the transaction the audit row lives in, so
*any* caller that turns a returned verdict back into an exception deletes the
refusal record its callee just wrote -- and every call site of an auditing
function is a place it can reappear. 0002 measured the shape (0 audit rows
after ``RAISE``, 1 after ``RETURN``) and 0003 restated it for identity; both
statements were prose that no gate enforced across layers, which is why the
same defect was still live in ``backlog_dispatch`` (0006) and
``backlog_ingest_results`` (0009) when 0010 removed it.

This is that gate, and it is computed rather than listed. It reads the
DEPLOYED schema -- every function as PostgreSQL actually holds it after the
full migration run, so a definition superseded by a later ``DROP``/``CREATE``
(0007's ``backlog_ingest_results`` by 0009 by 0010, 0006's
``backlog_dispatch`` by 0010) is judged only in its final form -- and takes
the transitive closure of "can write an audit row" over the call graph. Every
function in that closure must refuse by returning.

The behavioural half lives with each layer, one test per layer where a denial
is written:

* queue    -- ``test_queue_claim.py::test_a_refusal_is_audited_because_it_
  returned_rather_than_raised``
* identity -- ``test_enrollment.py::test_identity_refusal_audit_survives_
  every_public_call_layer``
* backlog  -- ``test_backlog_planner.py::test_a_wedged_gate_row_is_refused_
  per_row_and_keeps_its_audit`` and its dispatch-layer neighbours.
"""

from __future__ import annotations

import re

import pytest

pytestmark = [pytest.mark.serial]

#: The three functions that INSERT an audit row. Seeds of the closure; every
#: other member is derived from the call graph, so a fourth audit surface
#: added by a future migration is covered the moment something calls it.
AUDIT_WRITERS = frozenset({"_backlog_audit", "_queue_audit", "_principal_audit"})

#: `RAISE` with no level defaults to EXCEPTION, and a bare `RAISE;` re-raises,
#: so matching the word `EXCEPTION` would miss two thirds of the ways to abort
#: a transaction. Everything that only writes to the server log is fine: those
#: survive the rollback precisely because they are not part of it.
_RAISES = re.compile(
    r"\bRAISE\b(?!\s+(?:LOG|NOTICE|WARNING|INFO|DEBUG)\b)", re.IGNORECASE
)
_COMMENT = re.compile(r"--[^\n]*")
_LITERAL = re.compile(r"'(?:[^']|'')*'", re.DOTALL)


def _code(body: str) -> str:
    """The body with comments and string literals removed.

    Both routinely contain the word RAISE -- 0002 and 0010 explain in prose
    exactly what a RAISE there would have discarded -- and a guard that
    flagged prose would be turned off within a week.
    """
    return _LITERAL.sub("''", _COMMENT.sub("", body))


def _functions(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind = 'f'"
        )
        return {name: _code(src) for name, src in cur.fetchall()}


def _mentions(body: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\s*\(", body) is not None


def _audit_writing_closure(bodies: dict[str, str]) -> set[str]:
    """Every function that can reach an audit INSERT, directly or through
    another function.

    A fixpoint rather than one hop: `backlog_dispatch` never called
    `_backlog_audit` on the path that used to raise -- it called
    `backlog_transition`, which does -- and that indirection is exactly what
    made the defect a class instead of an episode.
    """
    reachable = set(AUDIT_WRITERS)
    calls = {
        name: {other for other in bodies if other != name and _mentions(body, other)}
        for name, body in bodies.items()
    }
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if name not in reachable and callees & reachable:
                reachable.add(name)
                changed = True
    return reachable


def _offenders(conn) -> list[str]:
    bodies = _functions(conn)
    return sorted(
        name
        for name in _audit_writing_closure(bodies)
        if _RAISES.search(bodies[name]) is not None
    )


def test_no_deployed_function_that_audits_can_raise(admin_conn) -> None:
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    bodies = _functions(admin_conn)
    assert "_backlog_audit" in bodies, "the schema did not migrate"

    assert _offenders(admin_conn) == [], (
        "these functions can write an audit row and then raise, which rolls "
        "that row back: " + ", ".join(_offenders(admin_conn))
    )


def test_the_integrity_trigger_that_must_raise_is_not_in_the_closure(
    admin_conn,
) -> None:
    """The guard's boundary, asserted rather than assumed.

    `work_attempt_claimant_is_derived` (0002) raises on purpose and must keep
    raising: it is a BEFORE INSERT trigger whose whole job is to make a forged
    `claimed_by_role` impossible, and it refuses BEFORE anything is written,
    so there is no audit row for the abort to take. A guard that could not
    tell that apart from `backlog_dispatch` would be one someone has to
    suppress, and a suppressed guard protects nothing.
    """
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    bodies = _functions(admin_conn)
    assert "work_attempt_claimant_is_derived" in bodies
    assert _RAISES.search(bodies["work_attempt_claimant_is_derived"]) is not None
    assert "work_attempt_claimant_is_derived" not in _audit_writing_closure(bodies)


def test_the_guard_would_catch_the_defect_it_was_written_for(admin_conn) -> None:
    """A guard nobody has seen fail is a guard nobody knows works.

    Reintroduce the exact shape 0003 refused to build -- a wrapper that calls
    the auditing identity gate and raises on its verdict -- and require the
    scan to name it. This is the ORIGINAL episode, one hop from an auditing
    function, rebuilt in a throwaway transaction.
    """
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            CREATE FUNCTION identity_assert_strict(p_secret text) RETURNS void
                LANGUAGE plpgsql AS $$
            DECLARE v identity_verdict;
            BEGIN
                v := identity_assert(p_secret);
                IF NOT v.ok THEN
                    RAISE EXCEPTION 'identity refused: %', v.reason;
                END IF;
            END
            $$;
            """
        )
    try:
        assert _offenders(admin_conn) == ["identity_assert_strict"], (
            "the closure missed a wrapper one hop from an auditing function"
        )
    finally:
        with admin_conn.cursor() as cur:
            cur.execute("DROP FUNCTION identity_assert_strict(text)")


def test_the_guard_catches_a_raiser_two_hops_from_the_audit(admin_conn) -> None:
    """One hop is the easy case; the deployed defect was two.

    `backlog_dispatch` raised on a verdict from `backlog_transition`, and it is
    `backlog_transition` -- not `backlog_dispatch` -- that calls
    `_backlog_audit`. Build a caller of a caller and require the fixpoint to
    reach it, so a future rewrite of the closure into a cheap one-hop scan
    fails here instead of silently passing everything.
    """
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            CREATE FUNCTION two_hops_away(p_task_id text) RETURNS void
                LANGUAGE plpgsql AS $$
            DECLARE v backlog_dispatch_verdict;
            BEGIN
                v := backlog_dispatch(p_task_id, 'p', 60, 1, '{}'::jsonb, 3);
                IF NOT v.ok THEN
                    RAISE EXCEPTION 'dispatch refused: %', v.reason;
                END IF;
            END
            $$;
            """
        )
    try:
        assert _offenders(admin_conn) == ["two_hops_away"]
    finally:
        with admin_conn.cursor() as cur:
            cur.execute("DROP FUNCTION two_hops_away(text)")


def test_migration_0010_reverses_to_the_defect_and_the_guard_names_it(
    admin_conn,
) -> None:
    """The historical proof, and 0010's reversibility in the same act.

    Down to 9 restores 0006's ``backlog_dispatch`` and 0009's
    ``backlog_ingest_results`` -- the two bodies that raised after
    ``backlog_transition()``/``backlog_return_to_pool()`` had audited the
    refusal -- so the scan must name exactly those two. Up again must remove
    them: a down-migration that left the fixed bodies in place would make the
    re-application a silent no-op, which is the failure
    ``test_migration_0009_is_reversible_without_residue`` pins for an earlier
    migration.
    """
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    assert _offenders(admin_conn) == []

    migrations.downgrade(admin_conn, target=9)
    assert _offenders(admin_conn) == ["backlog_dispatch", "backlog_ingest_results"], (
        "the down-migration did not restore the pre-fix bodies, so the "
        "next up would be an unmeasurable no-op"
    )

    migrations.upgrade(admin_conn)
    assert _offenders(admin_conn) == []
