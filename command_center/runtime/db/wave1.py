"""Wave 1 "new engine" table-families (W1-DATA-EVENTS): the persistence layer
behind the Советник (advisor_proposal), «Мой день» (owner_item) and Дайджест
(digest_item) surfaces.

This module is the **repository tier** of the Wave-1 stack (routes → service →
repository → db). It owns three additive tables that are wholly separate from
the autonomy `proposal` family in ``proposal.py`` — the names never collide and
the two carry different lifecycles. Every write goes through the shared
``connect()``/``transaction()`` primitives (WAL, ``BEGIN IMMEDIATE`` write lock,
per-row ``version`` compare-and-set), so the single-writer discipline the rest
of ``runtime.db`` follows holds here too.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)


def _exclude_projects_clause(
    exclude_projects: Iterable[str] | None, *, allow_null: bool
) -> tuple[str | None, list[str]]:
    """Build a ``WHERE`` fragment that drops rows whose ``project_ref`` is in
    ``exclude_projects`` — the redaction policy expressed *in SQL* so that a
    ``LIMIT``/``OFFSET`` page counts only visible rows (audit MED-2).

    ``allow_null`` keeps rows with no project attribution (``NULL``) — correct
    for ``owner_item``/``digest_item`` where the column is optional; pass
    ``False`` for ``advisor_proposal`` whose ``project_ref`` is ``NOT NULL``.
    Returns ``(None, [])`` when there is nothing to exclude."""
    projects = [p for p in (exclude_projects or []) if p]
    if not projects:
        return None, []
    placeholders = ", ".join("?" for _ in projects)
    if allow_null:
        clause = f"(project_ref IS NULL OR project_ref NOT IN ({placeholders}))"
    else:
        clause = f"project_ref NOT IN ({placeholders})"
    return clause, projects

# --------------------------------------------------------------------------
# advisor_proposal — Советник
# --------------------------------------------------------------------------

#: The advisor-proposal kinds (mirrors ``api.models.ProposalKind``). Validated at
#: the persistence boundary so a malformed kind can never reach a stored row.
ADVISOR_PROPOSAL_KINDS: frozenset[str] = frozenset(
    {"trend", "ux", "optimization", "competitor", "feedback"}
)

#: The advisor-proposal lifecycle. ``converted`` is the terminal state a
#: proposal reaches when it is promoted into a task; ``dismissed`` is the other
#: terminal state. Anything not listed here is refused before it reaches SQL.
ADVISOR_PROPOSAL_STATUSES: frozenset[str] = frozenset(
    {"new", "accepted", "dismissed", "converted"}
)

ADVISOR_PROPOSAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"accepted", "dismissed", "converted"}),
    "accepted": frozenset({"converted", "dismissed"}),
    "dismissed": frozenset(),
    "converted": frozenset(),
}


class InvalidAdvisorProposalTransitionError(Exception):
    """Raised when an advisor-proposal status change is not an allowed edge in
    ``ADVISOR_PROPOSAL_TRANSITIONS`` (a backward jump, or any move out of a
    terminal state)."""


_ADVISOR_PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "title",
    "body",
    "expected_gain",
    "effort",
    "project_ref",
    "status",
    "promoted_task_id",
    "version",
    "created_at",
    "updated_at",
)


def create_advisor_proposal(
    db_path: Path,
    *,
    kind: str,
    title: str,
    project_ref: str,
    body: str = "",
    expected_gain: str | None = None,
    effort: str | None = None,
    proposal_id: str | None = None,
    status: str = "new",
) -> dict:
    """Insert one ``advisor_proposal`` row in its initial state and return it.

    ``kind`` must be one of :data:`ADVISOR_PROPOSAL_KINDS` and ``project_ref``
    must be non-empty — a proposal always belongs to a project so it can be
    routed to the right board (and redacted when that project is sensitive)."""
    if kind not in ADVISOR_PROPOSAL_KINDS:
        raise ValueError(f"advisor_proposal.kind must be one of {sorted(ADVISOR_PROPOSAL_KINDS)}, got {kind!r}")
    if not project_ref or not str(project_ref).strip():
        raise ValueError("advisor_proposal.project_ref must be non-empty")
    if status not in ADVISOR_PROPOSAL_STATUSES:
        raise ValueError(f"advisor_proposal.status must be one of {sorted(ADVISOR_PROPOSAL_STATUSES)}, got {status!r}")
    now = db.iso_now()
    record = {name: None for name in _ADVISOR_PROPOSAL_COLUMNS}
    record.update(
        {
            "id": proposal_id or db.new_id(),
            "kind": kind,
            "title": title,
            "body": body,
            "expected_gain": expected_gain,
            "effort": effort,
            "project_ref": project_ref,
            "status": status,
            "promoted_task_id": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_ADVISOR_PROPOSAL_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _ADVISOR_PROPOSAL_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO advisor_proposal ({columns}) VALUES ({placeholders})",
                record,
            )
    _mirror_advisor_proposal(record)
    return record


def _mirror_advisor_proposal(record: dict) -> None:
    """Best-effort dual-write of one advisor proposal into PostgreSQL (slice 8).

    After the authoritative commit and silent on failure. The simplest mirror
    in the migration: no foreign key, no JSON column, nothing to convert but
    two timestamps.
    """
    try:
        from command_center.db.advisor_store import PostgresAdvisorProposalMirror

        PostgresAdvisorProposalMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror advisor_proposal into PostgreSQL", exc_info=True)



def get_advisor_proposal(db_path: Path, proposal_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM advisor_proposal WHERE id = ?", (proposal_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_advisor_proposals(
    db_path: Path,
    *,
    project: str | None = None,
    statuses: Iterable[str] | None = None,
    kind: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List advisor proposals, newest first, optionally filtered by project, a
    set of ``statuses`` and/or a ``kind``. ``limit``/``offset`` page the result
    (stable order: ``created_at DESC, id DESC``).

    ``exclude_projects`` drops rows for those projects *in the query* (the
    redaction policy — see :func:`_exclude_projects_clause`), so a page counts
    only visible rows rather than being trimmed after the fact."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project_ref = ?")
        params.append(project)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    statuses = list(statuses) if statuses is not None else None
    if statuses is not None:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects, allow_null=False)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM advisor_proposal{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def _advisor_proposal_transition(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expected_version: int,
    new_status: str,
    extra_fields: dict | None,
    now: str,
) -> dict:
    """CAS + transition-guarded status update inside an open transaction.

    Checks the caller's ``version`` before interpreting the request (a stale
    writer always loses as a stale writer), then refuses an illegal status edge,
    bumps ``version`` and sets ``updated_at``. Returns the updated row dict."""
    row = conn.execute(
        "SELECT status, version FROM advisor_proposal WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"No such advisor_proposal: {proposal_id!r}")
    if row["version"] != expected_version:
        raise db.LostUpdateError(
            f"advisor_proposal {proposal_id!r} version mismatch: "
            f"expected {expected_version}, actual {row['version']}"
        )
    if new_status not in ADVISOR_PROPOSAL_STATUSES:
        raise ValueError(f"unknown advisor_proposal status {new_status!r}")
    if new_status not in ADVISOR_PROPOSAL_TRANSITIONS.get(row["status"], frozenset()):
        raise InvalidAdvisorProposalTransitionError(
            f"advisor_proposal {proposal_id!r} cannot transition "
            f"{row['status']!r} -> {new_status!r}"
        )
    fields = dict(extra_fields or {})
    fields["status"] = new_status
    fields["updated_at"] = now
    set_clause = ", ".join(f"{key} = :{key}" for key in fields)
    params = dict(fields)
    params["proposal_id"] = proposal_id
    params["expected_version"] = expected_version
    cur = conn.execute(
        f"UPDATE advisor_proposal SET {set_clause}, version = version + 1 "
        "WHERE id = :proposal_id AND version = :expected_version",
        params,
    )
    if cur.rowcount != 1:
        raise db.LostUpdateError(
            f"advisor_proposal {proposal_id!r} update affected {cur.rowcount} rows"
        )
    updated = conn.execute(
        "SELECT * FROM advisor_proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    return dict(updated)


def set_advisor_proposal_status(
    db_path: Path, proposal_id: str, *, expected_version: int, status: str
) -> dict:
    """Compare-and-set status update guarded by the transition allowlist."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            record = db._advisor_proposal_transition(
                conn, proposal_id, expected_version=expected_version,
                new_status=status, extra_fields=None, now=now,
            )
    _mirror_advisor_proposal(record)
    return record


