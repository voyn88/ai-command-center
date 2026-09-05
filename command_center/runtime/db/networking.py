"""Wave-3 Networking engine table-family (VOYN-W3-NET): the persistence tier
behind the networking feedback/invitation loop (routes → service → repository →
db).

This module owns three additive tables — ``contact``, ``message`` and
``networking_invitation`` — wholly separate from every other family in
:mod:`command_center.runtime.db`. Every write goes through the shared
``connect()``/``transaction()`` primitives (WAL, ``BEGIN IMMEDIATE`` write lock,
per-row ``version`` compare-and-set), so the single-writer discipline the rest of
``runtime.db`` follows holds here too.

* A **contact** is a mutable current-state row (name/handle/org/note) guarded by a
  ``version`` compare-and-set column.
* A **message** is a write-once row exchanged with a contact; an inbound
  ``feedback``-kind message is the intake the *service* turns into an actionable
  board task.
* A **networking_invitation** is a mutable row moving through an explicit status
  allowlist (:data:`INVITATION_TRANSITIONS`, ``pending → accepted/declined``).
  ``council_ref`` is the stable seam the Council engine consumes — this layer
  wires no external identity/auth, only the boundary.

``project_ref`` (nullable) is the redaction key on all three tables:
:func:`list_contacts`/:func:`list_messages`/:func:`list_invitations` drop
BANK/LEGAL rows *in the SQL query* so a sensitive contact's handle never leaves
the read surface (the Wave-1 exclude-in-SQL pattern).

Statuses/directions/kinds are stored as their stable string *values* (never a
Python enum member name) so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares — the enum-name lesson.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

#: A message's direction relative to the operator.
MESSAGE_DIRECTIONS: frozenset[str] = frozenset({"inbound", "outbound"})

#: A message's kind. ``feedback`` is the intake the service turns into a task;
#: ``note`` is a plain logged message.
MESSAGE_KINDS: frozenset[str] = frozenset({"note", "feedback"})

#: The invitation lifecycle. ``accepted``/``declined`` are terminal.
INVITATION_STATUSES: frozenset[str] = frozenset({"pending", "accepted", "declined"})

#: The allowed status edges. A ``pending`` invitation may be accepted or declined;
#: both are terminal. The Council engine (once wired) drives these transitions
#: through the service after consuming ``council_ref``.
INVITATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"accepted", "declined"}),
    "accepted": frozenset(),
    "declined": frozenset(),
}

#: Fields a caller may set through :func:`update_contact_fields`. ``project_ref``
#: is write-once at creation (it is the redaction key) and is never routed here.
_UPDATABLE_CONTACT_FIELDS: frozenset[str] = frozenset({"display_name", "handle", "org", "note"})


class InvalidInvitationTransitionError(Exception):
    """Raised when an invitation status change is not an allowed edge in
    :data:`INVITATION_TRANSITIONS` (a backward jump, or any move out of a
    terminal ``accepted``/``declined`` state)."""


# --------------------------------------------------------------------------
# Shared redaction helper
# --------------------------------------------------------------------------


def _exclude_projects_clause(
    exclude_projects: Iterable[str] | None,
) -> tuple[str | None, list[str]]:
    """Build a ``WHERE`` fragment dropping rows whose ``project_ref`` is in
    ``exclude_projects`` — the redaction policy expressed *in SQL* so a
    ``LIMIT``/``OFFSET`` page counts only visible rows. Un-attributed rows
    (``project_ref IS NULL``) are always kept. Returns ``(None, [])`` when there
    is nothing to exclude."""
    projects = [p for p in (exclude_projects or []) if p]
    if not projects:
        return None, []
    placeholders = ", ".join("?" for _ in projects)
    clause = f"(project_ref IS NULL OR project_ref NOT IN ({placeholders}))"
    return clause, projects


# --------------------------------------------------------------------------
# contact
# --------------------------------------------------------------------------

_CONTACT_COLUMNS: tuple[str, ...] = (
    "id",
    "display_name",
    "handle",
    "org",
    "note",
    "project_ref",
    "version",
    "created_at",
    "updated_at",
)


def create_contact(
    db_path: Path,
    *,
    display_name: str,
    handle: str = "",
    org: str | None = None,
    note: str | None = None,
    project_ref: str | None = None,
    contact_id: str | None = None,
) -> dict:
    """Insert one ``contact`` row and return it. ``display_name`` is required;
    everything else is optional."""
    if not (display_name or "").strip():
        raise ValueError("contact.display_name must not be empty")
    now = db.iso_now()
    record = {name: None for name in _CONTACT_COLUMNS}
    record.update(
        {
            "id": contact_id or db.new_id(),
            "display_name": display_name,
            "handle": handle or "",
            "org": org,
            "note": note,
            "project_ref": project_ref,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_CONTACT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _CONTACT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(f"INSERT INTO contact ({columns}) VALUES ({placeholders})", record)
    _mirror_contact(record)
    return record


def _mirror_contact(record: dict) -> None:
    """Best-effort dual-write of one contact into PostgreSQL (SRV-01B slice 5).

    After the authoritative commit and silent on failure, as in slices 2-4.
    What is new is the consequence: ``contact`` is a foreign-key parent, so a
    swallowed failure here does not cost one row. Every later ``message`` write
    for this contact is refused by the target as well, and swallowed in turn,
    so one dropped parent becomes a growing hole that only reconciliation
    resolves. Tracked as ``VOYN-W0-AICC-MIRROR-SILENT-DROP``, which this table
    gives a multiplier rather than a new cause. The failure still logs at
    WARNING, though, so the hole is at least visible from the first row that
    falls into it rather than only once someone runs ``divergence``.
    """
    try:
        from command_center.db.networking_store import PostgresContactMirror

        PostgresContactMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror contact into PostgreSQL", exc_info=True)


def _mirror_message(record: dict) -> None:
    """Best-effort dual-write of one message into PostgreSQL (SRV-01B slice 5).

    Runs after the authoritative commit, and after its contact's mirror write
    for the same reason the authority is ordered that way: the foreign key.
    No ordering logic of its own is needed or wanted - a mirror that created a
    missing parent to make this land would write a row the authority never
    had, which is the one state no reconciliation flags as wrong.
    """
    try:
        from command_center.db.networking_store import PostgresMessageMirror

        PostgresMessageMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror message into PostgreSQL", exc_info=True)


def get_contact(db_path: Path, contact_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM contact WHERE id = ?", (contact_id,)).fetchone()
        return db._row_to_dict(row)


def list_contacts(
    db_path: Path,
    *,
    project: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List contacts, newest first (``created_at DESC, id DESC``), optionally
    filtered by ``project``. ``exclude_projects`` drops rows for those projects
    *in the query* (redaction), so a page counts only visible rows."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project_ref = ?")
        params.append(project)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM contact{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def update_contact_fields(
    db_path: Path, contact_id: str, *, expected_version: int, fields: dict
) -> dict:
    """Compare-and-set update of a contact's mutable fields. Refuses any key
    outside :data:`_UPDATABLE_CONTACT_FIELDS` and a stale ``version``
    (:class:`db.LostUpdateError`). Bumps ``version`` and ``updated_at``."""
    unknown = set(fields) - _UPDATABLE_CONTACT_FIELDS
    if unknown:
        raise ValueError(f"contact update has non-updatable fields: {sorted(unknown)}")
    if not fields:
        raise ValueError("update_contact_fields requires at least one field")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT version FROM contact WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such contact: {contact_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"contact {contact_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            payload = dict(fields)
            payload["updated_at"] = now
            set_clause = ", ".join(f"{key} = :{key}" for key in payload)
            params = dict(payload)
            params["contact_id"] = contact_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE contact SET {set_clause}, version = version + 1 "
                "WHERE id = :contact_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"contact {contact_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute("SELECT * FROM contact WHERE id = ?", (contact_id,)).fetchone()
            record = dict(updated)
    # Outside the `with`: the mirror follows the committed row, never one a
    # rollback could still discard.
    _mirror_contact(record)
    return record


# --------------------------------------------------------------------------
# message
# --------------------------------------------------------------------------

_MESSAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "contact_id",
    "direction",
    "kind",
    "body",
    "project_ref",
    "created_at",
)


def create_message(
    db_path: Path,
    *,
    contact_id: str,
    direction: str = "inbound",
    kind: str = "note",
    body: str = "",
    project_ref: str | None = None,
    message_id: str | None = None,
) -> dict:
    """Insert one write-once ``message`` row and return it.

    ``direction`` must be one of :data:`MESSAGE_DIRECTIONS` and ``kind`` one of
    :data:`MESSAGE_KINDS`; both are validated at this boundary so a malformed
    value can never reach a stored row. The referenced ``contact`` must exist
    (enforced by the foreign key)."""
    if direction not in MESSAGE_DIRECTIONS:
        raise ValueError(f"message.direction must be one of {sorted(MESSAGE_DIRECTIONS)}, got {direction!r}")
    if kind not in MESSAGE_KINDS:
        raise ValueError(f"message.kind must be one of {sorted(MESSAGE_KINDS)}, got {kind!r}")
    now = db.iso_now()
    record = {
        "id": message_id or db.new_id(),
        "contact_id": contact_id,
        "direction": direction,
        "kind": kind,
        "body": body or "",
        "project_ref": project_ref,
        "created_at": now,
    }
    columns = ", ".join(_MESSAGE_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _MESSAGE_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(f"INSERT INTO message ({columns}) VALUES ({placeholders})", record)
    _mirror_message(record)
    return record


def list_messages(
    db_path: Path,
    *,
    contact_id: str | None = None,
    kind: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List messages, newest first, optionally filtered by ``contact_id`` and/or
    ``kind``. ``exclude_projects`` drops rows for those projects *in the query*
    (redaction), so a page counts only visible rows."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if contact_id is not None:
        clauses.append("contact_id = ?")
        params.append(contact_id)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM message{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# networking_invitation — the Council seam
# --------------------------------------------------------------------------

_INVITATION_COLUMNS: tuple[str, ...] = (
    "id",
    "contact_id",
    "council_ref",
    "status",
    "note",
    "project_ref",
    "invited_at",
    "responded_at",
    "version",
    "created_at",
    "updated_at",
)


def create_invitation(
    db_path: Path,
    *,
    contact_id: str,
    council_ref: str,
    note: str | None = None,
    project_ref: str | None = None,
    status: str = "pending",
    invitation_id: str | None = None,
) -> dict:
    """Insert one ``networking_invitation`` row and return it.

    ``council_ref`` is the stable, opaque reference the Council engine consumes;
    the caller mints it (no external identity/auth is resolved here). ``status``
    defaults to ``pending`` and must be a known status."""
    if not (council_ref or "").strip():
        raise ValueError("invitation.council_ref must not be empty")
    if status not in INVITATION_STATUSES:
        raise ValueError(f"invitation.status must be one of {sorted(INVITATION_STATUSES)}, got {status!r}")
    now = db.iso_now()
    record = {name: None for name in _INVITATION_COLUMNS}
    record.update(
        {
            "id": invitation_id or db.new_id(),
            "contact_id": contact_id,
            "council_ref": council_ref,
            "status": status,
            "note": note,
            "project_ref": project_ref,
            "invited_at": now,
            "responded_at": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_INVITATION_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _INVITATION_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO networking_invitation ({columns}) VALUES ({placeholders})", record
            )
    _mirror_invitation(record)
    return record


def _mirror_invitation(record: dict) -> None:
    """Best-effort dual-write of one invitation into PostgreSQL (SRV-01B slice 8).

    After the authoritative commit and silent on failure, like every mirror
    since slice 2. Same foreign-key consequence as `message`: a lost `contact`
    write makes this one fail too, and both failures are swallowed, so the hole
    is visible only to reconciliation.
    """
    try:
        from command_center.db.networking_store import PostgresInvitationMirror

        PostgresInvitationMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror networking_invitation into PostgreSQL", exc_info=True)



def get_invitation(db_path: Path, invitation_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM networking_invitation WHERE id = ?", (invitation_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_invitations(
    db_path: Path,
    *,
    status: str | None = None,
    contact_id: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List invitations, newest first, optionally filtered by ``status`` and/or
    ``contact_id``. ``exclude_projects`` drops rows for those projects *in the
    query* (redaction). This is the read side of the Council seam — the Council
    engine lists ``pending`` invitations by ``council_ref``."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if contact_id is not None:
        clauses.append("contact_id = ?")
        params.append(contact_id)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM networking_invitation{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def set_invitation_status(
    db_path: Path, invitation_id: str, *, expected_version: int, status: str
) -> dict:
    """Compare-and-set status change guarded by the transition allowlist.

    Checks the caller's ``version`` first (a stale writer always loses as a stale
    writer), then refuses an illegal status edge, stamps ``responded_at`` when the
    move is into a terminal state, bumps ``version`` and sets ``updated_at``. This
    is the write side of the Council seam: Council resolves an invitation by
    driving it to ``accepted``/``declined`` through here."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM networking_invitation WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No such invitation: {invitation_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"invitation {invitation_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if status not in INVITATION_STATUSES:
                raise ValueError(f"unknown invitation status {status!r}")
            if status not in INVITATION_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidInvitationTransitionError(
                    f"invitation {invitation_id!r} cannot transition "
                    f"{row['status']!r} -> {status!r}"
                )
            fields: dict[str, Any] = {"status": status, "updated_at": now, "responded_at": now}
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["invitation_id"] = invitation_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE networking_invitation SET {set_clause}, version = version + 1 "
                "WHERE id = :invitation_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"invitation {invitation_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM networking_invitation WHERE id = ?", (invitation_id,)
            ).fetchone()
            record = dict(updated)
    # Outside the `with`: the mirror follows the committed row.
    _mirror_invitation(record)
    return record
