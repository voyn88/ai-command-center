"""Slice 6 of the runtime migration: the first **identity column**.

`model_event.id` is `INTEGER PRIMARY KEY AUTOINCREMENT` in SQLite and
`bigint GENERATED ALWAYS AS IDENTITY` in PostgreSQL. Seven columns in the
accepted map are declared that way, and the other six all live in the 15-table
family this migration has not reached — so the class is worked out here.

Two hazards, both asserted against a real PostgreSQL rather than described: an
explicit id is refused without `OVERRIDING SYSTEM VALUE`, and the identity
sequence is left untouched by the inserts that carry one — so the first row
PostgreSQL generates after a cutover collides with a mirrored row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.model_registry_store import (
    MIRROR_UNAVAILABLE,
    MODEL_ENTRY_COLUMNS,
    MODEL_EVENT_COLUMNS,
    PostgresModelEntryMirror,
    PostgresModelEventMirror,
    entry_divergence,
    event_divergence,
)
from command_center.runtime.db import model_registry as mr_db

ROOT = Path(__file__).resolve().parents[2]


def _entry(model_id: str, **overrides: object) -> dict:
    row = {
        "id": model_id,
        "name": f"model {model_id}",
        "kind": "external",
        "provider": None,
        "status": "available",
        "cost": None,
        "quality": None,
        "latency_ms": None,
        "provenance": None,
        "download_progress": 0,
        "version": 0,
        "created_at": "2026-08-14T00:00:00",  # naive local, what `models.iso_now()` emits
        "updated_at": "2026-08-14T00:00:00",
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


def _event(event_id: int, model_id: str, seq: int = 1, **overrides: object) -> dict:
    row = {
        "id": event_id,
        "model_id": model_id,
        "seq": seq,
        "action": "register",
        "actor": None,
        "target_ref": None,
        "provenance": None,
        "metadata_json": None,
        "created_at": "2026-08-14T00:00:00",
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


@pytest.fixture
def entries(pg_connection_factory) -> PostgresModelEntryMirror:
    return PostgresModelEntryMirror(connection_factory=pg_connection_factory)


@pytest.fixture
def events(pg_connection_factory) -> PostgresModelEventMirror:
    return PostgresModelEventMirror(connection_factory=pg_connection_factory)


# --- contract and schema ----------------------------------------------------


def test_both_mirrors_satisfy_the_row_oriented_contract() -> None:
    for mirror in (PostgresModelEntryMirror, PostgresModelEventMirror):
        assert isinstance(mirror(connection_factory=lambda: None), record_mirror.RecordMirror)
        assert mirror.name == "postgres"


def test_the_column_lists_match_the_accepted_postgresql_schema() -> None:
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    for table, expected in (
        ("model_entry", MODEL_ENTRY_COLUMNS),
        ("model_event", MODEL_EVENT_COLUMNS),
    ):
        body = ddl.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]
        declared = tuple(
            line.strip().split()[0]
            for line in body.strip().splitlines()
            if line.strip()
            and not line.strip().startswith("--")
            and not line.strip().startswith("UNIQUE")
        )
        assert declared == expected, table


def test_the_mirrors_cover_every_column_the_authority_stores(tmp_path) -> None:
    """Read from the live SQLite schema, not from a tuple in the source.

    `model_event` has no column tuple in the authority — the insert lists its
    columns inline and lets SQLite mint `id` — so a drifting column would be
    invisible to any comparison against source constants.
    """
    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)
    with mr_db.db.connect(db_path) as conn:
        for table, expected in (
            ("model_entry", MODEL_ENTRY_COLUMNS),
            ("model_event", MODEL_EVENT_COLUMNS),
        ):
            stored = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert stored == set(expected), table


# --- the identity column ----------------------------------------------------


def test_the_authoritys_id_is_the_mirrored_id(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror
) -> None:
    """PostgreSQL refuses an explicit value for a `GENERATED ALWAYS` column, so
    the mirror writes `OVERRIDING SYSTEM VALUE`. Letting PostgreSQL mint its own
    would not be a cosmetic difference: `divergence` matches rows by id, so the
    whole table would report twice — once missing, once ahead."""
    entries.upsert(_entry("m1"))
    events.upsert(_event(41, "m1", seq=1))
    events.upsert(_event(42, "m1", seq=2))

    assert [row["id"] for row in events.list_records()] == [41, 42]


def test_an_event_upsert_is_idempotent_despite_the_identity_column(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror
) -> None:
    """`OVERRIDING SYSTEM VALUE` has to compose with `ON CONFLICT (id) DO
    UPDATE`, or the backfill — which runs more than once by design — would fail
    on the rows it wrote itself."""
    entries.upsert(_entry("m1"))
    events.upsert(_event(7, "m1"))
    events.upsert(_event(7, "m1", action="assign"))

    stored = events.list_records()
    assert len(stored) == 1
    assert stored[0]["action"] == "assign"


def test_mirrored_ids_leave_the_sequence_behind_and_the_next_native_write_collides(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror, pg_connection_factory
) -> None:
    """The cutover hazard, reproduced rather than described.

    Inserts carrying an explicit id do not advance the identity sequence, so
    after mirroring ids 1..N the sequence still starts at 1 — and the first row
    PostgreSQL generates for itself duplicates a mirrored key. Nothing in a
    dual-write triggers this: while SQLite is the authority every id arrives
    from the mirror. It fails on the first native write after reads switch,
    which is the worst moment to find out.
    """
    entries.upsert(_entry("m1"))
    for n in (1, 2, 3):
        events.upsert(_event(n, "m1", seq=n))

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as collision:
                cur.execute(
                    "INSERT INTO model_event (model_id, seq, action, created_at) "
                    "VALUES ('m1', 99, 'use', now())"
                )
    assert "duplicate key" in str(collision.value).lower()


def test_resync_identity_makes_the_next_native_write_land(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror, pg_connection_factory
) -> None:
    """The cutover step that clears the hazard above, and the number an
    operator's log should carry."""
    entries.upsert(_entry("m1"))
    for n in (1, 2, 3):
        events.upsert(_event(n, "m1", seq=n))

    assert events.resync_identity() == 3

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO model_event (model_id, seq, action, created_at) "
                "VALUES ('m1', 99, 'use', now()) RETURNING id"
            )
            assert cur.fetchone()[0] == 4


