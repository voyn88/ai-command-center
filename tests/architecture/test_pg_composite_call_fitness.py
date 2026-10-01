"""Architecture fitness gate: no call site re-evaluates a composite per column.

`tests/db/test_queue_claim.py::test_no_call_site_uses_the_per_column_expansion_form`
guards this shape for `queue_`-prefixed functions inside one test file. This
repo has several other composite-returning functions -- `backlog_transition`,
`backlog_dispatch`, `identity_assert`, `enroll_redeem_ticket`, and more, across
`command_center/db/sql/*.sql` -- that the queue-scoped rule cannot see, and a
call written against any of them, in any `.py` or `.sql` file, would slip
through review looking correct: the query still returns the right verdict, it
just paid for it with extra invisible function calls (`test_queue_claim.py`'s
`test_one_claim_call_consumes_exactly_one_attempt` is the behavioural version
of that same trap, pinned for `queue_claim` specifically).

The scanner lives in `tests/architecture/pg_composite_call.py`. This file's
own sample text for the bad form is built at runtime from separate pieces via
`_dereference`, never as one literal -- otherwise the repo-wide scan below
would flag its own fixtures.
"""

from __future__ import annotations

import ast

from tests.architecture import pg_composite_call as scanner


def _dereference(call: str, accessor: str) -> str:
    """Build `(call).accessor` at runtime -- never as a literal in this file."""
    return "(" + call + ")." + accessor


def test_no_call_site_uses_the_per_column_composite_dereference_form():
    violations = scanner.find_violations()
    assert violations == [], (
        "PostgreSQL re-evaluates a composite-returning function once per "
        "column pulled out of it via dot-notation on the call, instead of "
        "once -- use `SELECT * FROM f(...)` in SQL, or assign the call to a "
        "variable once in PL/pgSQL, instead:\n" + "\n".join(str(v) for v in violations)
    )


def test_scanner_fires_on_the_form_it_exists_to_catch():
    """An empty result above must mean "none present", not "dead rule"."""
    assert scanner.BAD_FORM.search(_dereference("backlog_dispatch(%s)", "*"))
    assert scanner.BAD_FORM.search(_dereference("identity_assert(x)", "ok"))
    # A single dereferenced column is flagged too, not just `.*`: Postgres
    # already re-evaluates per reference, and one reference today is two
    # tomorrow.
    assert scanner.BAD_FORM.search("v_ok := " + _dereference("f(x)", "ok") + ";")
    # The safe forms this gate must never flag.
    assert not scanner.BAD_FORM.search("SELECT * FROM backlog_dispatch(%s)")
    assert not scanner.BAD_FORM.search("v := backlog_dispatch(%s); RETURN v.ok;")


def test_docstrings_naming_the_bad_form_are_excluded_by_identity():
    doc_source = '"""Discusses ' + _dereference("f(x)", "*") + ' as an example of the bad form."""\n'
    tree = ast.parse(doc_source)
    doc_node = tree.body[0].value
    assert id(doc_node) in scanner._python_docstring_ids(tree)


def test_sql_comments_naming_the_bad_form_are_stripped_before_scanning():
    raw = (
        "-- " + _dereference("f(x)", "*") + " is the bad form\n"
        + "SELECT 1; /* " + _dereference("g(y)", "ok") + " too */\n"
    )
    stripped = scanner._strip_sql_comments(raw)
    assert "f(x)" not in stripped
    assert "g(y)" not in stripped
    assert "SELECT 1;" in stripped


def test_scanner_catches_a_real_violation_in_either_file_type(tmp_path):
    py_file = tmp_path / "offender.py"
    py_file.write_text(
        # Concatenated at the point of use: two literals joined by `+`, never
        # one -- the same escape the production canary in test_queue_claim.py
        # relies on, so line 1 must NOT be caught.
        'ASSEMBLED = "queue_claim" + "(%s)).*"\n'
        + 'QUERY = "' + _dereference("backlog_dispatch(%s)", "*") + '"\n'
    )
    py_violations = scanner._scan_python_file(py_file, "offender.py")
    assert [v.lineno for v in py_violations] == [2]

    sql_file = tmp_path / "offender.sql"
    sql_file.write_text(
        "-- a comment, not a call site\n"
        + "SELECT " + _dereference("identity_assert(%s)", "ok") + ";\n"
    )
    sql_violations = scanner._scan_sql_file(sql_file, "offender.sql")
    assert [v.lineno for v in sql_violations] == [2]