def promote_advisor_proposal(
    db_path: Path, proposal_id: str, *, expected_version: int, task_id: str
) -> dict:
    """Record that ``proposal_id`` was promoted into ``task_id``: move it to the
    terminal ``converted`` status and stamp ``promoted_task_id``, atomically.

    The caller creates the task through the existing tasks path *first*; this
    only records the link — the persistence layer never launches or creates a
    task itself. Idempotency is the caller's: a second promote of an already-
    ``converted`` proposal is refused by the transition guard, so a retry cannot
    silently create a second task-link."""
    if not task_id:
        raise ValueError("promote_advisor_proposal requires a task_id")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            record = db._advisor_proposal_transition(
                conn, proposal_id, expected_version=expected_version,
                new_status="converted", extra_fields={"promoted_task_id": task_id},
                now=now,
            )
    _mirror_advisor_proposal(record)
    return record


# --------------------------------------------------------------------------
# owner_item — «Мой день»
# --------------------------------------------------------------------------

_OWNER_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "detail",
    "due",
    "done",
    "source_ref",
    "project_ref",
    "version",
    "created_at",
    "updated_at",
)


def create_owner_item(
    db_path: Path,
    *,
    title: str,
    detail: str | None = None,
    due: str | None = None,
    source_ref: str | None = None,
    project_ref: str | None = None,
    item_id: str | None = None,
    done: bool = False,
) -> dict:
    """Insert one ``owner_item`` row and return it. ``title`` must be non-empty.

    ``project_ref`` (optional) records which project the item belongs to so the
    redaction policy can drop it in the SQL query when that project is sensitive;
    an un-attributed item (``None``) is never redacted."""
    if not title or not str(title).strip():
        raise ValueError("owner_item.title must be non-empty")
    now = db.iso_now()
    record = {
        "id": item_id or db.new_id(),
        "title": title,
        "detail": detail,
        "due": due,
        "done": 1 if done else 0,
        "source_ref": source_ref,
        "project_ref": project_ref,
        "version": 0,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_OWNER_ITEM_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _OWNER_ITEM_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO owner_item ({columns}) VALUES ({placeholders})",
                record,
            )
    _mirror_owner_item(record)
    return record


