"""`historical_backfill` — the two failure modes independent review found.

Both regressions here were rejections of earlier PRs for this same task, not
hypotheticals:

* `--table typo` matched nothing, the run "succeeded" having copied zero rows,
  and the CLI exited 0 — indistinguishable from a deliberate, focused backfill
  of one table (PR #436's REJECT).
* the run shares one PostgreSQL connection for its whole duration, and a bare
  `try/except` around each `upsert()` is not enough to make that safe: without
  a transaction/savepoint per row, the first rejected row leaves the
  connection's transaction aborted and every later statement on it — later
  upserts, `resync_identity()`, reconciliation — raises too (PR #475's
  REJECT).

The behavioural test below needs a real PostgreSQL server for the same reason
`test_mirror_contract.py` does: an aborted-transaction cascade is exactly the
kind of thing a mocked driver cannot reproduce, since the test would only be
asserting that its own fake behaves as expected. It reuses that suite's
`_ensure_parents`/`pg_connection_factory` machinery rather than duplicating it.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import pytest

from command_center.db.historical_backfill import (
    UnknownTablesError,
    resolve_tables,
    run_historical_backfill,
)
from command_center.db.run_children_store import PostgresReportMirror
from command_center.runtime.db import core as runtime_core

from tests.db.test_mirror_contract import SAMPLE_TIMESTAMP, _ensure_parents


def test_resolve_tables_limits_the_run_to_a_valid_subset() -> None:
    resolved = resolve_tables({"report"})
    assert [table for table, _ in resolved] == ["report"]


def test_resolve_tables_rejects_a_name_no_mirror_declares() -> None:
    """The exact defect PR #436 was rejected for: a typo must fail loudly,
    not silently run the valid names and report success."""
    with pytest.raises(UnknownTablesError) as excinfo:
        resolve_tables({"report", "typo"})
    assert excinfo.value.unknown == ("typo",)
    assert "typo" in str(excinfo.value)


def test_an_unknown_table_is_refused_before_either_database_opens(tmp_path) -> None:
    sqlite_path = tmp_path / "does-not-exist.db"

    def _connection_factory():
        raise AssertionError("connection_factory must not be called: validation failed first")

    with pytest.raises(UnknownTablesError):
        run_historical_backfill(sqlite_path, _connection_factory, tables={"typo"})

    assert not sqlite_path.exists()


def test_a_rejected_row_does_not_poison_the_rest_of_the_run(tmp_path, pg_connection_factory) -> None:
    """PR #475's REJECT, reproduced and fixed: one FK-violating row must not
    take the rest of the table — or `resync_identity`/reconciliation after
    it — down with it on the shared connection."""
    _ensure_parents(PostgresReportMirror.spec, pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    runtime_core.migrate(db_path)
    raw = sqlite3.connect(str(db_path))
    try:
        # Sorted before "run-parent", so `_read_stored_rows`'s `ORDER BY
        # run_id` puts the failing row first -- proving survival, not luck.
        raw.execute(
            "INSERT INTO report (run_id, path, created_at) VALUES (?, ?, ?)",
            ("0-missing-run", "/reports/missing.json", SAMPLE_TIMESTAMP),
        )
        raw.execute(
            "INSERT INTO report (run_id, path, created_at) VALUES (?, ?, ?)",
            ("run-parent", "/reports/good.json", SAMPLE_TIMESTAMP),
        )
        raw.commit()
    finally:
        raw.close()

    with pg_connection_factory() as conn:
        report = run_historical_backfill(db_path, lambda: nullcontext(conn), tables={"report"})

    [table_report] = report.tables
    assert table_report.table == "report"
    assert table_report.rows_read == 2
    assert table_report.rows_written == 1
    assert [key for key, _reason in table_report.errors] == [("0-missing-run",)]
    assert "foreign key" in table_report.errors[0][1].lower()

    mirror = PostgresReportMirror(connection_factory=pg_connection_factory)
    assert [row["run_id"] for row in mirror.list_records()] == ["run-parent"]
