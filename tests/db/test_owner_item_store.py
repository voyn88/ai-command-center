"""Slice 2 of the runtime migration: `owner_item`'s PostgreSQL mirror.

Slice 2 is the harder shape. `queue_entry`'s authority was already JSON, so
slice 1 added a mirror beside an existing one and moved nothing. Here SQLite
*is* the authority, so the claim under test is that a dual-write leaves it that
way — and that the reconciliation which will eventually gate the cutover can
actually compare the two stores.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.owner_item_store import (
    MIRROR_UNAVAILABLE,
    OWNER_ITEM_COLUMNS,
    PostgresOwnerItemMirror,
    divergence,
)
from command_center.runtime.db import wave1
from tests.db.mirror_probe import each_lost_write_is_noticed

ROOT = Path(__file__).resolve().parents[2]


def _patch(monkeypatch, factory) -> None:
    from command_center.db import owner_item_store

    # The real class, captured before the patch: reading it back through the
    # module inside the lambda would resolve to the lambda itself, the mistake
    # this exact shape has caught in the `run`/`execution` families already.
    monkeypatch.setattr(
        owner_item_store,
        "PostgresOwnerItemMirror",
        lambda: PostgresOwnerItemMirror(connection_factory=factory),
    )


def _row(item_id: str, **overrides: object) -> dict:
    row = {
        "id": item_id,
        "title": f"item {item_id}",
        "detail": None,
        "due": None,
        "done": 0,
        "source_ref": None,
        "version": 0,
        "created_at": "2026-08-13T00:00:00",  # naive local, exactly what `models.iso_now()` emits
        "updated_at": "2026-08-13T00:00:00",
        "project_ref": None,
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


@pytest.fixture
def mirror(pg_connection_factory) -> PostgresOwnerItemMirror:
    return PostgresOwnerItemMirror(connection_factory=pg_connection_factory)


# --- contract and authority -------------------------------------------------


def test_the_mirror_satisfies_the_row_oriented_contract() -> None:
    assert isinstance(
        PostgresOwnerItemMirror(connection_factory=lambda: None), record_mirror.RecordMirror
    )
    assert PostgresOwnerItemMirror.name == "postgres"


def test_sqlite_remains_the_authority_for_owner_items() -> None:
    """The dangerous outcome of this slice is a second system of record.

    `create_owner_item` and `set_owner_item_done` must still write SQLite, and
    must not read PostgreSQL to decide anything. Read paths are switched only
    after reconciliation and the rollback and backup/restore drills — not as a
    side effect of a mirror landing.
    """
    for function in (wave1.create_owner_item, wave1.get_owner_item, wave1.list_owner_items):
        source = inspect.getsource(function)
        for marker in ("postgres", "owner_item_store", "list_records"):
            assert marker not in source.lower(), f"{function.__name__}: {marker}"

    assert "INSERT INTO owner_item" in inspect.getsource(wave1.create_owner_item)


def test_the_column_list_matches_the_accepted_postgresql_schema() -> None:
    """The map is the contract; drifting from it silently is how a mirror ends
    up writing a column the target does not have."""
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    body = ddl.split("CREATE TABLE owner_item (", 1)[1].split(");", 1)[0]
    declared = tuple(
        line.strip().split()[0]
        for line in body.strip().splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    assert declared == OWNER_ITEM_COLUMNS


# --- behaviour against a real PostgreSQL ------------------------------------


def test_the_boolean_and_timestamp_gaps_round_trip(mirror: PostgresOwnerItemMirror) -> None:
    """`done` is INTEGER here and boolean there; timestamps are TEXT and
    timestamptz. Unconverted, every row reads as different and the cutover gate
    is permanently red — which invites loosening the comparison."""
    mirror.upsert(_row("a", done=1))

    stored = mirror.list_records()[0]

    assert stored["done"] == 1 and isinstance(stored["done"], int)
    assert stored["created_at"] == "2026-08-13T00:00:00"
    assert isinstance(stored["created_at"], str)


def test_upsert_is_idempotent_and_updates_in_place(mirror: PostgresOwnerItemMirror) -> None:
    # The backfill runs more than once by design; an insert-only mirror would
    # fail the second run on rows it wrote itself.
    row = _row("a")
    mirror.upsert(row)
    mirror.upsert(row)
    assert len(mirror.list_records()) == 1

    mirror.upsert(_row("a", done=1, version=1, title="renamed"))

    stored = mirror.list_records()
    assert len(stored) == 1
    assert (stored[0]["done"], stored[0]["version"], stored[0]["title"]) == (1, 1, "renamed")


def test_reconciliation_reports_agreement_and_every_shape_of_disagreement(
    mirror: PostgresOwnerItemMirror,
) -> None:
    agreed = _row("same")
    mirror.upsert(agreed)
    assert divergence([agreed], mirror) == []

    # Field-level difference.
    mirror.upsert(_row("same", title="drifted"))
    reported = divergence([agreed], mirror)
    assert [entry["fields"] for entry in reported] == [["title"]]

    # Missing from the mirror.
    missing = divergence([agreed, _row("absent")], mirror)
    assert {entry["id"] for entry in missing} >= {"absent"}

    # Present in the mirror but not in the authority: a mirror ahead of the
    # system of record is the state no check would flag if reconciliation only
    # walked the authority.
    ahead = divergence([], mirror)
    assert {entry["id"] for entry in ahead} == {"same"}


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    """The cutover is gated on a session with no divergence. An absent store
    has nothing to disagree with, so returning `[]` here would let the
    migration advance on the strength of a store nobody wrote."""

    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    reported = divergence([_row("a")], Broken())

    assert [entry["id"] for entry in reported] == [MIRROR_UNAVAILABLE]
    assert "RuntimeError" in reported[0]["detail"]


def test_importing_the_store_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.owner_item_store as s;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules;"
        "s.PostgresOwnerItemMirror()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    """During dual-write the mirror is not load-bearing.

    Letting it raise would mean a migration step could take down the very table
    it is migrating. The mirror's health is reported by `divergence`, not by
    exceptions on the write path.
    """
    from command_center.db import owner_item_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(owner_item_store, "PostgresOwnerItemMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    created = wave1.create_owner_item(db_path, title="survives")

    assert wave1.get_owner_item(db_path, created["id"])["title"] == "survives"


def test_the_authoritative_write_happens_before_the_mirror(tmp_path, monkeypatch) -> None:
    """Order is not cosmetic. A mirror written first can end up ahead of the
    system of record, which no reconciliation flags as wrong — whereas a stale
    mirror is exactly what `divergence` exists to surface."""
    from command_center.db import owner_item_store

    observed: list[str] = []
    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    class Recording:
        def upsert(self, record: dict) -> None:
            # Recorded, not asserted. `_mirror_owner_item` swallows every
            # `Exception`, and `AssertionError` is one — an assertion in here
            # is caught, logged at DEBUG and lost, so the test would pass
            # whatever it claimed. Independent review proved that by inverting
            # this condition and watching the test still pass. The observation
            # has to escape the swallowing frame to mean anything.
            observed.append(wave1.get_owner_item(db_path, record["id"]) is not None)

    monkeypatch.setattr(owner_item_store, "PostgresOwnerItemMirror", lambda: Recording())

    wave1.create_owner_item(db_path, title="ordered")

    assert observed == [True], "the authoritative row must be readable before the mirror runs"


def test_a_real_iso_now_timestamp_round_trips(mirror: PostgresOwnerItemMirror) -> None:
    """The test that would have caught the blocker: use what the app writes.

    `models.iso_now()` returns *naive local time* — no offset, second
    precision. The first version of this mirror rendered UTC with a `Z` suffix
    and the tests fabricated `Z`-suffixed inputs to match, so the conversion was
    proved against data no writer in this application emits. Against real
    output, `divergence` would have reported every row as different — the
    permanently-red gate the module docstring warns about.
    """
    from command_center import models

    written = models.iso_now()
    assert "+" not in written and not written.endswith("Z")  # guard the premise

    mirror.upsert(_row("real", created_at=written, updated_at=written))

    stored = mirror.list_records()[0]
    assert stored["created_at"] == written
    assert stored["updated_at"] == written


def test_reconciliation_is_clean_for_a_row_the_application_actually_wrote(
    mirror: PostgresOwnerItemMirror, tmp_path
) -> None:
    """End to end against the authority, not against a fixture's idea of it.

    A row created through `wave1.create_owner_item` and mirrored must reconcile
    with zero divergence. This is the assertion the cutover is gated on, so it
    has to run on a row the real writer produced.
    """
    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)
    created = wave1.create_owner_item(db_path, title="reconciles", detail="d")

    mirror.upsert(created)

    assert divergence([created], mirror) == []


def test_the_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """`owner_item` has two write paths, not one: `create_owner_item` inserts
    and `set_owner_item_done` updates in place, and each calls `_mirror_owner_item`
    on its own. A reconciliation exercised only through `create_owner_item` —
    which is all the suite checked before this test — proves nothing about
    whether the second path still calls the mirror at all.

    Unlike `test_reconciliation_is_clean_for_a_row_the_application_actually_wrote`,
    this drives the mirror through the *real* dual-write hook (`_mirror_owner_item`,
    patched to the test's PostgreSQL rather than upserting by hand), so a mirror
    call deleted from either path is a divergence here, not silence.
    """
    _patch(monkeypatch, pg_connection_factory)
    mirror = PostgresOwnerItemMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert divergence(wave1.list_owner_items(db_path), mirror) == [], stage

    created = wave1.create_owner_item(db_path, title="mirror me")
    reconciled("owner_item created")

    wave1.set_owner_item_done(
        db_path, created["id"], expected_version=created["version"], done=True
    )
    reconciled("owner_item done toggled")


def test_every_lost_mirror_write_is_visible_to_reconciliation(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The lost-write probe, applied to `owner_item`'s two call sites.

    This module already imports `each_lost_write_is_noticed` for this test: the
    other tests drive `_mirror_owner_item` and then read the mirror they wrote,
    so none of them fails a write and asks whether the *absence* is noticed.
    The probe does — failing each call site's write in turn and checking
    reconciliation after each run.

    Both paths upsert the *whole* row, so a one-row create-then-done scenario
    cannot prove `create_owner_item`'s call site is covered: `set_owner_item_done`'s
    upsert writes the complete authoritative row and heals a lost create before
    post-scenario `noticed()` runs. This scenario uses two rows instead, each
    mirrored exactly once — `untouched` only through `create_owner_item`, and
    `finished` with its create's dual-write suppressed so its only mirror write
    is `set_owner_item_done`'s. Neither loss has a later same-row write to heal
    it, so `noticed()` observes the mirror after the scenario as the probe
    contract requires.

    `pg_connection_factory` is per-test (fresh database via `pg_database`). The
    DELETE between scenario runs is scoped to this probe's two fixed ids so it
    cannot wipe unrelated rows even if the fixture scope ever widened.
    """
    from command_center.db import owner_item_store

    untouched_id = "probe-untouched"
    finished_id = "probe-finished"
    mirror = PostgresOwnerItemMirror(connection_factory=pg_connection_factory)
    runs = 0
    db_path: Path = tmp_path / "unset.db"

    def scenario() -> None:
        nonlocal runs, db_path
        with pg_connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM owner_item WHERE id = ANY(%s)",
                    ([untouched_id, finished_id],),
                )
        db_path = tmp_path / f"runtime-{runs}.db"
        runs += 1
        wave1.db.migrate(db_path)
        # untouched: created once and never written again, so losing its
        # create is never healed by a later write to the same row.
        wave1.create_owner_item(db_path, title="untouched", item_id=untouched_id)
        # finished: its own create is mirrored with the dual-write suppressed,
        # so the row's *only* mirror write is set_owner_item_done's — losing
        # that one is never healed either. Restored by hand (not monkeypatch)
        # so the suppress cannot nest with the probe's own monkeypatch stack.
        real_mirror_hook = wave1._mirror_owner_item
        wave1._mirror_owner_item = lambda record: None
        try:
            finished = wave1.create_owner_item(
                db_path, title="finished", item_id=finished_id
            )
        finally:
            wave1._mirror_owner_item = real_mirror_hook
        wave1.set_owner_item_done(
            db_path, finished["id"], expected_version=finished["version"], done=True
        )

    def noticed() -> bool:
        return bool(divergence(wave1.list_owner_items(db_path, limit=1000), mirror))

    targets = (
        (
            owner_item_store,
            ("PostgresOwnerItemMirror",),
            # Same instance `noticed()` reads: a lost write stays lost on the
            # object reconciliation observes, and a successful write lands there.
            lambda: mirror,
        ),
    )

    # Negative control through a nested patch that undoes before the probe:
    # every intentional write lands → reconciliation must stay clean. Without
    # this, an over-reporting `divergence` would still pass the probe.
    with monkeypatch.context() as baseline:
        baseline.setattr(
            owner_item_store,
            "PostgresOwnerItemMirror",
            lambda: mirror,
        )
        scenario()
        assert not noticed(), "clean dual-write must reconcile with zero divergence"

    results = each_lost_write_is_noticed(
        monkeypatch,
        targets=targets,
        scenario=scenario,
        noticed=noticed,
    )

    assert [result.target for result in results] == [
        "PostgresOwnerItemMirror",
        "PostgresOwnerItemMirror",
    ], f"expected one probe result per call site, got {[r.target for r in results]}"
    missed = [result for result in results if not result.noticed]
    assert not missed, f"lost writes not noticed by reconciliation: {missed}"


def test_a_naive_timestamp_is_stored_as_the_instant_the_writer_meant(
    mirror: PostgresOwnerItemMirror, pg_connection_factory
) -> None:
    """Naive text handed to `timestamptz` is stamped with the *session* zone.

    That is silent: no error, every row shifted by the gap between the writing
    machine and the server. The zone is attached on the way in instead, so the
    stored instant is the one the writer meant regardless of the server's
    `TimeZone`.
    """
    from datetime import datetime

    written = "2026-08-13T12:00:00"
    expected = datetime.fromisoformat(written).astimezone()

    mirror.upsert(_row("tz", created_at=written, updated_at=written))

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT created_at FROM owner_item WHERE id = 'tz'")
            stored = cur.fetchone()[0]
    assert stored == expected
