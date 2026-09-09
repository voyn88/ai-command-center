"""The PG→markdown projection renders exactly what the projection reader
parses (VOYN-W0-AICC-BACKLOG-EXPORT-PROJECTION)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from command_center import backlog_client
from command_center.db import backlog_export
from command_center.db.backlog_parser import ParsedTask, parse_backlog
from command_center.db.backlog_store import BacklogStore

#: The render clock every test below pins, so a rendered file is a
#: function of its inputs alone and an expected header can be written out
#: byte for byte.
_GENERATED_AT = datetime(2026, 9, 9, 6, 30, tzinfo=UTC)

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
    text = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
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
    text = backlog_export.render_projection([], generated_at=_GENERATED_AT)
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
    result = backlog_client.parse_recommendations(
        backlog_export.render_projection(rows, generated_at=_GENERATED_AT)
    )
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
    report = parse_backlog(
        backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    )
    assert report.tasks == []
    assert report.unparsed == []


def test_the_header_stamps_when_it_was_rendered_and_from_how_many_rows():
    """The projection has to be able to tell its own reader that it is stale.

    The failure this exporter exists to end was a *silent* one: a console
    rendering a two-week-old file with nothing in the content to say so. The
    console reads freshness from mtime, but mtime lives in the filesystem,
    not in the text -- it does not survive a copy or an scp, and the owner
    reading the rendered markdown in an editor never sees it. So the stamp
    goes in the file, and this pins that it is really there, in UTC, with the
    row count beside it."""
    text = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    assert "Rendered 2026-09-09T06:30:00Z from 2 task row(s)" in text
    assert "Rendered 2026-09-09T06:30:00Z from 0 task row(s)" in (
        backlog_export.render_projection([], generated_at=_GENERATED_AT)
    )
    # Rendered in UTC whatever the caller's zone, so two ticks' stamps are
    # comparable and neither is ambiguous about which clock it means.
    other_zone = _GENERATED_AT.astimezone(timezone(timedelta(hours=5)))
    assert other_zone != _GENERATED_AT.replace(tzinfo=None)
    assert "Rendered 2026-09-09T06:30:00Z" in (
        backlog_export.render_projection([], generated_at=other_zone)
    )


def test_the_render_clock_is_an_argument_not_a_hidden_now():
    """Two renders of the same rows must be byte-identical -- the property
    that lets a test assert on a whole rendered file at all, and the reason
    `generated_at` is a required keyword rather than a `datetime.now()` read
    inside the renderer. Required, not defaulted: a silently omitted stamp
    would leave the projection claiming nothing about its own age, which is
    exactly the state the header exists to end."""
    first = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    second = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    assert first == second
    with pytest.raises(TypeError):
        backlog_export.render_projection(_ROWS)


def test_a_rendering_is_recognised_as_generated_and_an_authored_file_is_not():
    """`is_generated_projection` is the check `backlog-import` refuses on, so
    it has to be right in both directions.

    False negatives re-open the silent no-op it exists to prevent; false
    positives are worse -- they would refuse the owner's real backlog and
    freeze the store. The marker is matched as a whole line and only inside
    the header for that reason: the authored backlog legitimately *describes*
    this exporter inside a task body (BO-S4 is a task in it), and quoting the
    sentence there must not make the file unimportable."""
    rendered = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    assert backlog_export.is_generated_projection(rendered) is True

    authored = "- **VOYN-W0-X** | Wave 0 | OPEN | P0 | d | `s` | body\n"
    assert backlog_export.is_generated_projection(authored) is False

    quoted_deep_in_a_body = (
        "# VOYN master backlog\n\n"
        + "filler\n" * backlog_client.HEADER_SCAN_LINES
        + "- **VOYN-W0-BO-S4** | Wave 0 | OPEN | P0 | "
        + f"{backlog_export.GENERATED_MARKER}\n"
        + f"  {backlog_export.GENERATED_MARKER}\n"
    )
    assert backlog_export.is_generated_projection(quoted_deep_in_a_body) is False


def test_the_header_stamp_is_readable_by_the_projection_reader(tmp_path):
    """The stamp is not decoration for a human — it is the freshness signal the
    console shows (`Projection.stamp`, `master_backlog_panel._render_freshness`).
    So it is not enough that the header *mentions* a render time: the real
    rendered file has to parse back through `backlog_client.parse_generated_stamp`
    at the exact clock and row count it was rendered with.

    This is the guard against the failure the whole stamp exists to prevent
    recurring in a new form. Reword the header so the stamp stops parsing, and
    nothing breaks loudly: `parse_generated_stamp` returns None, the panel
    silently falls back to mtime, and a projection whose tick died reads as
    fresh the moment it is copied anywhere -- exactly the 2026-08-20-file-on-a-
    2026-09-03-console failure, restored quietly."""
    text = backlog_export.render_projection(_ROWS, generated_at=_GENERATED_AT)
    stamp = backlog_client.parse_generated_stamp(text)
    assert stamp == backlog_client.GeneratedStamp(
        rendered_at=_GENERATED_AT, row_count=len(_ROWS)
    )

    # ... and through the whole read path the console actually calls, so the
    # stamp's position in the file (inside the header scan bound) is pinned too.
    rendered = tmp_path / "VOYN_TASKS_BACKLOG.md"
    rendered.write_text(text, encoding="utf-8")
    projection = backlog_client.load_projection(rendered)
    assert projection.stamp == stamp
    # A file straight off the tick agrees with its own header by construction;
    # this is the baseline the panel's "edited after render" warning fires
    # against.
    assert backlog_client.stamp_matches_content(projection) is True


def test_an_empty_store_still_renders_a_readable_stamp(tmp_path):
    """The zero-row render is the one most likely to be mistaken for a broken
    file, so it has to be the most legible: a stamp saying "0 rows, just now"
    distinguishes an empty store from a dead tick, which is the whole
    distinction this header exists to make."""
    rendered = tmp_path / "empty.md"
    rendered.write_text(
        backlog_export.render_projection([], generated_at=_GENERATED_AT),
        encoding="utf-8",
    )
    projection = backlog_client.load_projection(rendered)
    assert projection.stamp == backlog_client.GeneratedStamp(
        rendered_at=_GENERATED_AT, row_count=0
    )
    assert projection.records == []
    assert backlog_client.stamp_matches_content(projection) is True


def test_the_generated_marker_is_the_line_the_header_actually_carries():
    """The marker is a constant read by the importer and interpolated by the
    exporter; if the header were reworded around it, the refusal would go
    quietly dead. Pins that the emitted file really contains it as its own
    line -- the exact form `is_generated_projection` matches."""
    lines = backlog_export.render_projection(
        [], generated_at=_GENERATED_AT
    ).splitlines()
    assert backlog_export.GENERATED_MARKER in lines
    position = lines.index(backlog_export.GENERATED_MARKER)
    assert position < backlog_client.HEADER_SCAN_LINES


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


def test_fetch_rows_orders_numeric_waves_numerically_not_lexically(pg_connection_factory):
    """`ORDER BY wave` alone sorts the column as TEXT, where `'10'` < `'2'`
    lexically -- the exact mistake `backlog_eligible` (0006_backlog_planner)
    already casts around for the planner's own dispatch order. A wave-10
    task rendered ahead of a wave-2 task would make this projection disagree
    with what the planner actually dispatched next, once a wave reaches two
    digits."""
    store = BacklogStore(pg_connection_factory)
    for task in [
        _task("VOYN-W0-FETCH-WAVE10", wave="10", priority="P0", status="OPEN"),
        _task("VOYN-W0-FETCH-WAVE2", wave="2", priority="P0", status="OPEN"),
        _task("VOYN-W0-FETCH-NAMED", wave="COM", priority="P0", status="OPEN"),
    ]:
        ok, reason, _ = store.upsert_task(task)
        assert ok, reason

    with pg_connection_factory() as conn:
        rows = backlog_export.fetch_rows(conn)

    ours = [
        row["task_id"]
        for row in rows
        if row["task_id"]
        in {"VOYN-W0-FETCH-WAVE10", "VOYN-W0-FETCH-WAVE2", "VOYN-W0-FETCH-NAMED"}
    ]
    assert ours == [
        "VOYN-W0-FETCH-WAVE2",
        "VOYN-W0-FETCH-WAVE10",
        "VOYN-W0-FETCH-NAMED",
    ]
