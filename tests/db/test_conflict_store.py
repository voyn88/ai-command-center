"""Slice 3 of the runtime migration: `conflict`'s PostgreSQL mirror.

Same dual-write shape as slice 2 — SQLite is the authority and must stay it —
with one property neither earlier slice had: a **nullable** `timestamptz`.
`resolved_at` is `NULL` on an open conflict and carries a timestamp once it
resolves, so both states are real and reconciliation must compare `None`
against `None` without calling it a difference.

The first version of this file also claimed a conflict can reopen and clear
that column again. It cannot — `resolved` is terminal — and the test offered as
evidence upserted two hand-built dicts, which is the "proved against data the
writer cannot emit" defect this migration has now produced twice. The claim is
pinned as a fact by `test_a_resolved_conflict_is_terminal` instead of being
asserted in prose.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.conflict_store import (
    CONFLICT_COLUMNS,
    MIRROR_UNAVAILABLE,
    PostgresConflictMirror,
    divergence,
)
from command_center.runtime.db import conflict as conflict_db

ROOT = Path(__file__).resolve().parents[2]


def _row(conflict_id: str, **overrides: object) -> dict:
    row = {
        "id": conflict_id,
        "kind": "merge",
        "source_ref": "incident:1",
        "severity": "sev3",
        "status": "open",
        "owner": None,
        "mitigation": None,
        "project_ref": None,
        "opened_at": "2026-08-13T00:00:00",  # naive local, what `models.iso_now()` emits
        "resolved_at": None,
        "version": 0,
        "created_at": "2026-08-13T00:00:00",
        "updated_at": "2026-08-13T00:00:00",
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


def _code_without_prose(function: object) -> str:
    """A function's executable code, with comments and docstrings removed.

    The guard below greps for `postgres`; the source text also contains the
    word in comments explaining why PostgreSQL is *not* consulted, so grepping
    raw source made the test fail on its own explanation. Prose is stripped so
    the assertion is about the code rather than about how it is described —
    which is what the test claims to check.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)  # comments never survive a parse/unparse round trip


@pytest.fixture
def mirror(pg_connection_factory) -> PostgresConflictMirror:
    return PostgresConflictMirror(connection_factory=pg_connection_factory)


# --- contract and authority -------------------------------------------------


def test_the_mirror_satisfies_the_row_oriented_contract() -> None:
    assert isinstance(
        PostgresConflictMirror(connection_factory=lambda: None), record_mirror.RecordMirror
    )
    assert PostgresConflictMirror.name == "postgres"


def test_sqlite_remains_the_authority_for_conflicts() -> None:
    """The dangerous outcome of this slice is a second system of record.

    The write paths must still write SQLite, and no read path may consult
    PostgreSQL to decide anything. Reads switch after reconciliation and the
    rollback and backup/restore drills — not as a side effect of a mirror
    landing.

    Its limit, stated rather than left to be discovered: this greps sources,
    so it catches a direct read and not one added through a helper — which is
    the very indirection the *write* now uses (`_mirror_conflict`). Independent
    review raised that; it is a weak guard kept for being cheap, not a proof.
    The read paths are additionally covered by every conflict test in the
    suite passing with no PostgreSQL available at all.
    """
    for function in (
        conflict_db.create_conflict,
        conflict_db.update_conflict_fields,
        conflict_db.set_conflict_status,
        conflict_db.get_conflict,
        conflict_db.get_conflict_by_source_ref,
        conflict_db.list_conflicts,
        conflict_db._conflict_transition,
    ):
        code = _code_without_prose(function)
        for marker in ("postgres", "conflict_store", "list_records"):
            assert marker not in code.lower(), f"{function.__name__}: {marker}"

    assert "INSERT INTO conflict" in inspect.getsource(conflict_db.create_conflict)
    for reader in (conflict_db.get_conflict, conflict_db.list_conflicts):
        assert "FROM conflict" in inspect.getsource(reader)


