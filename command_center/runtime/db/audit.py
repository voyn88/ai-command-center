"""Wave 2 "new engine" table-families (VOYN-W2-AUD): the persistence layer
behind the Audit engine — ``audit_run`` (one row per audit pass over a project)
and ``audit_finding`` (one row per finding).

This module is the **repository tier** of the Wave-2 stack (routes → service →
repository → db). It owns two additive tables that are wholly separate from
every other family in ``runtime.db``; the names never collide. Every write goes
through the shared ``connect()``/``transaction()`` primitives (WAL,
``BEGIN IMMEDIATE`` write lock, per-row ``version`` compare-and-set), so the
single-writer discipline the rest of ``runtime.db`` follows holds here too.

Two invariants are enforced *at this boundary* so they can never be bypassed by
a higher layer:

* **Every finding always carries a status and an owner.** :func:`create_audit_finding`
  refuses an empty ``owner`` and validates ``status`` against the allowlist — a
  finding with no owner can never reach a stored row (the acceptance invariant).
* **Redaction is expressed in SQL.** The list functions take ``exclude_projects``
  and drop those rows *in the query* (see :func:`_exclude_projects_clause`), so a
  ``LIMIT``/``OFFSET`` page counts only visible rows rather than being trimmed
  after the fact — the same policy the Wave-1 families use.

Statuses/severities/categories are stored as their stable string *values* (never
a Python enum's member name), so a column round-trips to exactly the Literal the
API contract (``api/models.py``) declares.

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

from command_center.runtime.db.wave1 import _exclude_projects_clause

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: The check families a finding can belong to (mirrors ``api.models.AuditCategory``).
AUDIT_CATEGORIES: frozenset[str] = frozenset(
    {"security", "coverage", "code-quality", "deps", "lint"}
)

#: Finding severities (mirrors ``api.models.AuditSeverity``).
AUDIT_SEVERITIES: frozenset[str] = frozenset(
    {"info", "low", "medium", "high", "critical"}
)

#: A finding's triage lifecycle (mirrors ``api.models.AuditFindingStatus``).
AUDIT_FINDING_STATUSES: frozenset[str] = frozenset({"open", "ack", "fixed"})

#: Allowed finding status edges. ``ack`` and ``fixed`` can each return to
#: ``open`` (a re-opened finding), but there is no ``ack``→``ack`` self-loop and
#: no jump that skips the lifecycle silently.
AUDIT_FINDING_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"ack", "fixed"}),
    "ack": frozenset({"fixed", "open"}),
    "fixed": frozenset({"open"}),
}

#: An audit run's execution lifecycle (mirrors ``api.models.AuditRunStatus``).
AUDIT_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "completed", "failed"}
)

#: Allowed run status edges. A run moves forward through its lifecycle and never
#: back out of a terminal state (``completed``/``failed``).
AUDIT_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "completed", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class InvalidAuditFindingTransitionError(Exception):
    """Raised when a finding status change is not an allowed edge in
    :data:`AUDIT_FINDING_TRANSITIONS`."""


class InvalidAuditRunTransitionError(Exception):
    """Raised when an audit-run status change is not an allowed edge in
    :data:`AUDIT_RUN_TRANSITIONS` (a backward jump, or any move out of a
    terminal state)."""


# --------------------------------------------------------------------------
# audit_run
# --------------------------------------------------------------------------

_AUDIT_RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "project_ref",
    "status",
    "checks_json",
    "finding_count",
    "version",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
)


def create_audit_run(
    db_path: Path,
    *,
    project_ref: str,
    checks: Iterable[str] | None = None,
    status: str = "running",
    run_id: str | None = None,
) -> dict:
    """Insert one ``audit_run`` row in its initial state and return it.

    ``project_ref`` must be non-empty — a run always targets a project so it can
    be routed and redacted. ``checks`` records which check families this pass
    ran; it is stored as a JSON array."""
    if not project_ref or not str(project_ref).strip():
        raise ValueError("audit_run.project_ref must be non-empty")
    if status not in AUDIT_RUN_STATUSES:
        raise ValueError(
            f"audit_run.status must be one of {sorted(AUDIT_RUN_STATUSES)}, got {status!r}"
        )
    check_names = [str(name) for name in (checks or [])]
    now = db.iso_now()
    record = {
        "id": run_id or db.new_id(),
        "project_ref": project_ref,
        "status": status,
        "checks_json": json.dumps(check_names, ensure_ascii=False),
        "finding_count": 0,
        "version": 0,
        "started_at": now,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_AUDIT_RUN_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _AUDIT_RUN_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO audit_run ({columns}) VALUES ({placeholders})",
                record,
            )
    # `record`, not the decoded row: `_decode_run_row` pops `checks_json`.
    _mirror_audit_run(record)
    return _decode_run_row(record)

def _mirror_audit_run(record: dict) -> None:
    """Best-effort dual-write of one audit run into PostgreSQL (SRV-01B slice 8).

    Takes the **stored** record, with `checks_json` as text — not the decoded
    row the public readers return, which replaces it with a `checks` list.
    Mirroring the decoded shape would write the column's default on every row.
    """
    try:
        from command_center.db.audit_store import PostgresAuditRunMirror

        PostgresAuditRunMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror audit_run into PostgreSQL", exc_info=True)


def _mirror_audit_finding(record: dict) -> None:
    """Best-effort dual-write of one audit finding into PostgreSQL (slice 8)."""
    try:
        from command_center.db.audit_store import PostgresAuditFindingMirror

        PostgresAuditFindingMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror audit_finding into PostgreSQL", exc_info=True)




def get_audit_run(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM audit_run WHERE id = ?", (run_id,)
        ).fetchone()
        return _decode_run_row(dict(row)) if row is not None else None


def list_audit_runs(
    db_path: Path,
    *,
    project: str | None = None,
    statuses: Iterable[str] | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List audit runs, newest first, optionally filtered by ``project`` and/or a
    set of ``statuses``. ``limit``/``offset`` page the result.

    ``exclude_projects`` drops rows for those projects *in the query* (the
    redaction policy), so a page counts only visible rows."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project_ref = ?")
        params.append(project)
    statuses = list(statuses) if statuses is not None else None
    if statuses is not None:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    exclude_clause, exclude_params = _exclude_projects_clause(
        exclude_projects, allow_null=False
    )
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_run{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_decode_run_row(dict(row)) for row in rows]


def set_audit_run_status(
    db_path: Path,
    run_id: str,
    *,
    expected_version: int,
    status: str,
    finding_count: int | None = None,
) -> dict:
    """Compare-and-set an audit run's ``status`` (guarded by the transition
    allowlist), optionally stamping ``finding_count``. Moving to a terminal
    state (``completed``/``failed``) records ``completed_at``."""
    if status not in AUDIT_RUN_STATUSES:
        raise ValueError(f"unknown audit_run status {status!r}")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM audit_run WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such audit_run: {run_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"audit_run {run_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if status not in AUDIT_RUN_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidAuditRunTransitionError(
                    f"audit_run {run_id!r} cannot transition "
                    f"{row['status']!r} -> {status!r}"
                )
            fields: dict[str, Any] = {"status": status, "updated_at": now}
            if finding_count is not None:
                if finding_count < 0:
                    raise ValueError("finding_count must be non-negative")
                fields["finding_count"] = finding_count
            if status in ("completed", "failed"):
                fields["completed_at"] = now
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["run_id"] = run_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE audit_run SET {set_clause}, version = version + 1 "
                "WHERE id = :run_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"audit_run {run_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM audit_run WHERE id = ?", (run_id,)
            ).fetchone()
            record = dict(updated)
    _mirror_audit_run(record)
    return _decode_run_row(record)


# --------------------------------------------------------------------------
# audit_finding
# --------------------------------------------------------------------------

_AUDIT_FINDING_COLUMNS: tuple[str, ...] = (
    "id",
    "run_id",
    "category",
    "severity",
    "summary",
    "file_path",
    "loc",
    "status",
    "owner",
    "project_ref",
    "promoted_task_id",
    "version",
    "created_at",
    "updated_at",
)


def create_audit_finding(
    db_path: Path,
    *,
    run_id: str,
    category: str,
    summary: str,
    owner: str,
    severity: str = "info",
    file_path: str | None = None,
    loc: str | None = None,
    status: str = "open",
    project_ref: str | None = None,
    finding_id: str | None = None,
) -> dict:
    """Insert one ``audit_finding`` row and return it.

    Enforces the acceptance invariant at the persistence boundary: ``owner`` must
    be non-empty and ``status`` must be a valid lifecycle value, so a stored
    finding *always* carries both triage axes. ``category`` and ``severity`` are
    validated against their allowlists too — a malformed value can never reach a
    stored row. ``run_id`` must reference an existing run (enforced by the FK)."""
    if not run_id or not str(run_id).strip():
        raise ValueError("audit_finding.run_id must be non-empty")
    if category not in AUDIT_CATEGORIES:
        raise ValueError(
            f"audit_finding.category must be one of {sorted(AUDIT_CATEGORIES)}, got {category!r}"
        )
    if severity not in AUDIT_SEVERITIES:
        raise ValueError(
            f"audit_finding.severity must be one of {sorted(AUDIT_SEVERITIES)}, got {severity!r}"
        )
    if status not in AUDIT_FINDING_STATUSES:
        raise ValueError(
            f"audit_finding.status must be one of {sorted(AUDIT_FINDING_STATUSES)}, got {status!r}"
        )
    if not owner or not str(owner).strip():
        # The acceptance invariant, fail-closed: a finding with no owner is a
        # defect, refused before it lands rather than stored owner-less.
        raise ValueError("audit_finding.owner must be non-empty (every finding carries an owner)")
    now = db.iso_now()
    record = {
        "id": finding_id or db.new_id(),
        "run_id": run_id,
        "category": category,
        "severity": severity,
        "summary": summary,
        "file_path": file_path,
        "loc": loc,
        "status": status,
        "owner": owner,
        "project_ref": project_ref,
        "promoted_task_id": None,
        "version": 0,
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(_AUDIT_FINDING_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _AUDIT_FINDING_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO audit_finding ({columns}) VALUES ({placeholders})",
                record,
            )
    _mirror_audit_finding(record)
    return dict(record)


def get_audit_finding(db_path: Path, finding_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM audit_finding WHERE id = ?", (finding_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_audit_findings(
    db_path: Path,
    *,
    run_id: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List audit findings, newest first, optionally filtered by ``run_id``,
    ``status`` and/or ``owner``. ``limit``/``offset`` page the result.

    ``exclude_projects`` drops rows attributed to those projects *in the query*
    (redaction); an un-attributed finding (``project_ref IS NULL``) is kept."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if owner is not None:
        clauses.append("owner = ?")
        params.append(owner)
    exclude_clause, exclude_params = _exclude_projects_clause(
        exclude_projects, allow_null=True
    )
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_finding{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def _audit_finding_transition(
    conn: sqlite3.Connection,
    finding_id: str,
    *,
    expected_version: int,
    new_status: str,
    extra_fields: dict | None,
    now: str,
) -> dict:
    """CAS + transition-guarded status update inside an open transaction.

    Checks the caller's ``version`` first (a stale writer always loses as a stale
    writer), then refuses an illegal status edge, bumps ``version`` and stamps
    ``updated_at``. Returns the updated row dict."""
    row = conn.execute(
        "SELECT status, version FROM audit_finding WHERE id = ?",
        (finding_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"No such audit_finding: {finding_id!r}")
    if row["version"] != expected_version:
        raise db.LostUpdateError(
            f"audit_finding {finding_id!r} version mismatch: "
            f"expected {expected_version}, actual {row['version']}"
        )
    if new_status not in AUDIT_FINDING_STATUSES:
        raise ValueError(f"unknown audit_finding status {new_status!r}")
    if new_status not in AUDIT_FINDING_TRANSITIONS.get(row["status"], frozenset()):
        raise InvalidAuditFindingTransitionError(
            f"audit_finding {finding_id!r} cannot transition "
            f"{row['status']!r} -> {new_status!r}"
        )
    fields = dict(extra_fields or {})
    fields["status"] = new_status
    fields["updated_at"] = now
    set_clause = ", ".join(f"{key} = :{key}" for key in fields)
    params = dict(fields)
    params["finding_id"] = finding_id
    params["expected_version"] = expected_version
    cur = conn.execute(
        f"UPDATE audit_finding SET {set_clause}, version = version + 1 "
        "WHERE id = :finding_id AND version = :expected_version",
        params,
    )
    if cur.rowcount != 1:
        raise db.LostUpdateError(
            f"audit_finding {finding_id!r} update affected {cur.rowcount} rows"
        )
    updated = conn.execute(
        "SELECT * FROM audit_finding WHERE id = ?", (finding_id,)
    ).fetchone()
    return dict(updated)


def set_audit_finding_status(
    db_path: Path, finding_id: str, *, expected_version: int, status: str
) -> dict:
    """Compare-and-set status update guarded by the transition allowlist."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            record = _audit_finding_transition(
                conn,
                finding_id,
                expected_version=expected_version,
                new_status=status,
                extra_fields=None,
                now=now,
            )
    _mirror_audit_finding(record)
    return record


