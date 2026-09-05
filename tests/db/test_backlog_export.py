"""The PG→markdown projection renders exactly what the projection reader
parses (VOYN-W0-AICC-BACKLOG-EXPORT-PROJECTION)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from command_center import backlog_client
from command_center.db import backlog_export
from command_center.db.backlog_parser import ParsedTask, parse_backlog
from command_center.db.backlog_store import BacklogStore

_ROWS = [
    {
        "task_id": "VOYN-W0-AICC-EXAMPLE",
        "wave": "0",
        "priority": "P1",
        "status": "OPEN",
        "title": "plain title",
        "repo": "ai-command-center",
        "updated_at": datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    },
    {
        # The hostile row: a title carrying the field separator, newlines,
        # and an empty priority — none of which may break the record line.
        "task_id": "VOYN-W0-AICC-HOSTILE",
        "wave": "0.5",
        "priority": None,
        "status": "READY_TO_REVIEW",
        "title": (
            "evil | trailing-pipe breaker |"
            + backlog_client.FIELD_SEP
            + " sep\nand\u2028unicode\x0bverticals |"
        ),
        "repo": None,
        "updated_at": None,
    },
]


def test_roundtrip_through_the_real_parser():
    """The one consumer contract: every rendered line must come back from
    `parse_recommendations` as a record, never as a ParseError — including
    the row built to break field separation."""
    text = backlog_export.render_projection(_ROWS)
    result = backlog_client.parse_recommendations(text)
    assert result.errors == []
    assert len(result.records) == len(_ROWS)
    first, hostile = result.records
    assert first.issue_id == "VOYN-W0-AICC-EXAMPLE"
    assert first.ts == "2026-09-03T12:00:00Z"
    # `OPEN` is an execution status, translated to the planning vocabulary
    # this field actually carries -- see test_status_translates_execution_
    # vocabulary_into_planning_vocabulary below for the translation itself.
    assert first.status == "PO-Approved"
    assert first.current_wave == "0"
    assert first.task == "plain title"
    assert hostile.issue_id == "VOYN-W0-AICC-HOSTILE"
    assert hostile.priority == "-"
    assert backlog_client.FIELD_SEP not in hostile.task
    assert "\n" not in hostile.task


def test_every_parser_field_is_rendered_in_order():
    """A field added to RECOMMENDATION_FIELDS must break THIS test, not the
    console: the exporter derives its token order from the parser's own
    constant, and this pins that the derivation stays complete."""
    line = backlog_export.render_record(_ROWS[0])
    body = line[2:]
    tokens = body.split(backlog_client.FIELD_SEP)
    assert tokens[0] == backlog_client.RECOMMENDATION_MARKER
    keys = [token.partition("=")[0] for token in tokens[1:]]
    assert keys == list(backlog_client.RECOMMENDATION_FIELDS)


def test_header_survives_the_parser_as_prose():
    """The generated header (and its do-not-edit warning) must never parse
    as records or errors."""
    text = backlog_export.render_projection([])
    result = backlog_client.parse_recommendations(text)
    assert result.records == [] and result.errors == []


def test_status_translates_execution_vocabulary_into_planning_vocabulary():
    """`backlog_task.status` is the store's execution vocabulary (OPEN,
    IN_PROGRESS, ...); the 0B record's `status` field is a different,
    planning vocabulary (AI-Reco/PO-Review/PO-Approved) that
    `BacklogRecommendation.is_approved` checks by exact literal match against
    `PO-Approved`. Writing the raw execution value straight into that field
    would make `is_approved` -- and everything built on it: `approved_
    recommendations`, `execution_queue`, the panel's "Approved" metric --
    permanently false for every export-generated row, since no execution
    status ever equals `PO-Approved`. Proves the translation actually bridges
    the two, end to end through the real parser."""
    rows = [
        {**_ROWS[0], "status": "IN_PROGRESS"},
        {**_ROWS[0], "task_id": "VOYN-W0-AICC-UNTRIAGED", "status": "UNTRIAGED"},
    ]
    result = backlog_client.parse_recommendations(backlog_export.render_projection(rows))
    approved, untriaged = result.records
    assert approved.status == "PO-Approved"
    assert approved.is_approved is True
    assert untriaged.status != "PO-Approved"
    assert untriaged.is_approved is False


def test_planning_status_partitions_the_full_db_vocabulary():
    """Every status the store's CHECK constraint allows
    (`backlog_parser.STATUSES`) must land in exactly one bucket -- guards
    against a newly added status silently falling through to "not approved"
    (or the reverse) without anyone updating this translation."""
    from command_center.db.backlog_parser import EXECUTABLE_STATUSES, STATUSES

    for status in STATUSES:
        approved = backlog_export._planning_status(status) == backlog_client.STATUS_APPROVED
        assert approved == (status in EXECUTABLE_STATUSES), status


def test_reimporting_a_projection_through_the_real_importer_is_a_no_op():
    """Proves the claim in the module docstring and ADR-0011 ("a render
    written by backlog-export and then re-imported by backlog-import must be
    a no-op") against `backlog_parser.parse_backlog` itself -- the function
    `backlog-import` actually calls -- rather than assuming it.

    The no-op holds, but not for the reason the docstring's field-mapping
    paragraph might suggest (narrative fields rendering as `-`): `parse_backlog`
    only recognizes bold task lines (`_TASK_LINE`: ``- **ID** | ...``), and a
    rendered record (``- VOYN_RECOMMENDATION | ts=... | ...``, no bold id) does
    not match that shape at all -- not even as a reported "unparsed" line, it
    is simply invisible to the importer. The two formats occupy disjoint
    syntax, which is the actual mechanism keeping a manual re-import inert."""
    report = parse_backlog(backlog_export.render_projection(_ROWS))
    assert report.tasks == []
    assert report.unparsed == []


def _task(task_id: str, **overrides) -> ParsedTask:
    values = dict(
        task_id=task_id,
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title=task_id.lower(),
        body="",
        repo=None,
        line_no=1,
    )
    values.update(overrides)
    return ParsedTask(**values)


def test_fetch_rows_reads_the_real_table_in_wave_priority_task_order(pg_connection_factory):
    """`render_record`/`render_projection` are proved above as pure functions
    over plain dicts; `fetch_rows` — the only part of this module that talks
    to `backlog_task` — had no coverage against a real table. Proves its
    column list matches the schema, and that a NULL priority sorts last
    within its wave (the query's explicit `nulls last`) rather than first
    (`NULLS FIRST` is Postgres's ASC default and would put an untriaged task
    ahead of a P0 one)."""
    store = BacklogStore(pg_connection_factory)
    for task in [
        _task("VOYN-W0-FETCH-B", wave="1", priority="P1", status="DONE", repo="repo-b"),
        _task("VOYN-W0-FETCH-NULL", wave="0", priority=None, status="OPEN"),
        _task("VOYN-W0-FETCH-A", wave="0", priority="P0", status="IN_PROGRESS", repo="repo-a"),
    ]:
        ok, reason, _ = store.upsert_task(task)
        assert ok, reason

    with pg_connection_factory() as conn:
        rows = backlog_export.fetch_rows(conn)

    ours = [row for row in rows if row["task_id"].startswith("VOYN-W0-FETCH-")]
    assert [row["task_id"] for row in ours] == [
        "VOYN-W0-FETCH-A",
        "VOYN-W0-FETCH-NULL",
        "VOYN-W0-FETCH-B",
    ]
    first = dict(ours[0])
    updated_at = first.pop("updated_at")
    assert isinstance(updated_at, datetime)
    assert first == {
        "task_id": "VOYN-W0-FETCH-A",
        "wave": "0",
        "priority": "P0",
        "status": "IN_PROGRESS",
        "title": "voyn-w0-fetch-a",
        "repo": "repo-a",
    }