def test_the_column_list_matches_the_accepted_postgresql_schema() -> None:
    """The map is the contract; drifting from it silently is how a mirror ends
    up writing a column the target does not have."""
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    body = ddl.split("CREATE TABLE conflict (", 1)[1].split(");", 1)[0]
    declared = tuple(
        line.strip().split()[0]
        for line in body.strip().splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    assert declared == CONFLICT_COLUMNS


def test_the_mirror_covers_every_column_the_authority_writes() -> None:
    """A column the authority stores and the mirror omits is invisible: the
    reconciliation only compares what it is given, so the missing field would
    never be reported as divergence."""
    assert set(conflict_db._CONFLICT_COLUMNS) == set(CONFLICT_COLUMNS)


# --- behaviour against a real PostgreSQL ------------------------------------


def test_the_timestamp_gap_round_trips(mirror: PostgresConflictMirror) -> None:
    """Timestamps are TEXT here and `timestamptz` there. Unconverted, every row
    reads as different and the cutover gate is permanently red — which invites
    loosening the comparison instead of fixing the conversion."""
    mirror.upsert(_row("a"))

    stored = mirror.list_records()[0]

    assert stored["opened_at"] == "2026-08-13T00:00:00"
    assert isinstance(stored["opened_at"], str)


def test_a_null_resolved_at_survives_the_round_trip(mirror: PostgresConflictMirror) -> None:
    """The column is nullable and an open conflict has no resolution date.
    `None` must come back as `None`, not as an epoch or a rendered string."""
    mirror.upsert(_row("open-one"))

    assert mirror.list_records()[0]["resolved_at"] is None


def test_a_resolved_conflict_is_terminal(tmp_path) -> None:
    """The fact the first version of this slice got wrong, now pinned.

    Its acceptance story said `resolved_at` returns to `NULL` when a conflict
    reopens — read off the clearing branch in `_conflict_transition` without
    reading the allowlist above it, which makes that branch unreachable.
    Independent review disproved it by running the real writer.

    Pinned here rather than fixed in a docstring, because the next person to
    open `resolved -> open` needs this test to fail: whole-row upserts, the
    mirror's `resolved_at` handling and this assertion all move together.
    """
    db_path = tmp_path / "runtime.db"
    conflict_db.db.migrate(db_path)
    opened = conflict_db.create_conflict(db_path, kind="merge", source_ref="incident:1")
    resolved = conflict_db.set_conflict_status(
        db_path, opened["id"], expected_version=0, status="resolved"
    )

    assert conflict_db.CONFLICT_TRANSITIONS["resolved"] == frozenset()
    for attempt in ("open", "mitigating"):
        with pytest.raises(conflict_db.InvalidConflictTransitionError):
            conflict_db.set_conflict_status(
                db_path, resolved["id"], expected_version=1, status=attempt
            )


def test_an_upsert_replaces_columns_the_caller_did_not_change(
    mirror: PostgresConflictMirror,
) -> None:
    """Whole-row replacement, stated as a property of `upsert` and nothing more.

    This is what makes `update_conflict_fields` safe to mirror: it changes only
    the caller's fields plus `updated_at` and `version`, and hands over the
    whole row, because the mirror has no other source for the rest. The rows below are hand-built, so this proves the store's
    behaviour — not the authority's, which is
    `test_reconciliation_is_clean_for_rows_the_application_actually_wrote`.
    """
    mirror.upsert(_row("r", status="resolved", resolved_at="2026-08-13T10:00:00", version=1))
    assert mirror.list_records()[0]["resolved_at"] == "2026-08-13T10:00:00"

    mirror.upsert(_row("r", status="open", resolved_at=None, owner="ops", version=2))

    stored = mirror.list_records()[0]
    assert stored["resolved_at"] is None
    assert (stored["owner"], stored["version"]) == ("ops", 2)


def test_upsert_is_idempotent_and_updates_in_place(mirror: PostgresConflictMirror) -> None:
    # The backfill runs more than once by design; an insert-only mirror would
    # fail the second run on rows it wrote itself.
    row = _row("a")
    mirror.upsert(row)
    mirror.upsert(row)
    assert len(mirror.list_records()) == 1

    mirror.upsert(_row("a", status="mitigating", owner="ops", version=1))

    stored = mirror.list_records()
    assert len(stored) == 1
    assert (stored[0]["status"], stored[0]["owner"], stored[0]["version"]) == (
        "mitigating",
        "ops",
        1,
    )


def test_reconciliation_reports_agreement_and_every_shape_of_disagreement(
    mirror: PostgresConflictMirror,
) -> None:
    agreed = _row("same")
    mirror.upsert(agreed)
    assert divergence([agreed], mirror) == []

    mirror.upsert(_row("same", severity="sev1"))
    assert [entry["fields"] for entry in divergence([agreed], mirror)] == [["severity"]]

    missing = divergence([agreed, _row("absent")], mirror)
    assert {entry["id"] for entry in missing} >= {"absent"}

    # A mirror ahead of the system of record is the state no check would flag
    # if reconciliation only walked the authority.
    assert {entry["id"] for entry in divergence([], mirror)} == {"same"}


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    reported = divergence([_row("a")], Broken())

    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]
    assert "RuntimeError" in reported[0]["detail"]


