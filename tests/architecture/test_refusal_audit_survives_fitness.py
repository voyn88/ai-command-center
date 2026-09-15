"""Fitness function: no function that can write an audit row may raise.

The class this gate exists for (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS) is not one
function's bug. `identity_assert()` writes the denial audit and returns a
verdict; any wrapper that turns that verdict into an exception destroys the row
it just wrote, *at every call site*. The same shape was live in the backlog
planner (`backlog_dispatch`, `backlog_ingest_results`, `backlog_split_task`,
`backlog_set_task_class`), which is why the rule is enforced here over the
whole migration set rather than argued about per function.

The subject of the rule is COMPUTED (see `refusal_audit`): audit tables are the
ones whose names end in `_event`, writers are the functions that insert into
them, and the closure is everything that can reach a writer. A fourth audit
surface added tomorrow is inside the gate the moment its table and writer land,
with nothing to remember to update — and the tests below pin exactly that,
along with the two ways a hand-rolled version of this check goes blind:
overload collision and a superseded definition.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture import refusal_audit


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _audit_schema(table: str = "widget_event") -> str:
    return f"""
CREATE TABLE {table} (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    outcome  text NOT NULL
);
CREATE FUNCTION widget_audit(p_outcome text) RETURNS void
    LANGUAGE sql AS $$
    INSERT INTO {table} (outcome) VALUES (p_outcome);
$$;
"""


# ---------------------------------------------------------------------------
# The repository itself
# ---------------------------------------------------------------------------


def test_no_deployed_function_that_can_audit_can_raise() -> None:
    found = refusal_audit.offenders()
    assert not found, (
        "these deployed functions can write an audit row and can also RAISE, so "
        "a refusal they record can be erased by the exception they raise:\n  "
        + "\n  ".join(f"{function} (from {function.source})" for function in found)
        + "\nReturn a verdict instead: a refusal is data, not an exception."
    )


def test_the_three_audit_surfaces_are_found_without_being_listed() -> None:
    """The seed is a query over the schema, not a maintained constant."""
    model = refusal_audit.build_model()
    writers = {name for name, _args in refusal_audit.audit_writers(model)}
    assert {"_backlog_audit", "_queue_audit", "_principal_audit"} <= writers
    assert {"backlog_event", "work_event", "principal_event"} <= model.audit_tables


def test_the_closure_reaches_the_functions_that_only_call_an_audit_writer() -> None:
    """Two hops are in: `backlog_dispatch` never inserts a row itself."""
    model = refusal_audit.build_model()
    closure = {name for name, _args in refusal_audit.audit_closure(model)}
    assert {"backlog_dispatch", "backlog_ingest_results", "queue_fail"} <= closure


def test_the_diagnostic_raise_log_in_principal_audit_is_not_an_offence() -> None:
    """`RAISE LOG` writes a server-log line; it aborts nothing."""
    model = refusal_audit.build_model()
    audit = model.functions[("_principal_audit", ("text", "text", "text", "text", "text", "jsonb"))]
    assert "RAISE LOG" in audit.body
    assert not refusal_audit.can_raise(audit.body)


def test_the_gate_catches_the_regression_it_exists_for(tmp_path) -> None:
    """Copy the real schema, add back one raising refusal path, fail."""
    for path in refusal_audit.SQL_DIR.iterdir():
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert not refusal_audit.offenders(tmp_path)
    _write(
        tmp_path,
        "0026_regression.up.sql",
        """
CREATE OR REPLACE FUNCTION backlog_set_task_class(p_task_id text, p_class text)
    RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    IF p_class NOT IN ('functional', 'pipeline') THEN
        RAISE EXCEPTION 'invalid task class: %', p_class;
    END IF;
    PERFORM _backlog_audit(p_task_id, 'task_class', 'granted', p_class);
    RETURN true;
END
$$;
""",
    )
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == [
        "backlog_set_task_class(text, text)"
    ]


# ---------------------------------------------------------------------------
# The computation itself — the ways a weaker version of it goes blind
# ---------------------------------------------------------------------------


def test_a_new_audit_surface_is_covered_without_editing_this_file(tmp_path) -> None:
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION widget_refuse(p_id text) RETURNS boolean
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RAISE EXCEPTION 'refused %', p_id;
END
$$;
""",
    )
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == ["widget_refuse(text)"]


def test_a_table_whose_name_merely_ends_in_event_is_not_an_audit_table(tmp_path) -> None:
    """`LIKE '%_event'` would match all three of these: `_` is a wildcard."""
    _write(
        tmp_path,
        "0001_lookalikes.up.sql",
        """
CREATE TABLE solvent (id bigint PRIMARY KEY, outcome text);
CREATE TABLE xevent (id bigint PRIMARY KEY, outcome text);
CREATE TABLE preventevent (id bigint PRIMARY KEY, outcome text);
CREATE FUNCTION lookalike_write(p_outcome text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO solvent (outcome) VALUES (p_outcome);
    INSERT INTO xevent (outcome) VALUES (p_outcome);
    INSERT INTO preventevent (outcome) VALUES (p_outcome);
    RAISE EXCEPTION 'not an audit writer, so not this gate''s business';
END
$$;
""",
    )
    model = refusal_audit.build_model(tmp_path)
    assert model.audit_tables == frozenset()
    assert not refusal_audit.offenders(tmp_path)


