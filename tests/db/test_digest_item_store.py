"""Slice 4 of the runtime migration: `digest_item`'s PostgreSQL mirror.

Two properties no earlier slice had, both declared blockers before this slice
started rather than found inside it:

* a `jsonb` column, whose round trip is **not** byte-stable, so it cannot be
  reconciled as text;
* a delete path, because the digest engine rebuilds a day by deleting it and
  re-inserting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.digest_item_store import (
    DIGEST_ITEM_COLUMNS,
    MIRROR_UNAVAILABLE,
    PostgresDigestItemMirror,
    divergence,
)
from command_center.runtime.db import wave1

ROOT = Path(__file__).resolve().parents[2]


def _row(item_id: str, **overrides: object) -> dict:
    row = {
        "id": item_id,
        "title": f"item {item_id}",
        "body": "",
        "category": None,
        "refs_json": '["task:1"]',
        "created_at": "2026-08-14T00:00:00",  # naive local, what `models.iso_now()` emits
        "day": "2026-08-14",
        "position": 0,
        "project_ref": None,
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


@pytest.fixture
def mirror(pg_connection_factory) -> PostgresDigestItemMirror:
    return PostgresDigestItemMirror(connection_factory=pg_connection_factory)


# --- contract and authority -------------------------------------------------


def test_the_mirror_satisfies_the_row_oriented_contract() -> None:
    assert isinstance(
        PostgresDigestItemMirror(connection_factory=lambda: None), record_mirror.RecordMirror
    )
    assert PostgresDigestItemMirror.name == "postgres"


def test_the_column_list_matches_the_accepted_postgresql_schema() -> None:
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    body = ddl.split("CREATE TABLE digest_item (", 1)[1].split(");", 1)[0]
    declared = tuple(
        line.strip().split()[0]
        for line in body.strip().splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    assert declared == DIGEST_ITEM_COLUMNS


def test_the_mirror_covers_every_column_the_authority_writes() -> None:
    """Column *order* differs between the two schemas and that is fine — the
    mirror names its columns. A column the authority writes and the mirror
    omits is the real hazard: reconciliation compares only what it is given, so
    the missing field would never be reported."""
    assert set(wave1._DIGEST_ITEM_COLUMNS) == set(DIGEST_ITEM_COLUMNS)


# --- jsonb ------------------------------------------------------------------


def test_json_text_round_trips_through_a_jsonb_column(mirror: PostgresDigestItemMirror) -> None:
    """The authority stores `json.dumps(...)` text; the column is `jsonb`.

    psycopg adapts neither `list` nor `dict` to `jsonb`, so the store casts the
    authority's own text — and reads back the parsed value, which is what the
    driver hands over for a `jsonb` column.
    """
    mirror.upsert(_row("a", refs_json='["task:1", "task:2"]'))

    stored = mirror.list_records()[0]

    assert stored["refs_json"] == ["task:1", "task:2"]


def test_non_ascii_references_survive_the_round_trip(mirror: PostgresDigestItemMirror) -> None:
    """`create_digest_item` writes with `ensure_ascii=False`, so the text
    reaching the mirror is genuinely non-ASCII rather than escaped."""
    refs = ["задача:1"]
    mirror.upsert(_row("a", refs_json=json.dumps(refs, ensure_ascii=False)))

    assert mirror.list_records()[0]["refs_json"] == refs


def test_reconciliation_ignores_the_key_order_postgresql_chose(
    mirror: PostgresDigestItemMirror,
) -> None:
    """The blocker this slice exists to close, demonstrated rather than argued.

    `jsonb` does not preserve the source bytes: PostgreSQL 17.6 stores
    `{"b": 1, "a": 2}` and returns it as `{"a": 2, "b": 1}`. Compared as text
    that is a difference on every object-valued row — a cutover gate
    permanently red, which somebody eventually satisfies by loosening the
    comparison. Compared as parsed values it is agreement, which is what it is.
    """
    authority = _row("a", refs_json='{"b": 1, "a": 2}')
    mirror.upsert(authority)

    stored = mirror.list_records()[0]

    # The premise: the bytes really did change. If this ever stops holding, the
    # test below stops proving anything and should be revisited, not deleted.
    assert json.dumps(stored["refs_json"]) != authority["refs_json"]
    assert divergence([authority], mirror) == []


def test_a_genuinely_different_reference_list_is_still_reported(
    mirror: PostgresDigestItemMirror,
) -> None:
    """Comparing parsed values must not turn into comparing nothing."""
    mirror.upsert(_row("a", refs_json='["task:1"]'))

    reported = divergence([_row("a", refs_json='["task:2"]')], mirror)

    assert [entry["fields"] for entry in reported] == [["refs_json"]]


def test_unparseable_json_is_refused_rather_than_stored(
    mirror: PostgresDigestItemMirror,
) -> None:
    """The accepted map requires unparseable text to break the insert instead
    of reaching `jsonb`. It is refused in Python, where the error can name the
    column, rather than being left to arrive as a driver error about a
    statement."""
    with pytest.raises(ValueError, match="refs_json"):
        mirror.upsert(_row("bad", refs_json="not json"))

    assert mirror.list_records() == []


# --- deletes ----------------------------------------------------------------


def test_deleting_a_day_removes_exactly_that_day(mirror: PostgresDigestItemMirror) -> None:
    mirror.upsert(_row("keep", day="2026-08-13"))
    mirror.upsert(_row("drop", day="2026-08-14"))

    mirror.delete_day("2026-08-14")

    assert [row["id"] for row in mirror.list_records()] == ["keep"]


def test_a_rebuilt_day_does_not_leave_the_mirror_ahead(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The reason the delete path had to be mirrored at all.

    The digest engine rebuilds a day by deleting it and re-inserting, so a
    mirror that only upserts keeps every superseded row. Reconciliation would
    report it permanently ahead of the authority — true, useless, and exactly
    the standing noise that gets a check switched off.

    Driven through the real `_mirror_digest_item` / `_mirror_digest_day_deletion`
    hooks, not by calling the store directly: the production path swallows
    every exception, so a mirror that failed would still let a hand-driven test
    pass.
    """
    from command_center.db import digest_item_store

    monkeypatch.setattr(
        digest_item_store,
        "PostgresDigestItemMirror",
        lambda: PostgresDigestItemMirror(connection_factory=pg_connection_factory),
    )
    mirror = PostgresDigestItemMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    wave1.create_digest_item(db_path, title="first build", day="2026-08-14", refs=["task:1"])
    wave1.delete_digest_items_for_day(db_path, "2026-08-14")
    wave1.create_digest_item(db_path, title="second build", day="2026-08-14", refs=["task:2"])

    authority = wave1.list_digest_items_for_day(db_path, "2026-08-14")
    assert [row["title"] for row in authority] == ["second build"]
    assert [row["title"] for row in mirror.list_records()] == ["second build"]


# --- the dual-write itself --------------------------------------------------


def test_the_mirror_receives_the_row_the_authority_stored(tmp_path, monkeypatch) -> None:
    """The trap this table sets, and the reason the hook takes `record`.

    `create_digest_item` returns `_decode_digest_row(record)`, which *pops*
    `refs_json` and puts a decoded `refs` list in its place. Mirroring the
    return value would write the column's default on every row — a mirror that
    agrees with the authority on everything except the one column this slice
    exists to migrate.
    """
    from command_center.db import digest_item_store

    observed: list[dict] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            observed.append(dict(record))

    monkeypatch.setattr(digest_item_store, "PostgresDigestItemMirror", lambda: Recording())

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    returned = wave1.create_digest_item(db_path, title="t", refs=["task:1"], day="2026-08-14")

    assert "refs_json" not in returned, "the premise: the returned row has no JSON column"
    assert observed[0]["refs_json"] == '["task:1"]'


def test_every_write_path_mirrors_after_the_authoritative_commit(tmp_path, monkeypatch) -> None:
    """Ordering and coverage together, recorded rather than asserted inside the
    callbacks — both hooks swallow every `Exception`, and `AssertionError` is
    one, so an assertion in there would be caught and lost."""
    from command_center.db import digest_item_store

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)
    observed: list[tuple[str, int]] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            # How many rows the *authority* holds for the day at this moment.
            observed.append(("upsert", len(wave1.list_digest_items_for_day(db_path, "2026-08-14"))))

        def delete_day(self, day: str) -> None:
            observed.append(("delete", len(wave1.list_digest_items_for_day(db_path, day))))

    monkeypatch.setattr(digest_item_store, "PostgresDigestItemMirror", lambda: Recording())

    wave1.create_digest_item(db_path, title="a", day="2026-08-14")
    wave1.delete_digest_items_for_day(db_path, "2026-08-14")
    wave1.create_digest_item(db_path, title="b", day="2026-08-14")

    # Both paths mirror, in order, and each ran after its own commit: the
    # authority already had the inserted row, and already had none left when
    # the deletion was mirrored.
    assert observed == [("upsert", 1), ("delete", 0), ("upsert", 1)]


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import digest_item_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_day(self, day: str) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(digest_item_store, "PostgresDigestItemMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    created = wave1.create_digest_item(db_path, title="survives", day="2026-08-14")
    removed = wave1.delete_digest_items_for_day(db_path, "2026-08-14")

    assert created["title"] == "survives"
    assert removed == 1
    assert wave1.list_digest_items_for_day(db_path, "2026-08-14") == []


# --- reconciliation and packaging -------------------------------------------


def test_reconciliation_reports_agreement_and_every_shape_of_disagreement(
    mirror: PostgresDigestItemMirror,
) -> None:
    agreed = _row("same")
    mirror.upsert(agreed)
    assert divergence([agreed], mirror) == []

    mirror.upsert(_row("same", title="drifted"))
    assert [entry["fields"] for entry in divergence([agreed], mirror)] == [["title"]]

    missing = divergence([agreed, _row("absent")], mirror)
    assert {entry["id"] for entry in missing} >= {"absent"}

    assert {entry["id"] for entry in divergence([], mirror)} == {"same"}


def test_reconciliation_is_clean_for_rows_the_application_actually_wrote(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The assertion the cutover is gated on, and the one this slice shipped
    without.

    Slices 2 and 3 each have it; slice 4 did not, and independent review found
    that the omission hid a trap rather than a gap. The obvious ways to obtain
    authority rows both break here: `create_digest_item` returns a decoded row,
    and every public list reader decodes too. Reconciling against either
    reports 100% divergence on `refs_json` — the permanently-red gate, reached
    not through a wrong conversion but through a reconciliation pointed at the
    wrong shape.

    So this runs against `list_digest_items_stored`, which exists for exactly
    this, and the next test pins why nothing else will do.
    """
    from command_center.db import digest_item_store

    monkeypatch.setattr(
        digest_item_store,
        "PostgresDigestItemMirror",
        lambda: PostgresDigestItemMirror(connection_factory=pg_connection_factory),
    )
    mirror = PostgresDigestItemMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)
    wave1.create_digest_item(
        db_path, title="reconciles", refs=["task:1", "task:2"], day="2026-08-14", position=1
    )
    wave1.create_digest_item(db_path, title="no refs", day="2026-08-14", position=2)

    assert divergence(wave1.list_digest_items_stored(db_path), mirror) == []


def test_reconciling_against_a_decoded_reader_is_not_clean(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The trap, pinned so it cannot be walked into twice.

    A future reader of this suite will reach for `list_digest_items_for_day` —
    it is the public reader, and it is what an operator wiring the cutover gate
    would find first. It reports every row divergent, because
    `_decode_digest_row` pops `refs_json`. The failure is loud, but it looks
    like a broken mirror rather than a wrong question, and the tempting fix is
    to loosen the comparison — which is the exact failure mode this table's
    mirror exists to prevent.
    """
    from command_center.db import digest_item_store

    monkeypatch.setattr(
        digest_item_store,
        "PostgresDigestItemMirror",
        lambda: PostgresDigestItemMirror(connection_factory=pg_connection_factory),
    )
    mirror = PostgresDigestItemMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)
    wave1.create_digest_item(db_path, title="t", refs=["task:1"], day="2026-08-14")

    decoded = wave1.list_digest_items_for_day(db_path, "2026-08-14")
    assert "refs_json" not in decoded[0]  # the premise

    reported = divergence(decoded, mirror)

    assert [entry["fields"] for entry in reported] == [["refs_json"]]


def test_sqlite_remains_the_authority_for_digest_items() -> None:
    """Parity with slices 2 and 3, which this slice also shipped without.

    Same limit as theirs, stated for the same reason: this greps code with
    prose stripped, so it catches a direct read and not one added through a
    helper — the indirection the write itself uses.
    """
    import ast
    import inspect
    import textwrap

    def code_without_prose(function: object) -> str:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    node.body.pop(0)
        return ast.unparse(tree)

    for function in (
        wave1.create_digest_item,
        wave1.delete_digest_items_for_day,
        wave1.get_digest_item,
        wave1.list_digest_items,
        wave1.list_digest_items_for_day,
        wave1.list_digest_items_stored,
    ):
        code = code_without_prose(function)
        for marker in ("postgres", "digest_item_store", "list_records"):
            assert marker not in code.lower(), f"{function.__name__}: {marker}"

    assert "INSERT INTO digest_item" in inspect.getsource(wave1.create_digest_item)
    assert "FROM digest_item" in inspect.getsource(wave1.list_digest_items_stored)


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    reported = divergence([_row("a")], Broken())

    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]


def test_importing_the_store_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.digest_item_store as s;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules;"
        "s.PostgresDigestItemMirror()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr
