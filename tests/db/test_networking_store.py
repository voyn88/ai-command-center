"""Slice 5 of the runtime migration: the first mirrored **foreign key**.

`message.contact_id` references `contact(id)`. Nothing about a single row
changes; what changes is what a lost row costs. The four tables mirrored so far
are independent — a failed mirror write leaves one row missing and every later
write still lands. Here a failed `contact` write makes every subsequent
`message` write for that contact fail too, and both failures are swallowed by
design, so one dropped parent becomes a growing hole.

That cascade is reproduced against a real PostgreSQL below rather than argued
in a docstring, because it is the kind of claim this migration has twice had to
retract.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.networking_store import (
    CONTACT_COLUMNS,
    MESSAGE_COLUMNS,
    MIRROR_UNAVAILABLE,
    PostgresContactMirror,
    PostgresMessageMirror,
    contact_divergence,
    message_divergence,
)
from command_center.runtime.db import networking as net_db

ROOT = Path(__file__).resolve().parents[2]


def _contact(contact_id: str, **overrides: object) -> dict:
    row = {
        "id": contact_id,
        "display_name": f"contact {contact_id}",
        "handle": "",
        "org": None,
        "note": None,
        "project_ref": None,
        "version": 0,
        "created_at": "2026-08-14T00:00:00",  # naive local, what `models.iso_now()` emits
        "updated_at": "2026-08-14T00:00:00",
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


def _message(message_id: str, contact_id: str, **overrides: object) -> dict:
    row = {
        "id": message_id,
        "contact_id": contact_id,
        "direction": "inbound",
        "kind": "note",
        "body": "",
        "project_ref": None,
        "created_at": "2026-08-14T00:00:00",
    }
    row.update(overrides)  # type: ignore[arg-type]
    return row


def _code_without_prose(function: object) -> str:
    """A function's executable code, with comments and docstrings removed.

    Third copy of this helper, and it should be the last: slice 4's acceptance
    found the second copy had already drifted from the first (a missing
    `ClassDef`), which is the "every restatement is subtly different" failure
    mode `mirror_support` exists to end, reproduced in the test suite. Hoisting
    all three into a shared helper is `VOYN-W0-AICC-TEST-HELPER-DUPLICATION`;
    this copy matches the slice-3 original exactly so the hoist is a move
    rather than a reconciliation.
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
    return ast.unparse(tree)


@pytest.fixture
def contacts(pg_connection_factory) -> PostgresContactMirror:
    return PostgresContactMirror(connection_factory=pg_connection_factory)


@pytest.fixture
def messages(pg_connection_factory) -> PostgresMessageMirror:
    return PostgresMessageMirror(connection_factory=pg_connection_factory)


# --- contract and authority -------------------------------------------------


def test_both_mirrors_satisfy_the_row_oriented_contract() -> None:
    for mirror in (PostgresContactMirror, PostgresMessageMirror):
        assert isinstance(mirror(connection_factory=lambda: None), record_mirror.RecordMirror)
        assert mirror.name == "postgres"


def test_the_column_lists_match_the_accepted_postgresql_schema() -> None:
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    for table, expected in (("contact", CONTACT_COLUMNS), ("message", MESSAGE_COLUMNS)):
        body = ddl.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]
        declared = tuple(
            line.strip().split()[0]
            for line in body.strip().splitlines()
            if line.strip() and not line.strip().startswith("--")
        )
        assert declared == expected, table


def test_the_mirrors_cover_every_column_the_authority_writes() -> None:
    assert set(net_db._CONTACT_COLUMNS) == set(CONTACT_COLUMNS)
    assert set(net_db._MESSAGE_COLUMNS) == set(MESSAGE_COLUMNS)


def test_sqlite_remains_the_authority_for_the_networking_family() -> None:
    """Same guard and same stated limit as slices 3 and 4: it reads code with
    prose stripped, so it catches a direct read and not one added through a
    helper — the indirection the write itself uses."""
    for function in (
        net_db.create_contact,
        net_db.update_contact_fields,
        net_db.create_message,
        net_db.get_contact,
        net_db.list_contacts,
        net_db.list_messages,
    ):
        code = _code_without_prose(function)
        for marker in ("postgres", "networking_store", "list_records"):
            assert marker not in code.lower(), f"{function.__name__}: {marker}"


def test_the_public_readers_already_return_the_stored_shape() -> None:
    """Slice 4 shipped without a runnable reconciliation because every
    `digest_item` reader decodes its JSON column. That was called out as true
    "for this table alone"; this pins the claim for the family added here
    instead of leaving the next slice to rediscover it.
    """
    for reader in (net_db.list_contacts, net_db.list_messages):
        assert "dict(row)" in inspect.getsource(reader)
    assert "_row_to_dict" in inspect.getsource(net_db.get_contact)


