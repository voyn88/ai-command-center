"""The PG→markdown projection renders exactly what the projection reader
parses (VOYN-W0-AICC-BACKLOG-EXPORT-PROJECTION)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from command_center import backlog_client
from command_center.db import backlog_export

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
    assert first.status == "OPEN"
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
