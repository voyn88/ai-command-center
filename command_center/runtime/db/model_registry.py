"""Wave-3 model-registry table families (VOYN-W3-MODELS): the persistence tier
behind the AI-model catalog — ``model_entry`` (one mutable current-state row per
registered model, external or local) and ``model_event`` (the append-only
governance log that makes a model's history fully traceable).

This module is the **repository tier** of the Wave-3 stack (routes → service →
repository → db). It owns two additive tables that are wholly separate from every
other family in ``runtime.db``; the names never collide. Every write goes through
the shared ``connect()``/``transaction()`` primitives (WAL, ``BEGIN IMMEDIATE``
write lock, per-row ``version`` compare-and-set), so the single-writer discipline
the rest of ``runtime.db`` follows holds here too.

Two invariants are enforced *at this boundary* so a higher layer cannot bypass
them:

* **Every model has a register event.** :func:`create_model_entry` writes the
  ``register`` governance event in the *same transaction* as the row, so a stored
  model can never exist without the first entry in its history (traceability
  starts at creation, not best-effort afterward).
* **Status only moves along allowed edges.** :func:`set_model_status` refuses a
  transition that is not in :data:`MODEL_STATUS_TRANSITIONS` and records a
  ``status-change`` governance event for the move — the download lifecycle
  (available → downloading → installed) is a real, guarded state machine, not a
  free-form string field.

Statuses/kinds/actions are stored as their stable string *values* (never a Python
enum's member name), so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they do
for the other table-family modules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: External (a hosted API provider) vs. local (run on the operator's machine).
MODEL_KINDS: frozenset[str] = frozenset({"external", "local"})

#: A model's availability lifecycle (mirrors ``api.models.ModelStatus``).
#: ``downloading`` and ``installed`` are the local-download milestones; ``error``
#: is the failure state a download or a probe can land in.
MODEL_STATUSES: frozenset[str] = frozenset(
    {"available", "downloading", "installed", "error"}
)

#: Allowed status edges. A local model is downloaded once (available →
#: downloading → installed); either end can fail to ``error`` and be retried; an
#: installed/errored model can be re-marked ``available`` after a re-probe. There
#: is no ``downloading`` self-loop — a progress tick is *not* a status change (it
#: goes through :func:`update_download_progress`), so the state machine stays a
#: state machine.
MODEL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "available": frozenset({"downloading", "installed", "error"}),
    "downloading": frozenset({"installed", "error"}),
    "installed": frozenset({"available", "error"}),
    "error": frozenset({"available", "downloading"}),
}

#: The governance-log action vocabulary. ``register`` is written once at
#: creation; ``download-request``/``download-progress`` track a local download;
#: ``assign`` records a model bound to a task/agent; ``use`` records an actual
#: invocation; ``status-change`` is emitted by every guarded status move.
MODEL_ACTIONS: frozenset[str] = frozenset(
    {
        "register",
        "download-request",
        "download-progress",
        "assign",
        "use",
        "status-change",
    }
)


class InvalidModelStatusTransitionError(Exception):
    """Raised when a model status change is not an allowed edge in
    :data:`MODEL_STATUS_TRANSITIONS` (a backward jump, or any move that skips the
    download lifecycle)."""


# --------------------------------------------------------------------------
# model_entry
# --------------------------------------------------------------------------

_MODEL_ENTRY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "provider",
    "status",
    "cost",
    "quality",
    "latency_ms",
    "provenance",
    "download_progress",
    "version",
    "created_at",
    "updated_at",
)


def create_model_entry(
    db_path: Path,
    *,
    name: str,
    kind: str,
    provider: str | None = None,
    status: str = "available",
    cost: float | None = None,
    quality: float | None = None,
    latency_ms: int | None = None,
    provenance: str | None = None,
    actor: str | None = None,
    model_id: str | None = None,
) -> dict:
    """Insert one ``model_entry`` row and its first (``register``) governance
    event, atomically, and return the row.

    ``kind`` and ``status`` are validated against their allowlists at this
    boundary — a malformed value can never reach a stored row. The ``register``
    event is written in the *same transaction* as the row, so a model always
    starts its history with a traceable creation record."""
    if not name or not str(name).strip():
        raise ValueError("model_entry.name must be non-empty")
    if kind not in MODEL_KINDS:
        raise ValueError(
            f"model_entry.kind must be one of {sorted(MODEL_KINDS)}, got {kind!r}"
        )
    if status not in MODEL_STATUSES:
        raise ValueError(
            f"model_entry.status must be one of {sorted(MODEL_STATUSES)}, got {status!r}"
        )
    now = db.iso_now()
    record = {
        "id": model_id or db.new_id(),
        "name": name,
        "kind": kind,
        "provider": provider,
        "status": status,
        "cost": cost,
        "quality": quality,
        "latency_ms": latency_ms,
        "provenance": provenance,
        "download_progress": 0,
        "version": 0,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_MODEL_ENTRY_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _MODEL_ENTRY_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO model_entry ({columns}) VALUES ({placeholders})",
                record,
            )
            event = _append_model_event(
                conn,
                model_id=record["id"],
                action="register",
                actor=actor,
                target_ref=None,
                provenance=provenance,
                metadata={"kind": kind, "status": status},
                now=now,
            )
    # Entry before event, after the commit: the event references the entry, so
    # mirroring the child first would be refused by the foreign key.
    _mirror_model_entry(record)
    _mirror_model_event(event)
    return dict(record)


def _mirror_model_entry(record: dict) -> None:
    """Best-effort dual-write of one model entry into PostgreSQL (SRV-01B slice 6).

    After the authoritative commit and silent on failure, as in slices 2-5.
    """
    try:
        from command_center.db.model_registry_store import PostgresModelEntryMirror

        PostgresModelEntryMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror model_entry into PostgreSQL", exc_info=True)


def _mirror_model_event(record: dict) -> None:
    """Best-effort dual-write of one governance event into PostgreSQL.

    Takes the **stored** record: it carries the id SQLite minted and the raw
    `metadata_json`, and the decoded row has neither. Mirroring a decoded row
    would send `id=None` into a column PostgreSQL refuses to take a non-DEFAULT
    value for, so every event would be lost - silently, since this swallows.
    """
    try:
        from command_center.db.model_registry_store import PostgresModelEventMirror

        PostgresModelEventMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror model_event into PostgreSQL", exc_info=True)


def get_model_entry(db_path: Path, model_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM model_entry WHERE id = ?", (model_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_model_entries(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List models, newest first, optionally filtered by ``kind`` and/or
    ``status``. ``limit``/``offset`` page the result."""
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
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM model_entry{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def set_model_status(
    db_path: Path,
    model_id: str,
    *,
    expected_version: int,
    status: str,
    download_progress: int | None = None,
    action: str = "status-change",
    actor: str | None = None,
    provenance: str | None = None,
) -> dict:
    """Compare-and-set a model's ``status`` (guarded by the transition
    allowlist), optionally stamping ``download_progress``, and record a governance
    event for the move — all in one transaction.

    ``action`` lets a caller label the move (e.g. ``download-request`` for the
    available → downloading edge) while still going through the same guarded
    path; it must be a known governance action."""
    if status not in MODEL_STATUSES:
        raise ValueError(f"unknown model_entry status {status!r}")
    if action not in MODEL_ACTIONS:
        raise ValueError(f"unknown model event action {action!r}")
    if download_progress is not None and not (0 <= download_progress <= 100):
        raise ValueError("download_progress must be between 0 and 100")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM model_entry WHERE id = ?", (model_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such model_entry: {model_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"model_entry {model_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if status not in MODEL_STATUS_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidModelStatusTransitionError(
                    f"model_entry {model_id!r} cannot transition "
                    f"{row['status']!r} -> {status!r}"
                )
            fields: dict[str, Any] = {"status": status, "updated_at": now}
            if download_progress is not None:
                fields["download_progress"] = download_progress
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["model_id"] = model_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE model_entry SET {set_clause}, version = version + 1 "
                "WHERE id = :model_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"model_entry {model_id!r} update affected {cur.rowcount} rows"
                )
            event = _append_model_event(
                conn,
                model_id=model_id,
                action=action,
                actor=actor,
                target_ref=None,
                provenance=provenance,
                metadata={"from": row["status"], "to": status},
                now=now,
            )
            entry = dict(
                conn.execute(
                    "SELECT * FROM model_entry WHERE id = ?", (model_id,)
                ).fetchone()
            )
    # Entry first, then its event, and both after the commit: the event's
    # foreign key would refuse a child whose parent is not mirrored yet.
    _mirror_model_entry(entry)
    _mirror_model_event(event)
    return entry


def update_download_progress(
    db_path: Path,
    model_id: str,
    *,
    expected_version: int,
    progress: int,
    actor: str | None = None,
) -> dict:
    """Record a download-progress tick (0..100) without changing status.

    Only valid while the model is ``downloading`` — a progress tick against any
    other state is a defect, refused rather than silently stored. CAS-guarded and
    logged as a ``download-progress`` governance event so the whole transfer is
    traceable, not just its endpoints."""
    if not (0 <= progress <= 100):
        raise ValueError("progress must be between 0 and 100")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM model_entry WHERE id = ?", (model_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such model_entry: {model_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"model_entry {model_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if row["status"] != "downloading":
                raise InvalidModelStatusTransitionError(
                    f"model_entry {model_id!r} is {row['status']!r}, not downloading — "
                    "progress ticks are only valid mid-download"
                )
            cur = conn.execute(
                "UPDATE model_entry SET download_progress = :progress, "
                "updated_at = :now, version = version + 1 "
                "WHERE id = :model_id AND version = :expected_version",
                {
                    "progress": progress,
                    "now": now,
                    "model_id": model_id,
                    "expected_version": expected_version,
                },
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"model_entry {model_id!r} update affected {cur.rowcount} rows"
                )
            event = _append_model_event(
                conn,
                model_id=model_id,
                action="download-progress",
                actor=actor,
                target_ref=None,
                provenance=None,
                metadata={"progress": progress},
                now=now,
            )
            entry = dict(
                conn.execute(
                    "SELECT * FROM model_entry WHERE id = ?", (model_id,)
                ).fetchone()
            )
    # Entry first, then its event, and both after the commit: the event's
    # foreign key would refuse a child whose parent is not mirrored yet.
    _mirror_model_entry(entry)
    _mirror_model_event(event)
    return entry


# --------------------------------------------------------------------------
# model_event — the governance log (append-only, per-model monotonic seq)
# --------------------------------------------------------------------------


def _next_model_seq(conn: sqlite3.Connection, model_id: str) -> int:
    """The next per-model event sequence number (1-based, gap-free within a
    transaction) — computed under the open write lock so two writers can never
    mint the same ``seq`` (the ``UNIQUE(model_id, seq)`` constraint is the final
    backstop)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM model_event WHERE model_id = ?",
        (model_id,),
    ).fetchone()
    return int(row["max_seq"]) + 1


def _append_model_event(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    action: str,
    actor: str | None,
    target_ref: str | None,
    provenance: str | None,
    metadata: dict | None,
    now: str,
) -> dict:
    """Append one governance-log row inside an already-open transaction and
    return it. Validates ``action`` against the allowlist so a malformed action
    can never reach the log."""
    if action not in MODEL_ACTIONS:
        raise ValueError(f"unknown model event action {action!r}")
    seq = _next_model_seq(conn, model_id)
    record = {
        "model_id": model_id,
        "seq": seq,
        "action": action,
        "actor": actor,
        "target_ref": target_ref,
        "provenance": provenance,
        "metadata_json": json.dumps(metadata, ensure_ascii=False) if metadata else None,
        "created_at": now,
    }
    cur = conn.execute(
        "INSERT INTO model_event "
        "(model_id, seq, action, actor, target_ref, provenance, metadata_json, created_at) "
        "VALUES (:model_id, :seq, :action, :actor, :target_ref, :provenance, "
        ":metadata_json, :created_at)",
        record,
    )
    # SQLite mints the id; the mirror needs *this* id, because reconciliation
    # matches rows by it and PostgreSQL would otherwise generate its own.
    record["id"] = cur.lastrowid
    # Returns the **stored** shape, not the decoded one. Callers that owe an
    # API response decode it themselves; callers that owe the mirror a row need
    # `metadata_json` and `id`, both of which `_decode_event_row` removes.
    return record


def append_model_event(
    db_path: Path,
    model_id: str,
    *,
    action: str,
    actor: str | None = None,
    target_ref: str | None = None,
    provenance: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Append a standalone governance event (e.g. ``assign`` or ``use``) for a
    model. Raises ``KeyError`` if the model does not exist."""
    if action not in MODEL_ACTIONS:
        raise ValueError(f"unknown model event action {action!r}")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM model_entry WHERE id = ?", (model_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"No such model_entry: {model_id!r}")
            event = _append_model_event(
                conn,
                model_id=model_id,
                action=action,
                actor=actor,
                target_ref=target_ref,
                provenance=provenance,
                metadata=metadata,
                now=now,
            )
    _mirror_model_event(event)
    # `keep_id=False`: this path never returned an id before slice 6 gave the
    # record one for the mirror, and widening a public return is still a shape
    # change nobody asked for.
    return _decode_event_row(event, keep_id=False)


def list_model_events(db_path: Path, model_id: str) -> list[dict]:
    """The full governance history for a model, oldest → newest (``seq`` order) —
    the traceable record every action appends to."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM model_event WHERE model_id = ? ORDER BY seq ASC",
            (model_id,),
        ).fetchall()
        return [_decode_event_row(dict(row)) for row in rows]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def list_model_events_stored(db_path: Path) -> list[dict]:
    """Every governance event in the shape SQLite **stores**, for reconciliation.

    :func:`list_model_events` returns :func:`_decode_event_row` output, which
    pops ``metadata_json`` in favour of a decoded ``metadata`` — the right
    default for callers, and the wrong input for a reconciliation, which
    compares what the authority stores against what the mirror stores. Fed the
    decoded rows it would report every event divergent on the JSON column.

    Slice 4 shipped without this for ``digest_item`` and the omission was only
    found because independent review asked what an operator would actually
    call. Written here in the same slice as the mirror rather than after a
    rejection.

    Ordered by ``id`` — the authority's own sequence — so the comparison runs
    against the same order the mirror returns.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM model_event ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]


def _decode_event_row(row: dict, *, keep_id: bool = True) -> dict:
    """Return a copy of a ``model_event`` row with ``metadata_json`` decoded to a
    ``metadata`` dict (the JSON column stays internal to the repository).

    ``keep_id`` exists because the two callers of this function historically
    returned **different** shapes, and preserving that is the whole point.
    :func:`list_model_events` reads ``SELECT *`` and has always included ``id``;
    :func:`append_model_event` built its dict by hand and never had one, because
    SQLite minted the id and nobody read it back.

    Slice 6 gave the append path an id — the mirror needs it, since PostgreSQL
    refuses a non-DEFAULT value for a ``GENERATED ALWAYS`` column and
    reconciliation matches rows by id. The first attempt at keeping the public
    shapes intact dropped ``id`` here unconditionally, on the belief that it had
    only just become visible. That was wrong: it had always been in the list
    reader's output, so the "fix" silently narrowed a public reader.
    Independent review caught it by running the same probe at both SHAs. Now
    each caller asks for the shape it always had, and
    ``test_the_public_event_shapes_are_unchanged`` pins both.

    Reconciliation needs the id under a stable column name and therefore reads
    :func:`list_model_events_stored`, which does no decoding at all."""
    out = dict(row)
    raw = out.pop("metadata_json", None)
    if not keep_id:
        out.pop("id", None)
    out["metadata"] = json.loads(raw) if raw else {}
    return out