def test_resync_identity_is_a_no_op_on_an_empty_table(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror, pg_connection_factory
) -> None:
    """A cutover may run before any row is mirrored — `setval` with `NULL`
    would raise, and an operator would read a failed cutover step.

    Stronger than "does not raise": the sequence must still yield **1**
    afterwards. The two-argument `setval` marks the sequence called and would
    burn the first id, which is harmless for a surrogate key but makes the
    operation something other than its name. Independent review raised it; the
    behaviour is pinned here rather than left incidental.
    """
    assert events.resync_identity() == 1

    entries.upsert(_entry("m1"))
    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO model_event (model_id, seq, action, created_at) "
                "VALUES ('m1', 1, 'use', now()) RETURNING id"
            )
            assert cur.fetchone()[0] == 1


# --- the already-solved shapes this table also has --------------------------


def test_metadata_round_trips_through_jsonb(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror
) -> None:
    entries.upsert(_entry("m1"))
    events.upsert(_event(1, "m1", metadata_json=json.dumps({"kind": "local"})))

    assert events.list_records()[0]["metadata_json"] == {"kind": "local"}


def test_an_event_needs_its_model_in_the_mirror_first(events: PostgresModelEventMirror) -> None:
    with pytest.raises(Exception) as refused:
        events.upsert(_event(1, "absent-model"))
    assert "foreign key" in str(refused.value).lower()


def test_timestamps_round_trip(
    entries: PostgresModelEntryMirror, events: PostgresModelEventMirror
) -> None:
    entries.upsert(_entry("m1"))
    events.upsert(_event(1, "m1"))

    assert entries.list_records()[0]["created_at"] == "2026-08-14T00:00:00"
    assert events.list_records()[0]["created_at"] == "2026-08-14T00:00:00"


# --- the dual-write ---------------------------------------------------------