def promote_audit_finding(
    db_path: Path, finding_id: str, *, expected_version: int, task_id: str
) -> dict:
    """Record that ``finding_id`` was promoted into ``task_id``: stamp
    ``promoted_task_id`` and move the finding to ``ack`` (acknowledged — it is
    now tracked as a task), atomically and CAS-guarded.

    The caller creates the task through the existing tasks path *first*; this
    only records the link — the persistence layer never creates a task itself.
    A finding already ``fixed`` cannot be promoted (the transition guard refuses
    ``fixed`` → ``ack``), so a promote of resolved work is rejected rather than
    silently re-opening it."""
    if not task_id:
        raise ValueError("promote_audit_finding requires a task_id")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            record = _audit_finding_transition(
                conn,
                finding_id,
                expected_version=expected_version,
                new_status="ack",
                extra_fields={"promoted_task_id": task_id},
                now=now,
            )
    _mirror_audit_finding(record)
    return record


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def list_audit_runs_stored(db_path: Path) -> list[dict]:
    """Every audit run in the shape SQLite **stores**, for reconciliation.

    Every other reader here returns :func:`_decode_run_row` output, which pops
    ``checks_json`` and substitutes a decoded ``checks`` list. Reconciliation
    compares what the authority stores against what the mirror stores, so fed a
    decoded row it reports **every** run divergent on the one column that
    needed converting.

    Slice 4 shipped without this for ``digest_item`` and the omission was found
    only because independent review asked what an operator would actually call.
    Written here in the same slice as the mirror.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM audit_run ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def _decode_run_row(row: dict) -> dict:
    """Return a copy of an ``audit_run`` row with ``checks_json`` decoded to a
    ``checks`` list (the JSON column stays internal to the repository)."""
    out = dict(row)
    raw = out.pop("checks_json", "[]")
    out["checks"] = json.loads(raw) if raw else []
    return out
