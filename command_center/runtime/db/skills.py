"""VOYN-W0-AICC-SKILL-ACQUISITION-REM: the persistence tier behind the skill
acquisition registry (routes -> service -> **repository** -> db).

This module owns four additive tables, wholly separate from every other family
in :mod:`command_center.runtime.db` (schema v26; see ``schema.py`` for the DDL
and the family-level design note):

* ``skill_source`` -- one mutable current-state row per allowlisted source an
  agent may pull skills from, guarded by a ``lock_version`` compare-and-set
  column and an explicit status allowlist (:data:`SKILL_SOURCE_TRANSITIONS`,
  ``proposed -> approved -> revoked``). A source starts ``proposed`` and stays
  inert — no skill may be registered against it — until a human approves it.
* ``skill_item`` -- one mutable current-state row per candidate/acquired skill,
  guarded the same way (:data:`SKILL_ITEM_TRANSITIONS`, ``candidate ->
  acquiring -> acquired``, with ``acquired -> revoked`` and ``candidate ->
  rejected`` as the other two edges).
* ``skill_acquisition_log`` -- append-only. One immutable row per lifecycle
  action, ordered by a per-skill monotonic ``seq``.
* ``skill_outcome`` -- append-only. One row per task a skill was (or was not,
  for the ``used=0`` baseline) applied to, the evidence the effect measurement
  in :mod:`command_center.skills.service` is computed from.

Two invariants are enforced *at this boundary* so a higher layer cannot bypass
them, both closing gaps an earlier version of this module shipped with:

* **A candidate can only ever reference an approved source, and the check is
  atomic with the insert.** :func:`create_skill_candidate` reads the source's
  status and writes the new ``skill_item`` row inside the *same*
  ``BEGIN IMMEDIATE`` transaction (the shared ``connect()``/``transaction()``
  primitives serialise every writer through this lock), so a concurrent
  ``revoke_source`` can never land between the check and the insert the way it
  could when the two ran as separate transactions.
* **Acquiring a skill claims it, via a guarded status transition, before the
  (possibly side-effecting) executor ever runs.** :func:`claim_skill_item` is a
  compare-and-set ``candidate -> acquiring`` on its own, called by the service
  *before* it invokes the injected executor; :func:`finalize_skill_item_acquisition`
  and :func:`fail_skill_item_acquisition` land the result afterwards. Two
  concurrent callers can therefore never both invoke the executor for the same
  skill — the loser's claim fails the compare-and-set and never reaches it —
  where a single check-then-CAS-write around the executor call would have let
  both through and silently dropped whichever lost the final write.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import command_center.runtime.db as db  # facade (late-bound; see docstring)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: What a source *is*: an MCP registry, an Agent Skills catalogue, a CLI-tool
#: index, or an internal repo doc/ADR/runbook — the reuse-before-creation set
#: named by the owner idea, before any bespoke format is considered.
SKILL_SOURCE_KINDS: frozenset[str] = frozenset(
    {"mcp_registry", "agent_skill_catalog", "cli_tool_index", "repo_doc"}
)

#: A source's lifecycle. ``revoked`` is terminal.
SKILL_SOURCE_STATUSES: frozenset[str] = frozenset({"proposed", "approved", "revoked"})

#: The only allowed source edges. A source starts ``proposed`` and stays inert
#: (no candidate may reference it — enforced in :func:`create_skill_candidate`)
#: until a human calls :func:`transition_skill_source` to ``approved``. This is
#: the human gate on the first connection to a new source the owner idea
#: requires: nothing in this module or its callers may drive this edge itself.
SKILL_SOURCE_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "revoked"}),
    "approved": frozenset({"revoked"}),
    "revoked": frozenset(),
}

#: What a skill *is* (mirrors ``api.models.SkillItemKind``).
SKILL_ITEM_KINDS: frozenset[str] = frozenset(
    {"mcp_server", "agent_skill", "cli_tool", "doc_reference"}
)

#: A skill's lifecycle. ``rejected``/``revoked`` are terminal.
SKILL_ITEM_STATUSES: frozenset[str] = frozenset(
    {"candidate", "acquiring", "acquired", "rejected", "revoked"}
)

#: The only allowed skill edges. ``acquiring`` is the exclusive-claim state: a
#: failed acquisition attempt returns the skill to ``candidate`` (retryable),
#: never anywhere else.
SKILL_ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"acquiring", "rejected"}),
    "acquiring": frozenset({"acquired", "candidate"}),
    "acquired": frozenset({"revoked"}),
    "rejected": frozenset(),
    "revoked": frozenset(),
}

#: The acquisition-log action vocabulary. Every guarded transition on
#: ``skill_item`` writes exactly one of these.
SKILL_LOG_ACTIONS: frozenset[str] = frozenset(
    {"registered", "acquiring", "acquired", "acquire_failed", "rejected", "revoked"}
)


class InvalidSkillSourceTransitionError(Exception):
    """Raised when a source status change is not an allowed edge in
    :data:`SKILL_SOURCE_TRANSITIONS`."""


class InvalidSkillItemTransitionError(Exception):
    """Raised when a skill status change is not an allowed edge in
    :data:`SKILL_ITEM_TRANSITIONS`."""


_SKILL_SOURCE_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "origin",
    "status",
    "proposed_by",
    "lock_version",
    "created_at",
    "updated_at",
)

_SKILL_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "source_id",
    "name",
    "kind",
    "version",
    "content_hash",
    "task_class",
    "provenance",
    "status",
    "lock_version",
    "created_at",
    "updated_at",
)


# --------------------------------------------------------------------------
# skill_source — propose / approve / revoke / get / list
# --------------------------------------------------------------------------


def create_skill_source(
    db_path: Path,
    *,
    kind: str,
    origin: str,
    proposed_by: str = "",
    source_id: str | None = None,
) -> dict:
    """Insert one ``skill_source`` row (always ``proposed``) and return it.

    ``kind`` must be one of :data:`SKILL_SOURCE_KINDS` and ``origin`` must be
    non-empty and not already proposed (``origin`` is unique). The existence
    check and the insert run inside one transaction, so the specific
    ``IntegrityError`` raised on a duplicate ``origin`` is attributed correctly
    rather than assumed — a caller-supplied ``source_id`` colliding with an
    existing row (a distinct constraint) is never mislabeled as a duplicate
    origin."""
    if kind not in SKILL_SOURCE_KINDS:
        raise ValueError(
            f"skill_source.kind must be one of {sorted(SKILL_SOURCE_KINDS)}, got {kind!r}"
        )
    if not (origin or "").strip():
        raise ValueError("skill_source.origin must be non-empty")
    now = db.iso_now()
    record = {
        "id": source_id or db.new_id(),
        "kind": kind,
        "origin": origin,
        "status": "proposed",
        "proposed_by": proposed_by or "",
        "lock_version": 0,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_SKILL_SOURCE_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _SKILL_SOURCE_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            existing = conn.execute(
                "SELECT 1 FROM skill_source WHERE origin = ?", (origin,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"skill_source.origin already proposed: {origin!r}")
            try:
                conn.execute(
                    f"INSERT INTO skill_source ({columns}) VALUES ({placeholders})",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                # The origin check above just ran clean, so a constraint firing
                # here is not the origin uniqueness rule — most likely a
                # caller-supplied `source_id` colliding with an existing row.
                # Attributed to what actually failed rather than reusing the
                # origin message a `source_id` collision would make false.
                raise ValueError(f"skill_source insert failed: {exc}") from exc
    return record


def get_skill_source(db_path: Path, source_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM skill_source WHERE id = ?", (source_id,)
        ).fetchone()
        return db._row_to_dict(row)


def get_skill_source_by_origin(db_path: Path, origin: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM skill_source WHERE origin = ?", (origin,)
        ).fetchone()
        return db._row_to_dict(row)


def list_skill_sources(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List sources, newest first, optionally filtered by ``kind``/``status``."""
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
            f"SELECT * FROM skill_source{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def transition_skill_source(
    db_path: Path,
    source_id: str,
    *,
    expected_version: int,
    to_status: str,
    actor: str,
) -> dict:
    """Compare-and-set a source's ``status`` along an allowed edge of
    :data:`SKILL_SOURCE_TRANSITIONS`. Raises ``KeyError`` if the source does not
    exist, :class:`InvalidSkillSourceTransitionError` for a disallowed edge, and
    ``db.LostUpdateError`` on a version mismatch (a concurrent writer)."""
    if not (actor or "").strip():
        raise ValueError("actor must be non-empty (the transition needs who acted)")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT * FROM skill_source WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such skill_source: {source_id!r}")
            if row["lock_version"] != expected_version:
                raise db.LostUpdateError(
                    f"skill_source {source_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['lock_version']}"
                )
            if to_status not in SKILL_SOURCE_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidSkillSourceTransitionError(
                    f"skill_source {source_id!r} cannot transition "
                    f"{row['status']!r} -> {to_status!r}"
                )
            cur = conn.execute(
                "UPDATE skill_source SET status = :status, updated_at = :now, "
                "lock_version = lock_version + 1 "
                "WHERE id = :source_id AND lock_version = :expected_version",
                {
                    "status": to_status,
                    "now": now,
                    "source_id": source_id,
                    "expected_version": expected_version,
                },
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"skill_source {source_id!r} update affected {cur.rowcount} rows"
                )
            updated = dict(
                conn.execute(
                    "SELECT * FROM skill_source WHERE id = ?", (source_id,)
                ).fetchone()
            )
    return updated