def test_reconciliation_is_clean_for_rows_the_application_actually_wrote(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """End to end through the production path, not around it.

    The mirror is driven by `_mirror_conflict` here rather than by an `upsert`
    this test performs itself, because the production path swallows every
    exception: a store that failed on one of the three writes would leave the
    row unmirrored and a test that upserts by hand would still pass. So the
    authority is written and transitioned by the real functions, the mirror is
    filled by the real hook, and only then are the two compared. This is the
    assertion the cutover is gated on.

    Suggested by independent review, which pointed out that the earlier version
    of this test proved the store rather than the dual-write.

    What it does *not* prove, established by perturbation rather than assumed:
    dropping the mirror from `update_conflict_fields` leaves this test green,
    because the later transition mirrors the whole row and hides the gap. An
    end-state reconciliation cannot see an intermediate write that a subsequent
    one covers — that is
    `test_every_write_path_mirrors_the_committed_row`'s job, and it does fail
    on that perturbation.
    """
    from command_center.db import conflict_store

    monkeypatch.setattr(
        conflict_store,
        "PostgresConflictMirror",
        lambda: PostgresConflictMirror(connection_factory=pg_connection_factory),
    )
    mirror = PostgresConflictMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    conflict_db.db.migrate(db_path)

    opened = conflict_db.create_conflict(db_path, kind="merge", source_ref="incident:7")
    conflict_db.update_conflict_fields(
        db_path, opened["id"], expected_version=0, fields={"owner": "ops", "mitigation": "m"}
    )
    resolved = conflict_db.set_conflict_status(
        db_path, opened["id"], expected_version=1, status="resolved"
    )
    assert resolved["resolved_at"], "the premise: this row carries a real resolution date"

    assert divergence(conflict_db.list_conflicts(db_path), mirror) == []


def test_importing_the_store_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.conflict_store as s;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules;"
        "s.PostgresConflictMirror()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr


# --- the dual-write itself --------------------------------------------------


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    """During dual-write the mirror is not load-bearing. Letting it raise would
    mean a migration step could take down the very table it is migrating."""
    from command_center.db import conflict_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(conflict_store, "PostgresConflictMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    conflict_db.db.migrate(db_path)

    created = conflict_db.create_conflict(db_path, kind="perf", source_ref="incident:9")
    updated = conflict_db.update_conflict_fields(
        db_path, created["id"], expected_version=0, fields={"owner": "ops"}
    )

    assert conflict_db.get_conflict(db_path, created["id"])["owner"] == "ops"
    assert updated["owner"] == "ops"


def test_every_write_path_mirrors_the_committed_row(tmp_path, monkeypatch) -> None:
    """Ordering and coverage in one, because both failures look identical from
    outside: a mirror that disagrees with the authority.

    Recorded rather than asserted inside the callback. `_mirror_conflict`
    swallows every `Exception`, and `AssertionError` is one — an assertion in
    there is caught, logged at WARNING and lost, so the test would pass
    whatever it claimed. Independent review proved that on slice 2 by
    inverting the condition and watching the test still pass.
    """
    from command_center.db import conflict_store

    db_path = tmp_path / "runtime.db"
    conflict_db.db.migrate(db_path)
    observed: list[tuple[str, dict | None]] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            # What SQLite holds *now*: if this runs before the commit, the
            # authority still shows the previous row — or no row at all.
            observed.append((record["status"], conflict_db.get_conflict(db_path, record["id"])))

    monkeypatch.setattr(conflict_store, "PostgresConflictMirror", lambda: Recording())

    created = conflict_db.create_conflict(db_path, kind="budget", source_ref="incident:3")
    conflict_db.update_conflict_fields(
        db_path, created["id"], expected_version=0, fields={"owner": "ops", "mitigation": "m"}
    )
    conflict_db.set_conflict_status(db_path, created["id"], expected_version=1, status="resolved")

    # Three writes, three mirror calls: an unmirrored path is divergence that
    # only reconciliation would find, and only if someone ran it.
    assert [status for status, _ in observed] == ["open", "open", "resolved"]
    # Each one saw the committed row, and saw the row this very write produced.
    for status, authoritative in observed:
        assert authoritative is not None, "the mirror ran before the commit"
        assert authoritative["status"] == status
    assert observed[1][1]["owner"] == "ops"
    assert observed[2][1]["resolved_at"] is not None