def test_overloads_do_not_collapse_into_one_body(tmp_path) -> None:
    """`{proname: prosrc}` lets the second overload overwrite the first.

    Here the raising overload is parsed last, so a name-keyed model would keep
    only `pick(integer)` — whose body has no audit write — and report no
    offender at all. Keyed by signature, both survive and the audit-writing
    raiser is caught.
    """
    _write(
        tmp_path,
        "0001_overloads.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION pick(p_id integer) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'no audit here, just a raise: %', p_id;
END
$$;
CREATE FUNCTION pick(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RAISE EXCEPTION 'audited, then erased: %', p_id;
END
$$;
""",
    )
    model = refusal_audit.build_model(tmp_path)
    assert {("pick", ("integer",)), ("pick", ("text",))} <= set(model.functions)
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == ["pick(text)"]


def test_a_later_migration_replaces_the_definition_it_supersedes(tmp_path) -> None:
    _write(tmp_path, "0001_widgets.up.sql", _audit_schema())
    _write(
        tmp_path,
        "0002_raises.up.sql",
        """
CREATE FUNCTION widget_refuse(p_id text) RETURNS boolean
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RAISE EXCEPTION 'refused %', p_id;
END
$$;
""",
    )
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == ["widget_refuse(text)"]
    _write(
        tmp_path,
        "0003_returns.up.sql",
        """
CREATE OR REPLACE FUNCTION widget_refuse(p_id text) RETURNS boolean
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RETURN false;
END
$$;
""",
    )
    assert not refusal_audit.offenders(tmp_path)


def test_a_dropped_function_leaves_the_closure(tmp_path) -> None:
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION widget_refuse(p_id text) RETURNS boolean
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RAISE EXCEPTION 'refused %', p_id;
END
$$;
""",
    )
    _write(tmp_path, "0002_drop.up.sql", "DROP FUNCTION IF EXISTS widget_refuse(text);\n")
    assert not refusal_audit.offenders(tmp_path)


def test_the_closure_is_transitive_over_several_hops(tmp_path) -> None:
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION widget_inner(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
END
$$;
CREATE FUNCTION widget_middle(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_inner(p_id);
END
$$;
CREATE FUNCTION widget_outer(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_middle(p_id);
    RAISE EXCEPTION 'three hops from the audit write: %', p_id;
END
$$;
""",
    )
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == ["widget_outer(text)"]


def test_raise_and_insert_inside_comments_and_strings_are_not_code(tmp_path) -> None:
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
-- A comment that says RAISE EXCEPTION and INSERT INTO widget_event.
CREATE FUNCTION widget_quiet(p_id text) RETURNS text
    LANGUAGE plpgsql AS $$
BEGIN
    /* INSERT INTO widget_event -- in a block comment */
    RETURN 'RAISE EXCEPTION is only a word in this string, as is '
           || 'INSERT INTO widget_event';
END
$$;
""",
    )
    model = refusal_audit.build_model(tmp_path)
    quiet = model.functions[("widget_quiet", ("text",))]
    assert not refusal_audit.can_raise(quiet.body)
    assert refusal_audit.writes_audit_table(quiet.body, model.audit_tables) is None
    assert not refusal_audit.offenders(tmp_path)


def test_a_raise_that_only_logs_is_not_an_offence(tmp_path) -> None:
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION widget_note(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
    RAISE LOG 'a denial also reaches the server log: %', p_id;
    RAISE NOTICE 'and the client, if it listens';
END
$$;
""",
    )
    assert not refusal_audit.offenders(tmp_path)


def test_a_bare_reraise_is_an_offence(tmp_path) -> None:
    """`RAISE;` inside a handler re-raises: the audit still dies with it."""
    _write(
        tmp_path,
        "0001_widgets.up.sql",
        _audit_schema()
        + """
CREATE FUNCTION widget_reraise(p_id text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM widget_audit('rejected');
EXCEPTION
    WHEN others THEN
        RAISE;
END
$$;
""",
    )
    assert [str(f) for f in refusal_audit.offenders(tmp_path)] == ["widget_reraise(text)"]


def test_down_migrations_are_not_the_deployed_schema(tmp_path) -> None:
    """The gate reads `*.up.sql`; a down-migration restores the old body."""
    _write(tmp_path, "0001_widgets.up.sql", _audit_schema())
    _write(
        tmp_path,
        "0001_widgets.down.sql",
        """
CREATE OR REPLACE FUNCTION widget_audit(p_outcome text) RETURNS void
    LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO widget_event (outcome) VALUES (p_outcome);
    RAISE EXCEPTION 'the shape this repository is moving away from';
END
$$;
""",
    )
    assert not refusal_audit.offenders(tmp_path)