def test_reconciliation_is_clean_for_rows_the_application_actually_wrote(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Staged after every authority write — the shape slice 5 arrived at.

    An end-state check cannot see a write that a later whole-row write covers,
    and this family has two such paths (`set_model_status`,
    `update_download_progress` both rewrite the entry). Reconciling per stage
    is what makes "any single lost mirror write is caught" true rather than
    hopeful.
    """
    from command_center.db import model_registry_store

    monkeypatch.setattr(
        model_registry_store,
        "PostgresModelEntryMirror",
        lambda: PostgresModelEntryMirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        model_registry_store,
        "PostgresModelEventMirror",
        lambda: PostgresModelEventMirror(connection_factory=pg_connection_factory),
    )
    entries = PostgresModelEntryMirror(connection_factory=pg_connection_factory)
    events = PostgresModelEventMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert entry_divergence(mr_db.list_model_entries(db_path), entries) == [], stage
        assert event_divergence(mr_db.list_model_events_stored(db_path), events) == [], stage

    created = mr_db.create_model_entry(
        db_path, model_id="m1", name="Local 7B", kind="local", status="downloading"
    )
    reconciled("entry created")
    progressed = mr_db.update_download_progress(
        db_path, created["id"], expected_version=created["version"], progress=50, actor="ops"
    )
    reconciled("download progress")
    mr_db.set_model_status(
        db_path,
        created["id"],
        expected_version=progressed["version"],
        status="installed",
        actor="ops",
    )
    reconciled("status set")
    mr_db.append_model_event(db_path, created["id"], action="use", target_ref="task:1")
    reconciled("standalone event appended")


def test_reconciling_against_the_decoded_reader_is_not_clean(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The trap slice 4 was rejected for, pinned here in the same slice.

    `list_model_events` returns `_decode_event_row` output, which pops
    `metadata_json` **and** carries no `id` for the rows it decodes from a
    stored record. Reconciliation fed those reports every event divergent.
    """
    from command_center.db import model_registry_store

    monkeypatch.setattr(
        model_registry_store,
        "PostgresModelEntryMirror",
        lambda: PostgresModelEntryMirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        model_registry_store,
        "PostgresModelEventMirror",
        lambda: PostgresModelEventMirror(connection_factory=pg_connection_factory),
    )
    events = PostgresModelEventMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)
    mr_db.create_model_entry(db_path, model_id="m1", name="Local 7B", kind="local")

    decoded = mr_db.list_model_events(db_path, "m1")
    assert "metadata_json" not in decoded[0]  # the premise

    assert event_divergence(decoded, events) != []
    assert event_divergence(mr_db.list_model_events_stored(db_path), events) == []


def test_every_write_path_mirrors_after_the_authoritative_commit(tmp_path, monkeypatch) -> None:
    """Recorded rather than asserted inside the callbacks: the hooks swallow
    every `Exception`, and `AssertionError` is one."""
    from command_center.db import model_registry_store

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)
    observed: list[tuple[str, object]] = []

    class RecordingEntries:
        def upsert(self, record: dict) -> None:
            stored = mr_db.get_model_entry(db_path, record["id"])
            observed.append(("entry", stored is not None and stored["status"] == record["status"]))

    class RecordingEvents:
        def upsert(self, record: dict) -> None:
            stored = mr_db.list_model_events_stored(db_path)
            observed.append(("event", any(row["id"] == record["id"] for row in stored)))

    monkeypatch.setattr(model_registry_store, "PostgresModelEntryMirror", lambda: RecordingEntries())
    monkeypatch.setattr(model_registry_store, "PostgresModelEventMirror", lambda: RecordingEvents())

    created = mr_db.create_model_entry(db_path, model_id="m1", name="Local 7B", kind="local")
    mr_db.set_model_status(
        db_path,
        created["id"],
        expected_version=created["version"],
        status="downloading",
        actor="ops",
    )
    mr_db.append_model_event(db_path, created["id"], action="use")

    # Each write mirrors the entry then its event, and every observation saw
    # its own committed row — including the event's SQLite-minted id.
    assert observed == [
        ("entry", True),
        ("event", True),
        ("entry", True),
        ("event", True),
        ("event", True),
    ]