# --- the foreign key --------------------------------------------------------


def test_a_message_needs_its_contact_in_the_mirror_first(
    contacts: PostgresContactMirror, messages: PostgresMessageMirror
) -> None:
    """The refusal is the honest outcome, and it is the target's, not ours.

    A mirror that created the missing parent to make the child land would put a
    row in the mirror the authority never wrote — the "mirror ahead of the
    system of record" state no reconciliation flags as wrong.
    """
    with pytest.raises(Exception) as refused:
        messages.upsert(_message("m1", "absent-contact"))
    assert "foreign key" in str(refused.value).lower()

    contacts.upsert(_contact("c1"))
    messages.upsert(_message("m1", "c1"))

    assert [row["id"] for row in messages.list_records()] == ["m1"]


def test_one_dropped_parent_silently_costs_every_child_after_it(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The property this slice exists to establish, reproduced rather than
    argued.

    A `contact` whose mirror write fails is one missing row. Every `message`
    for that contact then fails too — the target refuses the child — and the
    hooks swallow both, so nothing surfaces at write time. Reconciliation
    reports the hole from both sides, which is the only thing that does.
    """
    from command_center.db import networking_store

    real_contacts = PostgresContactMirror(connection_factory=pg_connection_factory)
    real_messages = PostgresMessageMirror(connection_factory=pg_connection_factory)

    class RefusingContacts:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres blipped on the parent")

    monkeypatch.setattr(networking_store, "PostgresContactMirror", lambda: RefusingContacts())
    monkeypatch.setattr(networking_store, "PostgresMessageMirror", lambda: real_messages)

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)

    contact = net_db.create_contact(db_path, display_name="dropped parent")
    for body in ("first", "second", "third"):
        net_db.create_message(db_path, contact_id=contact["id"], body=body)

    # The authority has everything; no caller saw an error.
    assert len(net_db.list_messages(db_path, contact_id=contact["id"])) == 3
    # The mirror has none of it — one dropped parent, three lost children.
    assert real_contacts.list_records() == []
    assert real_messages.list_records() == []

    # And reconciliation is the only thing that shows it, from both sides.
    assert len(contact_divergence(net_db.list_contacts(db_path), real_contacts)) == 1
    assert len(message_divergence(net_db.list_messages(db_path), real_messages)) == 3


# --- ordinary mirroring -----------------------------------------------------


def test_timestamps_round_trip_for_both_tables(
    contacts: PostgresContactMirror, messages: PostgresMessageMirror
) -> None:
    contacts.upsert(_contact("c1"))
    messages.upsert(_message("m1", "c1"))

    assert contacts.list_records()[0]["created_at"] == "2026-08-14T00:00:00"
    assert messages.list_records()[0]["created_at"] == "2026-08-14T00:00:00"


def test_upsert_is_idempotent_and_updates_in_place(contacts: PostgresContactMirror) -> None:
    contacts.upsert(_contact("c1"))
    contacts.upsert(_contact("c1"))
    assert len(contacts.list_records()) == 1

    contacts.upsert(_contact("c1", display_name="renamed", version=1))

    stored = contacts.list_records()
    assert (stored[0]["display_name"], stored[0]["version"]) == ("renamed", 1)


def test_reconciliation_is_clean_for_rows_the_application_actually_wrote(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The assertion the cutover is gated on, driven through the real hooks,
    and checked **after every write** rather than once at the end.

    Two rounds of review went into that sentence. The first version claimed a
    store failing on any one write could not pass it; that was false, because
    `contact` is written twice and the update is a whole-row upsert that
    repairs a failed create. The second version added `quiet`, a contact that
    is never updated, and claimed the same reach — also false. Adding a
    protected row does not protect the unprotected one: `reconciles` is still
    created, then updated, and its create is still repaired. Worse, the
    perturbation I used to check the fix stopped reaching the defect the moment
    `quiet` was inserted ahead of it, so the evidence looked like confirmation
    and was not. Independent review found this by failing each of the five
    mirror writes in turn — four caught, one still masked.

    The lesson is structural and slice 4 had already reached it: an *end-state*
    reconciliation cannot see an intermediate write that a later whole-row
    write covers. No arrangement of rows fixes that. So this test stops being
    an end-state check — it reconciles after each authority write, which makes
    every lost write visible at the stage it happened, and gives the failure an
    address instead of leaving attribution to the per-path test.
    """
    from command_center.db import networking_store

    monkeypatch.setattr(
        networking_store,
        "PostgresContactMirror",
        lambda: PostgresContactMirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        networking_store,
        "PostgresMessageMirror",
        lambda: PostgresMessageMirror(connection_factory=pg_connection_factory),
    )
    contacts = PostgresContactMirror(connection_factory=pg_connection_factory)
    messages = PostgresMessageMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert contact_divergence(net_db.list_contacts(db_path), contacts) == [], stage
        assert message_divergence(net_db.list_messages(db_path), messages) == [], stage

    # A contact written once and never touched again — the write-once shape.
    quiet = net_db.create_contact(db_path, display_name="quiet", handle="@quiet")
    reconciled("quiet created")
    net_db.create_message(db_path, contact_id=quiet["id"], body="only message")
    reconciled("quiet's message created")

    # And one that is updated after creation — the shape whose create an
    # end-state check could not see, because the update rewrites the whole row.
    created = net_db.create_contact(db_path, display_name="reconciles", org="acme")
    reconciled("contact created")
    net_db.update_contact_fields(
        db_path, created["id"], expected_version=0, fields={"note": "spoke on Tuesday"}
    )
    reconciled("contact updated")
    net_db.create_message(db_path, contact_id=created["id"], body="hello", direction="outbound")
    reconciled("message created")

    # Premise guards, not mirror assertions: they read the authority, so they
    # can never fail for a mirror reason. Stated because the previous version
    # of this test counted them among its evidence.
    assert len(net_db.list_contacts(db_path)) == 2
    assert len(net_db.list_messages(db_path)) == 2


def test_every_write_path_mirrors_after_the_authoritative_commit(tmp_path, monkeypatch) -> None:
    """Recorded rather than asserted inside the callbacks: both hooks swallow
    every `Exception`, and `AssertionError` is one."""
    from command_center.db import networking_store

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)
    observed: list[tuple[str, bool]] = []

    class RecordingContacts:
        def upsert(self, record: dict) -> None:
            stored = net_db.get_contact(db_path, record["id"])
            observed.append(("contact", stored is not None and stored["note"] == record["note"]))

    class RecordingMessages:
        def upsert(self, record: dict) -> None:
            found = net_db.list_messages(db_path, contact_id=record["contact_id"])
            observed.append(("message", any(row["id"] == record["id"] for row in found)))

    monkeypatch.setattr(networking_store, "PostgresContactMirror", lambda: RecordingContacts())
    monkeypatch.setattr(networking_store, "PostgresMessageMirror", lambda: RecordingMessages())

    created = net_db.create_contact(db_path, display_name="ordered")
    net_db.update_contact_fields(
        db_path, created["id"], expected_version=0, fields={"note": "after"}
    )
    net_db.create_message(db_path, contact_id=created["id"], body="m")

    # Three writes, three mirror calls, each seeing its own committed row.
    assert observed == [("contact", True), ("contact", True), ("message", True)]


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import networking_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(networking_store, "PostgresContactMirror", lambda: Exploding())
    monkeypatch.setattr(networking_store, "PostgresMessageMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)

    created = net_db.create_contact(db_path, display_name="survives")
    message = net_db.create_message(db_path, contact_id=created["id"], body="also survives")

    assert net_db.get_contact(db_path, created["id"])["display_name"] == "survives"
    assert [row["id"] for row in net_db.list_messages(db_path)] == [message["id"]]


# --- reconciliation and packaging -------------------------------------------


def test_reconciliation_reports_every_shape_of_disagreement(
    contacts: PostgresContactMirror,
) -> None:
    agreed = _contact("same")
    contacts.upsert(agreed)
    assert contact_divergence([agreed], contacts) == []

    contacts.upsert(_contact("same", display_name="drifted"))
    assert [e["fields"] for e in contact_divergence([agreed], contacts)] == [["display_name"]]

    missing = contact_divergence([agreed, _contact("absent")], contacts)
    assert {e["id"] for e in missing} >= {"absent"}

    assert {e["id"] for e in contact_divergence([], contacts)} == {"same"}


def test_an_unreadable_mirror_is_reported_not_treated_as_agreement() -> None:
    class Broken:
        name = "postgres"

        def list_records(self) -> list[dict]:
            raise RuntimeError("connection refused")

    for report in (contact_divergence([_contact("a")], Broken()),
                   message_divergence([_message("m", "a")], Broken())):
        assert [entry["id"] for entry in report] == [MIRROR_UNAVAILABLE]


def test_importing_the_store_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.networking_store as s;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules;"
        "s.PostgresContactMirror(); s.PostgresMessageMirror()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr
