"""Skill Acquisition table-family (VOYN-W0-AICC-SKILL-ACQUISITION): the
persistence tier behind autonomous capability acquisition (routes → service →
repository → db), modelled directly on the Wave-3 Marketplace family
(``runtime.db.marketplace``) — the same catalogue-with-audited-lifecycle shape
fits a acquired skill exactly, with three additions the marketplace baseline
did not need:

* an explicit **source allowlist** (``skill_source``) a candidate must resolve
  through before it can even be registered — a skill is never accepted from an
  origin nobody approved, and first connection of a new origin is a human
  gate (``propose`` → ``approve``), never automatic;
* **pinning**: every ``skill_item`` row requires both a non-empty ``version``
  and a 64-hex-char ``content_hash`` — a skill with no fixed version+hash is
  refused at the persistence boundary, not merely discouraged;
* **effect measurement** (``skill_outcome``): raw per-task samples tagged
  ``baseline``/``with_skill`` so the service tier can compute cost-per-
  accepted-change and first-pass-acceptance-rate before/after and retire a
  skill that never earned its place in the registry.

Four additive tables, wholly separate from every other family in
:mod:`command_center.runtime.db`:

* ``skill_source``          -- the allowlist. ``proposed → approved →
                                revoked``; only an ``approved`` row is ever
                                handed to a candidate finder or accepted as a
                                skill's origin.
* ``skill_item``            -- one mutable current-state row per skill,
                                guarded by ``lock_version`` compare-and-set and
                                an explicit status allowlist
                                (:data:`SKILL_ITEM_TRANSITIONS`, ``candidate →
                                acquired|rejected``, ``acquired → revoked``).
* ``skill_acquisition_log`` -- append-only. One immutable row per lifecycle
                                action (``acquire``/``reject``/``revoke``)
                                recording *who*, *when*, *what version+hash*,
                                and (for ``acquire``) which isolated
                                ``executor`` materialised it.
* ``skill_outcome``         -- append-only. One row per task a skill was (or
                                was deliberately not) used on, tagged
                                ``baseline``/``with_skill`` — the raw evidence
                                behind the effect measurement.

Every write goes through the shared ``connect()``/``transaction()`` primitives
(WAL, ``BEGIN IMMEDIATE`` write lock, per-row ``version``/``lock_version``
compare-and-set), so the single-writer discipline the rest of ``runtime.db``
follows holds here too: this module is the *only* writer of all four tables.

Kinds/statuses are stored as their stable string *values* (never a Python
enum's member name) so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares — the enum-name lesson carried forward
from the marketplace family.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import command_center.runtime.db as db  # facade (late-bound; see docstring)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: The four "reuse-before-creation" forms the owner idea names: MCP-server
#: registries, Claude Agent Skill catalogues, CLI-tool indices, and a
#: repository's own ADR/runbook docs. A new kind is a schema decision.
SKILL_SOURCE_KINDS: frozenset[str] = frozenset(
    {"mcp_registry", "agent_skill_catalog", "cli_tool_index", "repo_doc"}
)

#: A source's lifecycle. ``proposed`` is where every new origin starts —
#: nothing is searched through it until a human calls ``approve`` — and
#: ``revoked`` is terminal (a compromised or deprecated origin never comes
#: back without being proposed again under a fresh id).
SKILL_SOURCE_STATUSES: frozenset[str] = frozenset({"proposed", "approved", "revoked"})

SKILL_SOURCE_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "revoked"}),
    "approved": frozenset({"revoked"}),
    "revoked": frozenset(),
}

#: What an acquired skill *is*.
SKILL_ITEM_KINDS: frozenset[str] = frozenset(
    {"mcp_server", "agent_skill", "cli_tool", "library"}
)

#: A skill's lifecycle. ``candidate`` is registered but not yet trusted;
#: ``acquired`` is live; ``rejected``/``revoked`` are terminal — a rejected or
#: revoked skill is re-proposed as a new candidate row, never resurrected in
#: place, so the audit trail of *why it left* is never overwritten.
SKILL_ITEM_STATUSES: frozenset[str] = frozenset(
    {"candidate", "acquired", "rejected", "revoked"}
)

SKILL_ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"acquired", "rejected"}),
    "acquired": frozenset({"revoked"}),
    "rejected": frozenset(),
    "revoked": frozenset(),
}

SKILL_ACQUISITION_ACTIONS: frozenset[str] = frozenset({"acquire", "reject", "revoke"})

#: ``baseline`` = the task was done without this skill; ``with_skill`` = the
#: skill was used. Both phases are recorded against the same skill so the
#: service tier can compare them.
SKILL_OUTCOME_PHASES: frozenset[str] = frozenset({"baseline", "with_skill"})

#: A pin is a sha256 hex digest — nothing looser is accepted as a hash.
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidSkillSourceTransitionError(Exception):
    """Raised when a source status change is not an allowed edge in
    :data:`SKILL_SOURCE_TRANSITIONS`."""


class InvalidSkillItemTransitionError(Exception):
    """Raised when a skill status change is not an allowed edge in
    :data:`SKILL_ITEM_TRANSITIONS`."""


_SKILL_SOURCE_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "origin",
    "status",
    "proposed_by",
    "approved_by",
    "lock_version",
    "created_at",
    "updated_at",
)

_SKILL_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "version",
    "content_hash",
    "source_id",
    "provenance",
    "task_class",
    "status",
    "selection_rationale_json",
    "lock_version",
    "created_at",
    "updated_at",
)

_SKILL_ACQUISITION_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "skill_id",
    "seq",
    "actor",
    "action",
    "version",
    "content_hash",
    "executor",
    "detail",
    "metadata_json",
    "created_at",
)

_SKILL_OUTCOME_COLUMNS: tuple[str, ...] = (
    "id",
    "skill_id",
    "task_id",
    "phase",
    "cost",
    "accepted",
    "first_pass",
    "created_at",
)


# --------------------------------------------------------------------------
# skill_source — propose / approve / revoke / get / list (the allowlist)
# --------------------------------------------------------------------------


def create_skill_source(
    db_path: Path,
    *,
    name: str,
    kind: str,
    origin: str,
    proposed_by: str,
    source_id: str | None = None,
) -> dict:
    """Propose a new source origin. Always starts ``proposed`` — nothing is
    ever auto-approved; a human must call :func:`set_skill_source_status`
    with ``new_status='approved'`` before this origin is eligible for
    candidate discovery or skill registration (see
    :func:`create_skill_candidate`)."""
    if not (name or "").strip():
        raise ValueError("skill_source.name must be non-empty")
    if kind not in SKILL_SOURCE_KINDS:
        raise ValueError(
            f"skill_source.kind must be one of {sorted(SKILL_SOURCE_KINDS)}, got {kind!r}"
        )
    if not (origin or "").strip():
        raise ValueError("skill_source.origin must be non-empty")
    if not (proposed_by or "").strip():
        raise ValueError("skill_source.proposed_by must be non-empty")
    now = db.iso_now()
    record = {name_: None for name_ in _SKILL_SOURCE_COLUMNS}
    record.update(
        {
            "id": source_id or db.new_id(),
            "name": name,
            "kind": kind,
            "origin": origin,
            "status": "proposed",
            "proposed_by": proposed_by,
            "approved_by": "",
            "lock_version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_SKILL_SOURCE_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _SKILL_SOURCE_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            try:
                conn.execute(
                    f"INSERT INTO skill_source ({columns}) VALUES ({placeholders})",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"skill_source.origin already proposed: {origin!r}") from exc
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


def set_skill_source_status(
    db_path: Path,
    source_id: str,
    *,
    expected_version: int,
    new_status: str,
    actor: str,
) -> dict:
    """Move a source along :data:`SKILL_SOURCE_TRANSITIONS`. ``actor`` is
    recorded as ``approved_by`` when the new status is ``approved`` — the
    human-gate record for that source's first (and only) approval; a
    ``revoked`` transition leaves ``approved_by`` untouched, so the record of
    who once approved it survives the revocation."""
    if not (actor or "").strip():
        raise ValueError("skill_source status change actor must be non-empty")
    if new_status not in SKILL_SOURCE_STATUSES:
        raise ValueError(
            f"skill_source.status must be one of {sorted(SKILL_SOURCE_STATUSES)}, got {new_status!r}"
        )
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
            if new_status not in SKILL_SOURCE_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidSkillSourceTransitionError(
                    f"skill_source {source_id!r} cannot transition "
                    f"{row['status']!r} -> {new_status!r}"
                )
            approved_by = row["approved_by"] or ""
            if new_status == "approved":
                approved_by = actor
            cur = conn.execute(
                "UPDATE skill_source SET status = :status, approved_by = :approved_by, "
                "updated_at = :now, lock_version = lock_version + 1 "
                "WHERE id = :id AND lock_version = :expected_version",
                {
                    "status": new_status,
                    "approved_by": approved_by,
                    "now": now,
                    "id": source_id,
                    "expected_version": expected_version,
                },
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"skill_source {source_id!r} status change affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM skill_source WHERE id = ?", (source_id,)
            ).fetchone()
            return dict(updated)


# --------------------------------------------------------------------------
# skill_item — create (pinned + source-gated) / get / list
# --------------------------------------------------------------------------


def create_skill_candidate(
    db_path: Path,
    *,
    name: str,
    kind: str,
    version: str,
    content_hash: str,
    source_id: str,
    provenance: str = "",
    task_class: str = "",
    selection_rationale: dict | None = None,
    item_id: str | None = None,
) -> dict:
    """Register a candidate skill. Always starts ``candidate``.

    Refused at the persistence boundary (never merely discouraged) unless:

    * ``name`` is non-empty and ``kind`` is one of :data:`SKILL_ITEM_KINDS`;
    * ``version`` is non-empty (a skill with no fixed version is not pinned);
    * ``content_hash`` is a 64-hex-char sha256 digest (the artefact pin);
    * ``source_id`` resolves to a :data:`SKILL_SOURCE_STATUSES` ``approved``
      row -- an unknown or not-yet-approved source can never seed a
      candidate, which is the allowlist enforced structurally rather than by
      caller discipline.
    """
    if not (name or "").strip():
        raise ValueError("skill_item.name must be non-empty")
    if kind not in SKILL_ITEM_KINDS:
        raise ValueError(
            f"skill_item.kind must be one of {sorted(SKILL_ITEM_KINDS)}, got {kind!r}"
        )
    if not (version or "").strip():
        raise ValueError("skill_item.version must be non-empty (pinning invariant)")
    if not _CONTENT_HASH_RE.fullmatch((content_hash or "").lower()):
        raise ValueError(
            "skill_item.content_hash must be a 64-hex-char sha256 digest, "
            f"got {content_hash!r}"
        )
    source = get_skill_source(db_path, source_id)
    if source is None:
        raise ValueError(f"skill_item.source_id does not resolve to a known source: {source_id!r}")
    if source["status"] != "approved":
        raise ValueError(
            f"skill_item.source_id {source_id!r} is not an approved source "
            f"(status={source['status']!r}) -- allowlist gate"
        )
    now = db.iso_now()
    record = {name_: None for name_ in _SKILL_ITEM_COLUMNS}
    record.update(
        {
            "id": item_id or db.new_id(),
            "name": name,
            "kind": kind,
            "version": version,
            "content_hash": content_hash.lower(),
            "source_id": source_id,
            "provenance": provenance or "",
            "task_class": task_class or "",
            "status": "candidate",
            "selection_rationale_json": json.dumps(
                selection_rationale or {}, sort_keys=True
            ),
            "lock_version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_SKILL_ITEM_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _SKILL_ITEM_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO skill_item ({columns}) VALUES ({placeholders})",
                record,
            )
    return _decode_skill_item(record)


def _decode_skill_item(record: dict) -> dict:
    decoded = dict(record)
    try:
        decoded["selection_rationale"] = json.loads(
            decoded.get("selection_rationale_json") or "{}"
        )
    except (ValueError, TypeError):
        decoded["selection_rationale"] = {}
    return decoded


def get_skill_item(db_path: Path, item_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM skill_item WHERE id = ?", (item_id,)
        ).fetchone()
        decoded = db._row_to_dict(row)
        return _decode_skill_item(decoded) if decoded is not None else None


def list_skill_items(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    task_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
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
        return [_decode_skill_item(dict(row)) for row in rows]


# --------------------------------------------------------------------------
# Atomic lifecycle transition: status change + acquisition-log append
# --------------------------------------------------------------------------


def _transition_skill_item(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    new_status: str,
    action: str,
    actor: str,
    executor: str = "",
    detail: str = "",
    metadata: dict[str, str] | None = None,
    log_id: str | None = None,
) -> tuple[dict, dict]:
    """Transition ``item_id`` to ``new_status`` **and** append its
    acquisition-log row inside one ``BEGIN IMMEDIATE`` transaction, returning
    ``(item_row, log_row)`` -- atomic for the same reason
    ``marketplace.install_market_item`` is: a crash can never leave a status
    flip with no trail line, or a trail line for a status that never flipped.
    """
    if action not in SKILL_ACQUISITION_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(SKILL_ACQUISITION_ACTIONS)}, got {action!r}"
        )
    if not (actor or "").strip():
        raise ValueError("skill acquisition-log actor must be non-empty (the log records who)")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
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
            if new_status not in SKILL_ITEM_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidSkillItemTransitionError(
                    f"skill_item {item_id!r} cannot transition "
                    f"{row['status']!r} -> {new_status!r}"
                )
            cur = conn.execute(
                "UPDATE skill_item SET status = :status, updated_at = :now, "
                "lock_version = lock_version + 1 "
                "WHERE id = :id AND lock_version = :expected_version",
                {
                    "status": new_status,
                    "now": now,
                    "id": item_id,
                    "expected_version": expected_version,
                },
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"skill_item {item_id!r} transition affected {cur.rowcount} rows"
                )
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM skill_acquisition_log WHERE skill_id = ?",
                (item_id,),
            ).fetchone()[0]
            log_record = {
                "id": log_id or db.new_id(),
                "skill_id": item_id,
                "seq": next_seq,
                "actor": actor,
                "action": action,
                "version": row["version"] or "",
                "content_hash": row["content_hash"] or "",
                "executor": executor or "",
                "detail": detail or "",
                "metadata_json": json.dumps(metadata or {}, sort_keys=True),
                "created_at": now,
            }
            columns = ", ".join(_SKILL_ACQUISITION_LOG_COLUMNS)
            placeholders = ", ".join(f":{name_}" for name_ in _SKILL_ACQUISITION_LOG_COLUMNS)
            conn.execute(
                f"INSERT INTO skill_acquisition_log ({columns}) VALUES ({placeholders})",
                log_record,
            )
            item_row = conn.execute(
                "SELECT * FROM skill_item WHERE id = ?", (item_id,)
            ).fetchone()
            return _decode_skill_item(dict(item_row)), dict(log_record)


def acquire_skill_item(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    executor: str,
    detail: str = "",
    metadata: dict[str, str] | None = None,
    log_id: str | None = None,
) -> tuple[dict, dict]:
    """``candidate → acquired``. ``executor`` names the isolated seam that
    materialised it (see ``command_center.skills.executor``); a blank
    executor is refused -- the log must always attribute *which* isolated
    implementation acted, even when that implementation is the safe no-op
    default."""
    if not (executor or "").strip():
        raise ValueError("skill acquisition executor must be non-empty")
    return _transition_skill_item(
        db_path, item_id, expected_version=expected_version, new_status="acquired",
        action="acquire", actor=actor, executor=executor, detail=detail, metadata=metadata,
        log_id=log_id,
    )


def reject_skill_item(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    detail: str = "",
    metadata: dict[str, str] | None = None,
    log_id: str | None = None,
) -> tuple[dict, dict]:
    """``candidate → rejected`` (terminal). A candidate that lost the
    measurable selection, or that a human declined, never silently
    disappears -- it leaves the same audited trail an acquisition would."""
    return _transition_skill_item(
        db_path, item_id, expected_version=expected_version, new_status="rejected",
        action="reject", actor=actor, detail=detail, metadata=metadata, log_id=log_id,
    )


def revoke_skill_item(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    detail: str = "",
    metadata: dict[str, str] | None = None,
    log_id: str | None = None,
) -> tuple[dict, dict]:
    """``acquired → revoked`` (terminal). The only way a live skill leaves
    the registry -- by a human call or by the effect sweep
    (``skills.service.sweep_retire_underperforming``) finding no measurable
    improvement, never by deletion."""
    return _transition_skill_item(
        db_path, item_id, expected_version=expected_version, new_status="revoked",
        action="revoke", actor=actor, detail=detail, metadata=metadata, log_id=log_id,
    )


def list_skill_acquisition_log(
    db_path: Path, item_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    """The append-only acquisition trail for one skill (newest first)."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_acquisition_log WHERE skill_id = ? "
            "ORDER BY seq DESC LIMIT ? OFFSET ?",
            (item_id, limit, offset),
        ).fetchall()
    decoded: list[dict] = []
    for row in rows:
        record = dict(row)
        try:
            record["metadata"] = json.loads(record.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            record["metadata"] = {}
        decoded.append(record)
    return decoded


# --------------------------------------------------------------------------
# skill_outcome — the raw evidence behind effect measurement
# --------------------------------------------------------------------------


def record_skill_outcome(
    db_path: Path,
    *,
    skill_id: str,
    task_id: str,
    phase: str,
    cost: float,
    accepted: bool,
    first_pass: bool,
    outcome_id: str | None = None,
) -> dict:
    if get_skill_item(db_path, skill_id) is None:
        raise KeyError(f"No such skill_item: {skill_id!r}")
    if phase not in SKILL_OUTCOME_PHASES:
        raise ValueError(
            f"skill_outcome.phase must be one of {sorted(SKILL_OUTCOME_PHASES)}, got {phase!r}"
        )
    if not (task_id or "").strip():
        raise ValueError("skill_outcome.task_id must be non-empty")
    if cost < 0:
        raise ValueError(f"skill_outcome.cost must be non-negative, got {cost!r}")
    now = db.iso_now()
    record = {
        "id": outcome_id or db.new_id(),
        "skill_id": skill_id,
        "task_id": task_id,
        "phase": phase,
        "cost": float(cost),
        "accepted": 1 if accepted else 0,
        "first_pass": 1 if first_pass else 0,
        "created_at": now,
    }
    columns = ", ".join(_SKILL_OUTCOME_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _SKILL_OUTCOME_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO skill_outcome ({columns}) VALUES ({placeholders})",
                record,
            )
    return record


def list_skill_outcomes(
    db_path: Path,
    skill_id: str,
    *,
    phase: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict]:
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses = ["skill_id = ?"]
    params: list[Any] = [skill_id]
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    where = " AND ".join(clauses)
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM skill_outcome WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        results = []
        for row in rows:
            record = dict(row)
            record["accepted"] = bool(record["accepted"])
            record["first_pass"] = bool(record["first_pass"])
            results.append(record)
        return results