# --------------------------------------------------------------------------
# skill_item — register / get / list
# --------------------------------------------------------------------------


def create_skill_candidate(
    db_path: Path,
    *,
    source_id: str,
    name: str,
    kind: str,
    content_hash: str,
    version: str = "",
    task_class: str = "",
    provenance: str = "",
    item_id: str | None = None,
) -> dict:
    """Insert one ``skill_item`` row (always ``candidate``) and return it.

    The source's ``approved`` status is checked and the row is inserted inside
    one ``BEGIN IMMEDIATE`` transaction — see the module docstring — so the
    allowlist this enforces cannot be defeated by a source revoked between a
    separate check and a separate insert."""
    if not (name or "").strip():
        raise ValueError("skill_item.name must be non-empty")
    if kind not in SKILL_ITEM_KINDS:
        raise ValueError(
            f"skill_item.kind must be one of {sorted(SKILL_ITEM_KINDS)}, got {kind!r}"
        )
    if not (content_hash or "").strip():
        raise ValueError("skill_item.content_hash must be non-empty (skills are pinned)")
    now = db.iso_now()
    record = {
        "id": item_id or db.new_id(),
        "source_id": source_id,
        "name": name,
        "kind": kind,
        "version": version or "",
        "content_hash": content_hash,
        "task_class": task_class or "",
        "provenance": provenance or "",
        "status": "candidate",
        "lock_version": 0,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_SKILL_ITEM_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _SKILL_ITEM_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            source = conn.execute(
                "SELECT status FROM skill_source WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise ValueError(f"no such skill_source: {source_id!r}")
            if source["status"] != "approved":
                raise ValueError(
                    f"skill_source {source_id!r} is not approved "
                    f"(status={source['status']!r}); only an approved source may "
                    "have candidates registered against it"
                )
            conn.execute(
                f"INSERT INTO skill_item ({columns}) VALUES ({placeholders})",
                record,
            )
            _append_skill_log(
                conn,
                skill_id=record["id"],
                action="registered",
                actor=None,
                from_status="",
                to_status="candidate",
                detail="",
                metadata=None,
                now=now,
            )
    return record


def get_skill_item(db_path: Path, item_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM skill_item WHERE id = ?", (item_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_skill_items(
    db_path: Path,
    *,
    source_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    task_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List skills, newest first, optionally filtered by ``source_id``/``kind``/
    ``status``/``task_class``. ``limit``/``offset`` page the result."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if task_class is not None:
        clauses.append("task_class = ?")
        params.append(task_class)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM skill_item{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# skill_item lifecycle — claim / finalize / fail / reject / revoke
# --------------------------------------------------------------------------


def _transition_skill_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    expected_version: int,
    to_status: str,
    action: str,
    actor: str | None,
    detail: str,
    metadata: dict | None,
    now: str,
) -> dict:
    """Compare-and-set a skill's ``status`` along an allowed edge of
    :data:`SKILL_ITEM_TRANSITIONS` and append its log line, inside an
    already-open transaction. Shared by every public transition below so the
    guard and the log write can never drift apart."""
    if action not in SKILL_LOG_ACTIONS:
        raise ValueError(f"unknown skill acquisition-log action {action!r}")
    row = conn.execute(
        "SELECT * FROM skill_item WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No such skill_item: {item_id!r}")
    if row["lock_version"] != expected_version:
        raise db.LostUpdateError(
            f"skill_item {item_id!r} version mismatch: "
            f"expected {expected_version}, actual {row['lock_version']}"
        )
    if to_status not in SKILL_ITEM_TRANSITIONS.get(row["status"], frozenset()):
        raise InvalidSkillItemTransitionError(
            f"skill_item {item_id!r} cannot transition "
            f"{row['status']!r} -> {to_status!r}"
        )
    cur = conn.execute(
        "UPDATE skill_item SET status = :status, updated_at = :now, "
        "lock_version = lock_version + 1 "
        "WHERE id = :item_id AND lock_version = :expected_version",
        {
            "status": to_status,
            "now": now,
            "item_id": item_id,
            "expected_version": expected_version,
        },
    )
    if cur.rowcount != 1:
        raise db.LostUpdateError(
            f"skill_item {item_id!r} update affected {cur.rowcount} rows"
        )
    _append_skill_log(
        conn,
        skill_id=item_id,
        action=action,
        actor=actor,
        from_status=row["status"],
        to_status=to_status,
        detail=detail,
        metadata=metadata,
        now=now,
    )
    return dict(
        conn.execute("SELECT * FROM skill_item WHERE id = ?", (item_id,)).fetchone()
    )


def claim_skill_item(db_path: Path, item_id: str, *, expected_version: int, actor: str) -> dict:
    """Compare-and-set ``candidate -> acquiring``: the exclusive claim a caller
    must win *before* invoking a (possibly side-effecting) executor. Only one
    concurrent caller's compare-and-set can succeed; the other raises
    :class:`InvalidSkillItemTransitionError` (the row is no longer
    ``candidate``) or ``db.LostUpdateError`` (the version moved) — either way,
    before its own executor call, so a skill is never materialised twice."""
    if not (actor or "").strip():
        raise ValueError("actor must be non-empty (the claim needs who acted)")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            return _transition_skill_item(
                conn, item_id, expected_version=expected_version, to_status="acquiring",
                action="acquiring", actor=actor, detail="", metadata=None, now=now,
            )


def finalize_skill_item_acquisition(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    detail: str = "",
    metadata: dict[str, str] | None = None,
) -> dict:
    """Compare-and-set ``acquiring -> acquired`` and log the executor's outcome.
    Called only after the executor invoked following :func:`claim_skill_item`
    has returned successfully."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            return _transition_skill_item(
                conn, item_id, expected_version=expected_version, to_status="acquired",
                action="acquired", actor=actor, detail=detail, metadata=metadata, now=now,
            )


def fail_skill_item_acquisition(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    detail: str = "",
) -> dict:
    """Compare-and-set ``acquiring -> candidate`` (revert) and log the failure.
    Called when the executor invoked following :func:`claim_skill_item` raises —
    the skill returns to ``candidate`` so acquisition may be retried."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            return _transition_skill_item(
                conn, item_id, expected_version=expected_version, to_status="candidate",
                action="acquire_failed", actor=actor, detail=detail, metadata=None, now=now,
            )


def reject_skill_item(
    db_path: Path, item_id: str, *, expected_version: int, actor: str, detail: str = ""
) -> dict:
    """Compare-and-set ``candidate -> rejected`` (terminal)."""
    if not (actor or "").strip():
        raise ValueError("actor must be non-empty (the rejection needs who acted)")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            return _transition_skill_item(
                conn, item_id, expected_version=expected_version, to_status="rejected",
                action="rejected", actor=actor, detail=detail, metadata=None, now=now,
            )


def revoke_skill_item(
    db_path: Path, item_id: str, *, expected_version: int, actor: str, detail: str = ""
) -> dict:
    """Compare-and-set ``acquired -> revoked`` (terminal)."""
    if not (actor or "").strip():
        raise ValueError("actor must be non-empty (the revocation needs who acted)")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            return _transition_skill_item(
                conn, item_id, expected_version=expected_version, to_status="revoked",
                action="revoked", actor=actor, detail=detail, metadata=None, now=now,
            )


# --------------------------------------------------------------------------
# skill_acquisition_log — the lifecycle audit (append-only, per-skill seq)
# --------------------------------------------------------------------------


def _next_skill_log_seq(conn: sqlite3.Connection, skill_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM skill_acquisition_log WHERE skill_id = ?",
        (skill_id,),
    ).fetchone()
    return int(row["max_seq"]) + 1


def _append_skill_log(
    conn: sqlite3.Connection,
    *,
    skill_id: str,
    action: str,
    actor: str | None,
    from_status: str,
    to_status: str,
    detail: str,
    metadata: dict | None,
    now: str,
) -> dict:
    if action not in SKILL_LOG_ACTIONS:
        raise ValueError(f"unknown skill acquisition-log action {action!r}")
    seq = _next_skill_log_seq(conn, skill_id)
    record = {
        "skill_id": skill_id,
        "seq": seq,
        "action": action,
        "actor": actor,
        "from_status": from_status,
        "to_status": to_status,
        "detail": detail or "",
        "metadata_json": json.dumps(metadata, ensure_ascii=False) if metadata else None,
        "created_at": now,
    }
    cur = conn.execute(
        "INSERT INTO skill_acquisition_log "
        "(skill_id, seq, action, actor, from_status, to_status, detail, metadata_json, created_at) "
        "VALUES (:skill_id, :seq, :action, :actor, :from_status, :to_status, :detail, "
        ":metadata_json, :created_at)",
        record,
    )
    record["id"] = cur.lastrowid
    return record


def list_skill_acquisition_log(
    db_path: Path, skill_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    """The append-only lifecycle history for one skill, oldest first (``seq``
    order — the traceable record every transition appends to)."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_acquisition_log WHERE skill_id = ? "
            "ORDER BY seq ASC LIMIT ? OFFSET ?",
            (skill_id, limit, offset),
        ).fetchall()
    decoded: list[dict] = []
    for row in rows:
        record = dict(row)
        raw = record.pop("metadata_json", None)
        record["metadata"] = json.loads(raw) if raw else {}
        decoded.append(record)
    return decoded


# --------------------------------------------------------------------------
# skill_outcome — per-task evidence, and the aggregates the effect
# measurement is computed from
# --------------------------------------------------------------------------


def record_skill_outcome(
    db_path: Path,
    item_id: str,
    *,
    task_id: str,
    used: bool,
    cost_usd: float,
    accepted: bool,
    latency_seconds: float = 0.0,
    detail: str = "",
) -> dict | None:
    """Append one ``skill_outcome`` row for ``item_id`` and return it. Returns
    ``None`` if the skill does not exist — the existence check and the insert
    happen inside one transaction of this single call, so there is no gap
    between "the skill was there" and "the row was written" for a concurrent
    revoke to land in."""
    if not (task_id or "").strip():
        raise ValueError("skill_outcome.task_id must be non-empty")
    now = db.iso_now()
    record = {
        "skill_id": item_id,
        "task_id": task_id,
        "used": 1 if used else 0,
        "cost_usd": float(cost_usd),
        "accepted": 1 if accepted else 0,
        "latency_seconds": float(latency_seconds),
        "detail": detail or "",
        "created_at": now,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM skill_item WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                return None
            cur = conn.execute(
                "INSERT INTO skill_outcome "
                "(skill_id, task_id, used, cost_usd, accepted, latency_seconds, detail, created_at) "
                "VALUES (:skill_id, :task_id, :used, :cost_usd, :accepted, :latency_seconds, "
                ":detail, :created_at)",
                record,
            )
            record["id"] = cur.lastrowid
    return record


def list_skill_outcomes(
    db_path: Path, item_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    """The raw per-task evidence for one skill, newest first."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_outcome WHERE skill_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (item_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def _outcome_stats(conn: sqlite3.Connection, item_id: str, *, used: bool) -> dict:
    """Aggregate ``count``/``avg_cost_usd``/``first_pass_rate`` for one skill's
    outcomes, computed in SQL rather than fetched into Python — a scalar
    aggregate has no row limit to silently truncate, unlike paging through the
    raw rows and averaging them here would."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, AVG(cost_usd) AS avg_cost, "
        "AVG(CAST(accepted AS REAL)) AS fp_rate "
        "FROM skill_outcome WHERE skill_id = ? AND used = ?",
        (item_id, 1 if used else 0),
    ).fetchone()
    count = int(row["n"] or 0)
    return {
        "count": count,
        "avg_cost_usd": float(row["avg_cost"]) if count and row["avg_cost"] is not None else None,
        "first_pass_rate": float(row["fp_rate"]) if count and row["fp_rate"] is not None else None,
    }


def get_skill_effect(db_path: Path, item_id: str) -> dict | None:
    """The effect measurement for one skill: baseline (``used=0``) vs. with-skill
    (``used=1``) aggregates over its recorded outcomes. Returns ``None`` if the
    skill does not exist. A single call encapsulating both the existence check
    and the read, so a caller never has to make two calls (and open a gap
    between them) to get one answer."""
    with db.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM skill_item WHERE id = ?", (item_id,)
        ).fetchone()
        if exists is None:
            return None
        baseline = _outcome_stats(conn, item_id, used=False)
        with_skill = _outcome_stats(conn, item_id, used=True)
    return {"skill_id": item_id, "baseline": baseline, "with_skill": with_skill}
