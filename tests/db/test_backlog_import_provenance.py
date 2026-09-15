"""The importer acts on the provenance verdict it is handed. Hermetic.

The migration gate's third acceptance criterion ("existing records migrated
with provenance") is a property of EVERY migrated row, and 0025 now makes the
insert and the stamp one transaction so the database cannot commit half of
it. That leaves one way the guarantee could still be lost quietly: the Python
caller taking a `provenance_recorded = false` back and reporting the row as
`inserted` anyway. These tests hold the seam with a scripted connection --
no PostgreSQL, so they run in the default laptop gate where the rest of the
backlog-store suite skips, and they can assert the one verdict a live
database is built never to return.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from command_center.db.backlog_parser import parse_backlog
from command_center.db.backlog_store import BacklogStore, ProvenanceNotRecorded

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "backlog_sample.md"
TEXT = FIXTURE.read_text(encoding="utf-8")
TASKS = parse_backlog(TEXT).tasks


class _ScriptedConnection:
    """One fixed verdict for every call, and a record of the SQL it saw."""

    def __init__(self, verdict: tuple) -> None:
        self.verdict = verdict
        self.statements: list[str] = []

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append(sql)

    def fetchone(self) -> tuple:
        return self.verdict


def _store(verdict: tuple) -> tuple[BacklogStore, _ScriptedConnection]:
    conn = _ScriptedConnection(verdict)

    @contextmanager
    def factory():
        yield conn

    return BacklogStore(factory), conn


# (ok, reason, changed, revision, provenance_recorded)
_STAMPED = (True, "inserted", True, 1, True)
_UNSTAMPED = (True, "inserted", True, 1, False)


def test_an_unstamped_insert_stops_the_import_instead_of_counting_it() -> None:
    """The bug this remediation exists for: a row reported `inserted` whose
    provenance never landed used to be counted as a successful migration, so
    an operator reading the report saw the acceptance criterion met for a row
    that does not meet it."""
    store, _ = _store(_UNSTAMPED)
    with pytest.raises(ProvenanceNotRecorded) as raised:
        store.import_markdown(TEXT)
    assert raised.value.task_id == TASKS[0].task_id
    assert TASKS[0].task_id in str(raised.value)


def test_a_stamped_insert_is_counted_as_the_migration_it_is() -> None:
    store, _ = _store(_STAMPED)
    report = store.import_markdown(TEXT)
    assert report.inserted == len(TASKS)
    assert report.refused == []
    assert report.unparsed, "the fixture's malformed lines are still reported"


def test_the_insert_and_its_stamp_are_one_statement_not_two() -> None:
    """The non-atomic pair is gone, not merely discouraged: nothing in the
    import path calls `backlog_record_provenance` as a follow-up round trip
    that a crash in between could lose."""
    store, conn = _store(_STAMPED)
    store.import_markdown(TEXT)
    assert conn.statements, "the import issued no SQL at all"
    for statement in conn.statements:
        assert "backlog_import_task(" in statement
        assert "backlog_record_provenance" not in statement
    assert len(conn.statements) == len(TASKS), "one round trip per record"