def _mirror_owner_item(record: dict) -> None:
    """Best-effort dual-write of one owner item into PostgreSQL (SRV-01B slice 2).

    Called *after* the authoritative SQLite commit, and deliberately silent on
    failure. Both follow from the same rule as the queue's mirror: during
    dual-write the mirror is not load-bearing, so letting it raise would mean a
    migration step could take down the table it is migrating. Writing it first
    would allow the opposite and worse state — a mirror ahead of the system of
    record, which no reconciliation would flag as wrong.

    The mirror's health is reported by `owner_item_store.divergence`, not by
    exceptions raised here. Imported lazily so the desktop and CLI entry points
    keep working on a machine with no PostgreSQL client library.
    """
    try:
        from command_center.db.owner_item_store import PostgresOwnerItemMirror

        PostgresOwnerItemMirror().upsert(record)
    except Exception:  # noqa: BLE001 — the mirror must never break the real write
        _LOG.warning("Could not mirror owner_item into PostgreSQL", exc_info=True)


def get_owner_item(db_path: Path, item_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM owner_item WHERE id = ?", (item_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_owner_items(
    db_path: Path,
    *,
    done: bool | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List owner items, newest first, optionally filtered by ``done`` state,
    paged by ``limit``/``offset``.

    ``exclude_projects`` drops rows attributed to those projects *in the query*
    (redaction — see :func:`_exclude_projects_clause`); un-attributed rows
    (``project_ref IS NULL``) are kept."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if done is not None:
        clauses.append("done = ?")
        params.append(1 if done else 0)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects, allow_null=True)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM owner_item{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def set_owner_item_done(
    db_path: Path, item_id: str, *, expected_version: int, done: bool
) -> dict:
    """Compare-and-set toggle of an owner item's ``done`` flag."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT version FROM owner_item WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such owner_item: {item_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"owner_item {item_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            cur = conn.execute(
                "UPDATE owner_item SET done = :done, updated_at = :now, "
                "version = version + 1 WHERE id = :id AND version = :expected",
                {"done": 1 if done else 0, "now": now, "id": item_id,
                 "expected": expected_version},
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"owner_item {item_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM owner_item WHERE id = ?", (item_id,)
            ).fetchone()
            result = dict(updated)
    _mirror_owner_item(result)
    return result


# --------------------------------------------------------------------------
# digest_item — Дайджест
# --------------------------------------------------------------------------

_DIGEST_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "body",
    "category",
    "refs_json",
    "project_ref",
    "day",
    "position",
    "created_at",
)


def create_digest_item(
    db_path: Path,
    *,
    title: str,
    body: str = "",
    category: str | None = None,
    refs: list[str] | None = None,
    project_ref: str | None = None,
    item_id: str | None = None,
    day: str | None = None,
    position: int = 0,
) -> dict:
    """Insert one write-once ``digest_item`` row and return it (with ``refs``
    decoded back to a list). ``refs`` is a list of opaque string references.

    ``day`` (``YYYY-MM-DD``) and ``position`` scope and order a row inside one
    day's morning digest; both are optional so an ad-hoc entry (no day, no rank)
    still round-trips. ``position`` must be non-negative."""
    if not title or not str(title).strip():
        raise ValueError("digest_item.title must be non-empty")
    if position < 0:
        raise ValueError(f"digest_item.position must be non-negative, got {position}")
    refs = list(refs or [])
    if not all(isinstance(ref, str) for ref in refs):
        raise ValueError("digest_item.refs must be a list of strings")
    now = db.iso_now()
    record = {
        "id": item_id or db.new_id(),
        "title": title,
        "body": body,
        "category": category,
        "refs_json": json.dumps(refs, ensure_ascii=False),
        "project_ref": project_ref,
        "day": day,
        "position": position,
        "created_at": now,
    }
    columns = ", ".join(_DIGEST_ITEM_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _DIGEST_ITEM_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO digest_item ({columns}) VALUES ({placeholders})",
                record,
            )
    # `record`, not the return value. `_decode_digest_row` pops `refs_json` and
    # replaces it with a decoded `refs` list, so mirroring what this function
    # returns would write the column's default on every row and leave the
    # mirror agreeing with nothing.
    _mirror_digest_item(record)
    return _decode_digest_row(record)


def _mirror_digest_item(record: dict) -> None:
    """Best-effort dual-write of one digest item into PostgreSQL (SRV-01B slice 4).

    After the authoritative commit and silent on failure, for the reasons
    slices 2 and 3 established: during dual-write the mirror is not
    load-bearing, so letting it raise would mean a migration step could take
    down the table it is migrating, and writing it first would allow a mirror
    ahead of the system of record — the one state no reconciliation flags.
    """
    try:
        from command_center.db.digest_item_store import PostgresDigestItemMirror

        PostgresDigestItemMirror().upsert(record)
    except Exception:  # noqa: BLE001 — the mirror must never break the real write
        _LOG.warning("Could not mirror digest_item into PostgreSQL", exc_info=True)


def _mirror_digest_day_deletion(day: str) -> None:
    """Best-effort mirror of a whole-day digest deletion.

    The digest engine rebuilds a day by deleting it and re-inserting, so
    without this the mirror keeps every superseded row and reconciliation
    reports it permanently ahead of the authority. Mirrored as the same
    predicate rather than as the ids removed: deriving ids would mean reading
    the authority before the delete, which is an extra query and a race with
    the rebuild this is following.
    """
    try:
        from command_center.db.digest_item_store import PostgresDigestItemMirror

        PostgresDigestItemMirror().delete_day(day)
    except Exception:  # noqa: BLE001 — the mirror must never break the real write
        _LOG.warning("Could not mirror digest_item day deletion into PostgreSQL", exc_info=True)


def delete_digest_items_for_day(db_path: Path, day: str) -> int:
    """Delete every digest row built for ``day`` and return how many were
    removed. The idempotency primitive behind a per-day rebuild: the digest
    engine deletes the day, then re-inserts the freshly assembled rows, so a
    second build for the same day replaces rather than duplicates."""
    if not day or not str(day).strip():
        raise ValueError("delete_digest_items_for_day requires a non-empty day")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            cur = conn.execute("DELETE FROM digest_item WHERE day = ?", (day,))
            removed = cur.rowcount
    # Outside the `with`: the mirror follows the committed delete, never a
    # delete a rollback could still discard.
    _mirror_digest_day_deletion(day)
    return removed


def list_digest_items_for_day(
    db_path: Path, day: str, *, exclude_projects: Iterable[str] | None = None
) -> list[dict]:
    """Return one day's digest, in stable assembly order (``position`` asc, then
    ``created_at``/``id`` to fully break any tie). Distinct from
    :func:`list_digest_items`, which pages the whole table newest-first.

    ``exclude_projects`` drops rows attributed to those projects (redaction);
    un-attributed rows (``project_ref IS NULL``) are kept."""
    if not day or not str(day).strip():
        raise ValueError("list_digest_items_for_day requires a non-empty day")
    clauses = ["day = ?"]
    params: list[Any] = [day]
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects, allow_null=True)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM digest_item WHERE {' AND '.join(clauses)} "
            "ORDER BY position ASC, created_at ASC, id ASC",
            params,
        ).fetchall()
        return [_decode_digest_row(dict(row)) for row in rows]


def get_digest_item(db_path: Path, item_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM digest_item WHERE id = ?", (item_id,)
        ).fetchone()
        return _decode_digest_row(dict(row)) if row is not None else None


def list_digest_items(
    db_path: Path,
    *,
    category: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List digest items, newest first, optionally filtered by ``category``,
    paged by ``limit``/``offset``.

    ``exclude_projects`` drops rows attributed to those projects *in the query*
    (redaction — see :func:`_exclude_projects_clause`); un-attributed rows
    (``project_ref IS NULL``) are kept, so a page counts only visible rows."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects, allow_null=True)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM digest_item{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_decode_digest_row(dict(row)) for row in rows]


def list_digest_items_stored(db_path: Path) -> list[dict]:
    """Every digest row in the shape SQLite **stores**, for reconciliation.

    Every other reader here returns :func:`_decode_digest_row` output, which
    pops ``refs_json`` and substitutes a decoded ``refs`` list — a view for
    callers, and the right default for them. Reconciliation is not one of those
    callers: it compares what the authority stores against what the mirror
    stores, and fed a decoded row it sees ``refs_json`` missing on one side and
    present on the other, so it reports **every** digest row as divergent.

    That is the permanently-red cutover gate this migration keeps almost
    building, reached from the direction nobody was watching: not a wrong
    conversion, but a reconciliation pointed at the wrong shape. Independent
    review found it by asking what an operator would actually call — there was
    no answer, because until this function existed the only readers were
    decoding ones (SRV-01B slice 4, second acceptance round).

    Deliberately without ``exclude_projects``: redaction is a read-surface
    policy, and a reconciliation that skipped redacted rows would certify a
    cutover over a subset of the table while reporting it as the whole.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM digest_item ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def _decode_digest_row(row: dict) -> dict:
    """Return a copy of a digest row with ``refs_json`` decoded to a ``refs``
    list (the JSON column stays internal to the repository)."""
    out = dict(row)
    raw = out.pop("refs_json", "[]")
    out["refs"] = json.loads(raw) if raw else []
    return out
