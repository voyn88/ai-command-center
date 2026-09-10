"""The class guard: nothing that audits a refusal may then raise.

VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS. The episode was one raising wrapper; the
CLASS is that an exception aborts the transaction the audit row lives in, so
*any* caller that turns a returned verdict back into an exception deletes the
refusal record its callee just wrote -- and every call site of an auditing
function is a place it can reappear. 0002 measured the shape (0 audit rows
after ``RAISE``, 1 after ``RETURN``) and 0003 restated it for identity; both
statements were prose that no gate enforced across layers, which is why the
same defect was still live in ``backlog_dispatch`` (0006) and
``backlog_ingest_results`` (0007/0009/0011) when 0017 removed it.

This is that gate, and it is computed rather than listed, on two axes:

* It reads the DEPLOYED schema -- every function as PostgreSQL actually holds
  it after the full migration run, so a definition superseded by a later
  ``DROP``/``CREATE`` is judged only in its final form -- and takes the
  transitive closure of "can write an audit row" over the call graph. Every
  function in that closure must refuse by returning.
* The seed of that closure -- "writes an audit row" -- is itself computed
  from the catalog rather than a maintained list of writer function names: a
  table is an audit trail if its name ends ``_event`` and it carries both an
  ``outcome`` and a ``reason`` column (the shape ``backlog_event``,
  ``work_event`` and ``principal_event`` all share), and the seed is every
  function whose body inserts into one. A fourth audit surface is covered
  the moment its writer inserts into a fourth ``*_event`` table shaped like
  the other three -- no edit here required, which a fixed name list cannot
  claim honestly.

Functions are keyed by ``pg_proc.oid``, not by name: PostgreSQL allows
overloads sharing one name, and a name-keyed dict would let one overload's
body silently overwrite another's in the map, hiding whichever overload lost
the collision from every check below.

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

    Both routinely contain the word RAISE -- 0002 and 0017 explain in prose
    exactly what a RAISE there would have discarded -- and a guard that
    flagged prose would be turned off within a week.
    """
    return _LITERAL.sub("''", _COMMENT.sub("", body))


def _functions(conn) -> dict[int, tuple[str, str]]:
    """``oid -> (proname, code)`` for every function in the public schema.

    Keyed by oid rather than name so two overloads of the same name each get
    their own entry -- a `{proname: prosrc}` dict would let the second
    overload PostgreSQL returns silently replace the first in the map, and
    whichever body lost that race would never be scanned for a RAISE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid, p.proname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind = 'f'"
        )
        return {oid: (name, _code(src)) for oid, name, src in cur.fetchall()}


def _audit_tables(conn) -> list[str]:
    """Tables recognisable as an audit trail by shape, not by an enumerated
    list: name ending ``_event`` plus both an ``outcome`` and a ``reason``
    column -- exactly what ``backlog_event``, ``work_event`` and
    ``principal_event`` share. A future audit surface built the same way is
    picked up here without a Python-side edit.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "  AND c.relname LIKE %s "
            "  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
            "               AND a.attname = 'outcome' AND NOT a.attisdropped) "
            "  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
            "               AND a.attname = 'reason' AND NOT a.attisdropped)",
            ("%_event",),
        )
        return [row[0] for row in cur.fetchall()]


def _mentions(body: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\s*\(", body) is not None


def _writes_audit_table(body: str, table: str) -> bool:
    return re.search(rf"\bINSERT\s+INTO\s+{re.escape(table)}\b", body, re.IGNORECASE) is not None


def _seed_writers(functions: dict[int, tuple[str, str]], audit_tables: list[str]) -> set[int]:
    return {
        oid
        for oid, (_name, body) in functions.items()
        if any(_writes_audit_table(body, table) for table in audit_tables)
    }


def _audit_writing_closure(functions: dict[int, tuple[str, str]], seed: set[int]) -> set[int]:
    """Every function that can reach an audit INSERT, directly or through
    another function -- a fixpoint rather than one hop.

    `backlog_dispatch` never called `_backlog_audit` on the path that used to
    raise -- it called `backlog_transition`, which does -- and that
    indirection is exactly what made the defect a class instead of an
    episode. Mentions are matched by NAME: a call site in SQL text does not
    spell out which overload it resolves to, so mentioning `foo(` marks
    EVERY oid named `foo` as a callee -- conservative, and the only sound
    choice without a full parse of PostgreSQL's overload resolution.
    """
    calls: dict[int, set[int]] = {
        oid: {
            other_oid
            for other_oid, (other_name, _ob) in functions.items()
            if other_oid != oid and _mentions(body, other_name)
        }
        for oid, (_name, body) in functions.items()
    }
    reachable = set(seed)
    changed = True
    while changed:
        changed = False
        for oid, callees in calls.items():
            if oid not in reachable and callees & reachable:
                reachable.add(oid)
                changed = True
    return reachable


def _offenders(conn) -> list[str]:
    functions = _functions(conn)
    seed = _seed_writers(functions, _audit_tables(conn))
    closure = _audit_writing_closure(functions, seed)
    return sorted(
        {
            name
            for oid, (name, body) in functions.items()
            if oid in closure and _RAISES.search(body) is not None
        }
    )


def test_no_deployed_function_that_audits_can_raise(admin_conn) -> None:
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    functions = _functions(admin_conn)
    names = {name for name, _body in functions.values()}
    assert "_backlog_audit" in names, "the schema did not migrate"

    assert _offenders(admin_conn) == [], (
        "these functions can write an audit row and then raise, which rolls "
        "that row back: " + ", ".join(_offenders(admin_conn))
    )


def test_the_seed_is_computed_from_the_three_known_audit_tables(admin_conn) -> None:
    """The seed detection is pinned to the schema's real shape, not to an
    ad hoc guess: exactly the three writer functions this migration's history
    names, and nothing else, until a fourth `*_event` table appears."""
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    assert set(_audit_tables(admin_conn)) == {
        "backlog_event",
        "work_event",
        "principal_event",
    }
    functions = _functions(admin_conn)
    seed = _seed_writers(functions, _audit_tables(admin_conn))
    seed_names = {functions[oid][0] for oid in seed}
    assert seed_names == {"_backlog_audit", "_queue_audit", "_principal_audit"}


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
    functions = _functions(admin_conn)
    by_name = {name: (oid, body) for oid, (name, body) in functions.items()}
    assert "work_attempt_claimant_is_derived" in by_name
    oid, body = by_name["work_attempt_claimant_is_derived"]
    assert _RAISES.search(body) is not None
    seed = _seed_writers(functions, _audit_tables(admin_conn))
    assert oid not in _audit_writing_closure(functions, seed)


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


def test_the_guard_does_not_collapse_overloads(admin_conn) -> None:
    """A second overload of a real audit-writing function name, given a body
    that RAISES, must be caught even though a same-named, safe overload also
    exists. A `{proname: prosrc}` dict would let one of the two bodies
    silently win the collision and hide the other from every check above --
    this pins that PostgreSQL overloading cannot be used to smuggle a raiser
    past the guard under a name that already reads as "known safe".
    """
    from command_center.db import migrations

    migrations.upgrade(admin_conn)
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            CREATE FUNCTION _backlog_audit(
                p_task_id text, p_event text, p_outcome text, p_reason text,
                p_detail jsonb, p_extra text
            ) RETURNS void LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM _backlog_audit(p_task_id, p_event, p_outcome, p_reason, p_detail);
                RAISE EXCEPTION 'overload should not hide behind the safe one: %', p_extra;
            END
            $$;
            """
        )
    try:
        assert "_backlog_audit" in _offenders(admin_conn), (
            "an overload of a real audit writer can raise and the guard missed it"
        )
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(
                "DROP FUNCTION _backlog_audit(text, text, text, text, jsonb, text)"
            )


def test_migration_0017_reverses_to_the_defect_and_the_guard_names_it(
    admin_conn,
) -> None:
    """The historical proof, and 0017's reversibility in the same act.

    Down to 16 restores 0006's ``backlog_dispatch`` and 0011's
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

    migrations.downgrade(admin_conn, target=16)
    assert _offenders(admin_conn) == ["backlog_dispatch", "backlog_ingest_results"], (
        "the down-migration did not restore the pre-fix bodies, so the "
        "next up would be an unmeasurable no-op"
    )

    migrations.upgrade(admin_conn)
    assert _offenders(admin_conn) == []