def test_the_event_mirror_receives_the_stored_record_not_the_decoded_one(
    tmp_path, monkeypatch
) -> None:
    """`_decode_event_row` drops `metadata_json` and the decoded row carries no
    id. Mirroring that would send `id=None` into a column PostgreSQL refuses a
    non-DEFAULT value for — every event lost, silently, since the hook
    swallows."""
    from command_center.db import model_registry_store

    seen: list[dict] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            seen.append(dict(record))

    monkeypatch.setattr(model_registry_store, "PostgresModelEntryMirror", lambda: Recording())
    monkeypatch.setattr(model_registry_store, "PostgresModelEventMirror", lambda: Recording())

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)
    returned = mr_db.append_model_event(
        db_path,
        mr_db.create_model_entry(db_path, model_id="m1", name="n", kind="local")["id"],
        action="use",
        metadata={"why": "test"},
    )

    assert "metadata_json" not in returned and "id" not in returned  # the public shape is unchanged
    event = seen[-1]
    assert isinstance(event["id"], int)
    assert json.loads(event["metadata_json"]) == {"why": "test"}


def test_the_public_event_shapes_are_unchanged(tmp_path, monkeypatch) -> None:
    """The two public readers had **different** shapes before this slice, and
    both are preserved.

    `list_model_events` reads `SELECT *` and has always included `id`;
    `append_model_event` built its dict by hand and never had one. Slice 6 gave
    the append path an id because the mirror needs it — and the first attempt
    at keeping shapes intact dropped `id` from the shared decoder, on the belief
    that it had only just appeared. It had not: that silently narrowed the list
    reader, which is exported on the `runtime.db` facade. Independent review
    caught it by running the same probe at both SHAs.

    Pinned here so the next person who touches the decoder has to look at both
    callers rather than at one.
    """
    from command_center.db import model_registry_store

    class Recording:
        def upsert(self, record: dict) -> None:
            pass

    monkeypatch.setattr(model_registry_store, "PostgresModelEntryMirror", lambda: Recording())
    monkeypatch.setattr(model_registry_store, "PostgresModelEventMirror", lambda: Recording())

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)
    created = mr_db.create_model_entry(db_path, model_id="m1", name="n", kind="local")
    appended = mr_db.append_model_event(db_path, created["id"], action="use")

    assert "id" not in appended, "append_model_event never returned an id"
    assert all("id" in row for row in mr_db.list_model_events(db_path, "m1")), (
        "list_model_events has always returned an id"
    )


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import model_registry_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(model_registry_store, "PostgresModelEntryMirror", lambda: Exploding())
    monkeypatch.setattr(model_registry_store, "PostgresModelEventMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    mr_db.db.migrate(db_path)

    created = mr_db.create_model_entry(db_path, model_id="m1", name="survives", kind="local")
    mr_db.append_model_event(db_path, created["id"], action="use")

    assert mr_db.get_model_entry(db_path, "m1")["name"] == "survives"
    assert len(mr_db.list_model_events_stored(db_path)) == 2


# --- reconciliation and packaging -------------------------------------------


def test_reconciliation_reports_every_shape_of_disagreement(
    entries: PostgresModelEntryMirror,
) -> None:
    agreed = _entry("m1")
    entries.upsert(agreed)
    assert entry_divergence([agreed], entries) == []

    entries.upsert(_entry("m1", status="ready"))
    assert [e["fields"] for e in entry_divergence([agreed], entries)] == [["status"]]

    missing = entry_divergence([agreed, _entry("m2")], entries)
    assert {e["id"] for e in missing} >= {"m2"}

    assert {e["id"] for e in entry_divergence([], entries)} == {"m1"}


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    for report in (
        entry_divergence([_entry("m1")], Broken()),
        event_divergence([_event(1, "m1")], Broken()),
    ):
        assert [entry["id"] for entry in report] == [MIRROR_UNAVAILABLE]


def test_importing_the_store_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.model_registry_store as s;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules;"
        "s.PostgresModelEntryMirror(); s.PostgresModelEventMirror()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr
