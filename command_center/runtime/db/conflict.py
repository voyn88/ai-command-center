"""Wave-2 Conflicts/Incidents engine table-family (VOYN-W2-CONFLICT): the
persistence tier behind the conflicts surface (routes → service → repository →
db).

This module owns one additive ``conflict`` table, wholly separate from every
other family in :mod:`command_center.runtime.db`. Every write goes through the
shared ``connect()``/``transaction()`` primitives (WAL, ``BEGIN IMMEDIATE``
write lock, per-row ``version`` compare-and-set), so the single-writer
discipline the rest of ``runtime.db`` follows holds here too.

A conflict is a mutable current-state row moving through an explicit status
allowlist (:data:`CONFLICT_TRANSITIONS`, ``open → mitigating → resolved``). The
*business* invariant — a conflict may only reach ``resolved`` once it carries a
mitigation **and** an owner — is enforced one tier up, in the service, not here:
the repository is the structural backstop (legal edge + compare-and-set), the
service is the policy. ``owner``/``mitigation`` are plain nullable fields the
service fills through :func:`update_conflict_fields` before it resolves.

``project_ref`` (nullable) is the redaction key: :func:`list_conflicts` can drop
BANK/LEGAL rows *in the SQL query* so a sensitive conflict's ``source_ref`` never
leaves the read surface (the Wave-1 exclude-in-SQL pattern).

Statuses/kinds/severities are stored as their stable string *values* (never a
Python enum member name) so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares — the enum-name lesson.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

#: The conflict kinds (mirrors ``api.models.ConflictKind``). Validated at the
#: persistence boundary so a malformed kind can never reach a stored row.
CONFLICT_KINDS: frozenset[str] = frozenset({"merge", "perf", "budget", "security"})

#: The accepted severities (mirrors the ``Conflict``/``Incident`` contract and
#: the ``IncidentOpened`` event vocabulary).
CONFLICT_SEVERITIES: frozenset[str] = frozenset({"sev1", "sev2", "sev3", "sev4"})

#: The conflict lifecycle. ``resolved`` is terminal.
CONFLICT_STATUSES: frozenset[str] = frozenset({"open", "mitigating", "resolved"})

#: The allowed status edges. ``open`` may jump straight to ``resolved`` (a
#: conflict mitigated in one step) or move through ``mitigating`` first; a
#: ``resolved`` conflict is terminal. The *service* additionally refuses a
#: resolve that lacks an owner/mitigation — that policy is not encoded here.
CONFLICT_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"mitigating", "resolved"}),
    "mitigating": frozenset({"resolved", "open"}),
    "resolved": frozenset(),
}

#: Fields a caller may set through :func:`update_conflict_fields` (owner and the
#: mitigation plan). Status is never routed through here — it goes through the
#: transition-guarded :func:`set_conflict_status`.
_UPDATABLE_CONFLICT_FIELDS: frozenset[str] = frozenset({"owner", "mitigation"})


class InvalidConflictTransitionError(Exception):
    """Raised when a conflict status change is not an allowed edge in
    :data:`CONFLICT_TRANSITIONS` (a backward jump, or any move out of the
    terminal ``resolved`` state)."""


class ConflictResolvedError(Exception):
    """Raised when a field update (owner/mitigation) is attempted on a conflict
    that is already ``resolved`` — a resolved conflict is frozen."""


_CONFLICT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "source_ref",
    "severity",
    "status",
    "owner",
    "mitigation",
    "project_ref",
    "opened_at",
    "resolved_at",
    "version",
    "created_at",
    "updated_at",
)


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


def create_conflict(
    db_path: Path,
    *,
    kind: str,
    source_ref: str = "",
    severity: str = "sev3",
    project_ref: str | None = None,
    owner: str | None = None,
    mitigation: str | None = None,
    conflict_id: str | None = None,
    status: str = "open",
) -> dict:
    """Insert one ``conflict`` row and return it.

    ``kind`` must be one of :data:`CONFLICT_KINDS` and ``severity`` one of
    :data:`CONFLICT_SEVERITIES`; ``status`` defaults to ``open`` and must be a
    known status. ``owner``/``mitigation`` are optional at creation — a conflict
    routinely opens without either and acquires them before it is resolved."""
    if kind not in CONFLICT_KINDS:
        raise ValueError(f"conflict.kind must be one of {sorted(CONFLICT_KINDS)}, got {kind!r}")
    if severity not in CONFLICT_SEVERITIES:
        raise ValueError(
            f"conflict.severity must be one of {sorted(CONFLICT_SEVERITIES)}, got {severity!r}"
        )
    if status not in CONFLICT_STATUSES:
        raise ValueError(f"conflict.status must be one of {sorted(CONFLICT_STATUSES)}, got {status!r}")
    now = db.iso_now()
    record = {name: None for name in _CONFLICT_COLUMNS}
    record.update(
        {
            "id": conflict_id or db.new_id(),
            "kind": kind,
            "source_ref": source_ref or "",
            "severity": severity,
            "status": status,
            "owner": owner,
            "mitigation": mitigation,
            "project_ref": project_ref,
            "opened_at": now,
            "resolved_at": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_CONFLICT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _CONFLICT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO conflict ({columns}) VALUES ({placeholders})",
                record,
            )
    _mirror_conflict(record)
    return record


def _mirror_conflict(record: dict) -> None:
    """Best-effort dual-write of one conflict into PostgreSQL (SRV-01B slice 3).

    Called *after* the authoritative SQLite commit, and deliberately silent on
    failure — the same rule slice 2 established for `owner_item`, for the same
    two reasons. During dual-write the mirror is not load-bearing, so letting it
    raise would mean a migration step could take down the table it is
    migrating; and writing it first would allow the opposite and worse state, a
    mirror ahead of the system of record, which no reconciliation flags as
    wrong.

    Every mutating path calls this with the row as it now stands, not with the
    fields that changed: `update_conflict_fields` touches the caller's one or
    two fields plus `updated_at` and `version`, and the
    mirror has no other source for the rest.

    The mirror's health is reported by `conflict_store.divergence`, not by
    exceptions raised here. Imported lazily so the desktop and CLI entry points
    keep working on a machine with no PostgreSQL client library.
    """
    try:
        from command_center.db.conflict_store import PostgresConflictMirror

        PostgresConflictMirror().upsert(record)
    except Exception:  # noqa: BLE001 — the mirror must never break the real write
        _LOG.debug("Could not mirror conflict into PostgreSQL", exc_info=True)


def get_conflict(db_path: Path, conflict_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM conflict WHERE id = ?", (conflict_id,)
        ).fetchone()
        return db._row_to_dict(row)


def get_conflict_by_source_ref(db_path: Path, source_ref: str) -> dict | None:
    """Return the most recent conflict opened from ``source_ref`` (or ``None``).

    The dedup primitive behind the event intake: an ``IncidentOpened`` redelivery
    carrying the same ``source_ref`` must not open a second conflict. An empty
    ``source_ref`` never dedups (manual API conflicts may share the empty
    default) — the caller passes a real ref."""
    if not source_ref:
        return None
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM conflict WHERE source_ref = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (source_ref,),
        ).fetchone()
        return db._row_to_dict(row)


def list_conflicts(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List conflicts, newest first, optionally filtered by ``kind``, ``status``
    and/or ``owner``. ``limit``/``offset`` page the result (stable order:
    ``created_at DESC, id DESC``).

    ``exclude_projects`` drops rows for those projects *in the query* (the
    redaction policy — see :func:`_exclude_projects_clause`), so a page counts
    only visible rows rather than being trimmed after the fact."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if owner is not None:
        clauses.append("owner = ?")
        params.append(owner)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM conflict{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def update_conflict_fields(
    db_path: Path,
    conflict_id: str,
    *,
    expected_version: int,
    fields: dict,
) -> dict:
    """Compare-and-set update of a conflict's mutable fields (owner/mitigation).

    Refuses any key outside :data:`_UPDATABLE_CONFLICT_FIELDS`, refuses a stale
    ``version`` (:class:`db.LostUpdateError`), and refuses to touch a conflict
    that is already ``resolved`` (:class:`ConflictResolvedError`) — a resolved
    conflict is frozen. Bumps ``version`` and ``updated_at``."""
    unknown = set(fields) - _UPDATABLE_CONFLICT_FIELDS
    if unknown:
        raise ValueError(f"conflict update has non-updatable fields: {sorted(unknown)}")
    if not fields:
        raise ValueError("update_conflict_fields requires at least one field")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM conflict WHERE id = ?", (conflict_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such conflict: {conflict_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"conflict {conflict_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if row["status"] == "resolved":
                raise ConflictResolvedError(
                    f"conflict {conflict_id!r} is resolved and cannot be modified"
                )
            payload = dict(fields)
            payload["updated_at"] = now
            set_clause = ", ".join(f"{key} = :{key}" for key in payload)
            params = dict(payload)
            params["conflict_id"] = conflict_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE conflict SET {set_clause}, version = version + 1 "
                "WHERE id = :conflict_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"conflict {conflict_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM conflict WHERE id = ?", (conflict_id,)
            ).fetchone()
            record = dict(updated)
    # Outside the `with`, so the mirror runs only once SQLite has committed.
    # Mirroring inside the transaction would publish a row a rollback then
    # discards — a mirror ahead of the system of record, which reconciliation
    # reports but nothing else would.
    _mirror_conflict(record)
    return record


def _conflict_transition(
    conn: sqlite3.Connection,
    conflict_id: str,
    *,
    expected_version: int,
    new_status: str,
    now: str,
) -> dict:
    """CAS + transition-guarded status update inside an open transaction.

    Checks the caller's ``version`` first (a stale writer always loses as a stale
    writer), then refuses an illegal status edge, stamps ``resolved_at`` when the
    move is into ``resolved`` (the clearing branch below is unreachable while
    ``resolved`` is terminal — see the comment on it), bumps ``version`` and
    sets ``updated_at``. Returns the updated row dict."""
    row = conn.execute(
        "SELECT status, version FROM conflict WHERE id = ?", (conflict_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No such conflict: {conflict_id!r}")
    if row["version"] != expected_version:
        raise db.LostUpdateError(
            f"conflict {conflict_id!r} version mismatch: "
            f"expected {expected_version}, actual {row['version']}"
        )
    if new_status not in CONFLICT_STATUSES:
        raise ValueError(f"unknown conflict status {new_status!r}")
    if new_status not in CONFLICT_TRANSITIONS.get(row["status"], frozenset()):
        raise InvalidConflictTransitionError(
            f"conflict {conflict_id!r} cannot transition "
            f"{row['status']!r} -> {new_status!r}"
        )
    fields: dict[str, Any] = {"status": new_status, "updated_at": now}
    if new_status == "resolved":
        fields["resolved_at"] = now
    elif row["status"] == "resolved":
        # Unreachable while `resolved` is terminal: the allowlist check above
        # rejects every edge out of it, so no call reaches here with a resolved
        # row. Left in place as the correct behaviour if `resolved -> open` is
        # ever opened, and labelled because reading it as live behaviour is
        # exactly the mistake SRV-01B slice 3 made — its acceptance story was
        # built on this branch, and independent review had to disprove it.
        # Whoever opens that edge owns `test_a_resolved_conflict_is_terminal`
        # and the PostgreSQL mirror's assumptions along with it.
        fields["resolved_at"] = None
    set_clause = ", ".join(f"{key} = :{key}" for key in fields)
    params = dict(fields)
    params["conflict_id"] = conflict_id
    params["expected_version"] = expected_version
    cur = conn.execute(
        f"UPDATE conflict SET {set_clause}, version = version + 1 "
        "WHERE id = :conflict_id AND version = :expected_version",
        params,
    )
    if cur.rowcount != 1:
        raise db.LostUpdateError(
            f"conflict {conflict_id!r} update affected {cur.rowcount} rows"
        )
    updated = conn.execute(
        "SELECT * FROM conflict WHERE id = ?", (conflict_id,)
    ).fetchone()
    return dict(updated)


def set_conflict_status(
    db_path: Path, conflict_id: str, *, expected_version: int, status: str
) -> dict:
    """Compare-and-set status change guarded by the transition allowlist. The
    resolve *invariant* (owner + mitigation present) is the service's job — this
    layer only guarantees a legal edge and an atomic version bump."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            record = db._conflict_transition(
                conn, conflict_id, expected_version=expected_version,
                new_status=status, now=now,
            )
    # After the commit, and with the whole row rather than the fields this call
    # happened to touch.
    _mirror_conflict(record)
    return record
