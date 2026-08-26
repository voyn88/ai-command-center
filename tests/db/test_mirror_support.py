"""The machinery the three PostgreSQL mirrors share (VOYN-W0-AICC-SRV-01B).

These are the properties that were previously restated in each store, and one
of them was restated *wrongly* — the timestamp conversion shipped broken in
both directions and cost a review round. Now there is one copy, so this is the
one place that has to hold it down. Pure functions over `datetime` and dicts:
no PostgreSQL, so this file runs on a laptop with no server and no Docker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from command_center.db.mirror_support import (
    MIRROR_UNAVAILABLE,
    ColumnCodec,
    divergence,
    render_authority_timestamp,
    to_instant,
)

# --- the conversion that was wrong once -------------------------------------


def test_a_naive_timestamp_is_read_in_the_writers_zone() -> None:
    """Naive text handed to `timestamptz` is stamped with the *session* zone.

    That is silent: no error, every row shifted by the gap between the writing
    machine and the server. The zone is attached here instead, so the instant
    stored is the one the writer meant.
    """
    written = "2026-08-13T12:00:00"

    attached = to_instant(written)

    assert attached.tzinfo is not None
    assert attached == datetime.fromisoformat(written).astimezone()


def test_an_already_aware_timestamp_keeps_its_own_offset() -> None:
    """Not every timestamp in the application is naive, and re-stamping an
    aware one with the local zone would move an instant that was unambiguous."""
    aware = "2026-08-13T12:00:00+05:00"

    assert to_instant(aware).utcoffset() == timedelta(hours=5)


def test_the_render_reproduces_exactly_what_the_application_writes() -> None:
    """The regression test for the defect that reached `main`.

    An earlier render emitted UTC with a `Z` suffix "matching what the
    application writes". It does not — `models.iso_now()` writes naive local
    time at second precision — so `divergence` called every row different: a
    cutover gate permanently red, which invites loosening the comparison.
    """
    from command_center import models

    written = models.iso_now()
    assert "+" not in written and not written.endswith("Z")  # guard the premise

    assert render_authority_timestamp(to_instant(written)) == written


def test_the_render_survives_a_mirror_read_in_another_zone() -> None:
    """`timestamptz` comes back in the session's zone, not the writer's. The
    render converts to local first, so the same instant renders identically
    however the server chose to present it."""
    written = "2026-08-13T12:00:00"
    instant = to_instant(written)

    elsewhere = instant.astimezone(timezone(timedelta(hours=-7)))

    assert render_authority_timestamp(elsewhere) == written


# --- the per-column codec ---------------------------------------------------


CODEC = ColumnCodec(timestamps=frozenset({"created_at"}), flags=frozenset({"done"}))


def test_flags_and_timestamps_round_trip_through_their_column_types() -> None:
    assert CODEC.to_column("done", 1) is True
    assert CODEC.to_authority("done", True) == 1
    assert isinstance(CODEC.to_authority("done", True), int)

    stored = CODEC.to_column("created_at", "2026-08-13T00:00:00")
    assert CODEC.to_authority("created_at", stored) == "2026-08-13T00:00:00"


def test_a_null_stays_null_in_both_directions() -> None:
    """`resolved_at` is NULL until a conflict resolves, and `bool(None)` is
    `False` — a flag converted without this check would turn "unknown" into
    "no"."""
    for name in ("done", "created_at"):
        assert CODEC.to_column(name, None) is None
        assert CODEC.to_authority(name, None) is None


def test_columns_the_table_did_not_name_pass_through_untouched() -> None:
    """`owner_item.due` and `digest_item.day` are deliberately `text` on both
    sides — free user input, not dates. A codec that guessed from the column
    name would convert them and lose whatever the user typed."""
    assert CODEC.to_column("due", "someday") == "someday"
    assert CODEC.to_authority("due", "someday") == "someday"
    assert CODEC.to_column("version", 3) == 3


def test_an_empty_timestamp_is_not_parsed() -> None:
    """Pass-through, and the reason is narrower than it first looks.

    An earlier version of this docstring said the guard prevents a silently
    unmirrored row, since `datetime.fromisoformat("")` raises on a write path
    that swallows exceptions. Independent review disproved it by execution:
    PostgreSQL then rejects `""` for `timestamptz` and *that* is swallowed too,
    so the guard moves the failure between layers rather than removing it.

    What the guard actually buys is that the codec invents no rule for data no
    writer produces — every mirrored timestamp column is written by
    `models.iso_now()`, which never emits `""`. Mapping `""` to `NULL` here
    would be a guess about a case that does not exist, and guessing at absent
    cases is how the wrong conversion reached `main` in slice 1. The real gap —
    that any unmirrorable value is lost silently — is
    `VOYN-W0-AICC-MIRROR-SILENT-DROP`.
    """
    assert CODEC.to_column("created_at", "") == ""


# --- the jsonb rule ---------------------------------------------------------


JSON_CODEC = ColumnCodec(json_values=frozenset({"refs_json"}))


def test_json_columns_keep_their_text_on_the_way_in() -> None:
    """The store casts with `%s::jsonb`, so the codec hands over the
    authority's own text rather than a re-serialisation of it — and stays free
    of the driver import `command_center.db.__init__` forbids."""
    assert JSON_CODEC.to_column("refs_json", '["a"]') == '["a"]'


def test_unparseable_json_raises_and_names_the_column() -> None:
    """The map requires unparseable text to break the insert rather than reach
    `jsonb`. Refused here, where the error can say which column — the same
    rejection from PostgreSQL arrives as a driver error about a statement."""
    with pytest.raises(ValueError, match="refs_json"):
        JSON_CODEC.to_column("refs_json", "not json")


def test_json_columns_are_compared_as_parsed_values() -> None:
    """`jsonb` is not byte-stable — PostgreSQL returns `{"b":1,"a":2}` with its
    own key order — so text comparison would report every object-valued row as
    different."""
    assert JSON_CODEC.comparable("refs_json", '{"b": 1, "a": 2}') == {"a": 2, "b": 1}
    assert JSON_CODEC.comparable("refs_json", {"a": 2, "b": 1}) == {"a": 2, "b": 1}


def test_only_declared_json_columns_are_parsed() -> None:
    """Parsing a column the target stores as text would make two rows agree on
    a value that differs — a false clean, which is worse than a false
    difference because nothing follows up on it."""
    assert JSON_CODEC.comparable("title", '{"a": 1}') == '{"a": 1}'


def test_unparseable_authority_text_compares_as_itself() -> None:
    """`to_column` refuses such text, so no unparseable value reaches the
    mirror and the row is reported divergent — which is what an unmirrorable
    row is."""
    assert JSON_CODEC.comparable("refs_json", "not json") == "not json"


def test_a_json_string_scalar_collides_with_unparseable_text() -> None:
    """The counterexample to the flat version of the claim above, pinned.

    A `jsonb` column may hold a JSON string scalar, which the driver returns as
    a plain `str`; `comparable` sees two values and no provenance, so mirror
    `"not json"` — valid `jsonb` — compares equal to authority text `not json`,
    which is unmirrorable. A false clean, and the docstring used to deny it
    could happen. Unreachable for every column mirrored today, all of which are
    written by `json.dumps`; tracked as
    `VOYN-W0-AICC-MIRROR-JSON-SCALAR-AMBIGUITY`.

    Asserted rather than described so the day it is fixed, this fails and says
    so instead of quietly agreeing.
    """
    from_mirror = "not json"  # what psycopg returns for the jsonb value '"not json"'
    from_authority = "not json"  # text that is not JSON at all

    assert JSON_CODEC.comparable("refs_json", from_mirror) == JSON_CODEC.comparable(
        "refs_json", from_authority
    )


# --- reconciliation ---------------------------------------------------------


COLUMNS = ("id", "title", "done")


def _row(row_id: str, **overrides: object) -> dict:
    return {"id": row_id, "title": f"row {row_id}", "done": 0, **overrides}


class _Mirror:
    name = "postgres"

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_records(self) -> list[dict]:
        return list(self._rows)


def test_agreement_reports_nothing() -> None:
    row = _row("a")
    assert divergence([row], _Mirror([row]), COLUMNS) == []


def test_a_round_tripped_boolean_is_not_a_difference() -> None:
    """SQLite hands back the integer it stores; the mirror renders a boolean
    back to 0/1, but a caller comparing raw driver output would otherwise see
    `True != 1` on every row."""
    assert divergence([_row("a", done=1)], _Mirror([_row("a", done=True)]), COLUMNS) == []


def test_every_shape_of_disagreement_is_reported() -> None:
    authority = [_row("same"), _row("absent")]
    mirror = _Mirror([_row("same", title="drifted"), _row("ahead")])

    reported = {entry["id"]: entry for entry in divergence(authority, mirror, COLUMNS)}

    assert reported["same"]["fields"] == ["title"]
    # Missing from the mirror: the ordinary stale-mirror case.
    assert reported["absent"]["mirror"] is None
    # Present in the mirror and not in the authority. A mirror *ahead* of the
    # system of record is the state nothing else flags, and a reconciliation
    # that only walked the authority would call this session clean.
    assert reported["ahead"]["authority"] is None


def test_only_the_named_columns_are_compared() -> None:
    """The queue's `position` is the standing example: a column the mirror must
    store and the authority gets for free. Comparing it would report a
    difference that exists only because the target needed it."""
    assert divergence([_row("a")], _Mirror([_row("a", position=4)]), COLUMNS) == []


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    """The cutover is gated on a session with no divergence. An absent store
    has nothing to disagree with, so `[]` here would let the migration advance
    on the strength of a store nobody wrote."""

    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    reported = divergence([_row("a")], Broken(), COLUMNS)

    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]
    assert "RuntimeError" in reported[0]["detail"]


def test_reconciliation_accepts_a_generator_of_authority_rows() -> None:
    """The backfill streams; materialising the authority to reconcile it would
    put the table this migration exists to move into one process's memory."""
    assert divergence((_row(str(n)) for n in range(3)), _Mirror([]), COLUMNS) != []


def test_importing_the_shared_machinery_needs_no_postgresql_client() -> None:
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import sys;"
        "import command_center.db.mirror_support as m;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )
    assert result.returncode == 0, result.stderr
