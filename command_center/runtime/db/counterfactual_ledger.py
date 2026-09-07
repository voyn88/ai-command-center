"""Counterfactual Ledger table-family (VOYN-MIN-COMP): the persistence tier
behind the "attention and decisions" surface's alternatives record (routes →
service → repository → db).

This module owns two additive tables, wholly separate from every other family
in :mod:`command_center.runtime.db`:

* A **decision** is a mutable current-state row moving through an explicit
  status allowlist (:data:`DECISION_TRANSITIONS`, ``draft → finalized``).
  ``criticality`` marks whether the acceptance rule — a ``critical`` decision
  needs at least :data:`MIN_ALTERNATIVES_FOR_CRITICAL` recorded alternatives
  before it may finalize — applies to it. That rule is *policy*, enforced one
  tier up in the service, never here: this layer only guarantees a legal status
  edge and an atomic version bump, the same split the conflicts and council
  engines use.
* An **alternative** is an append-only row: one per path considered and not
  taken, carrying the reason it was rejected. There is no update path — the
  ledger is a record of what was weighed, not a mutable scratchpad, so once
  written a rejection reason cannot be quietly edited after the decision.

``project_ref`` (nullable, on the decision only) is the redaction key:
:func:`list_counterfactual_decisions` can drop BANK/LEGAL rows *in the SQL
query* so a sensitive decision's title/rationale never leaves the read surface
(the Wave-1 exclude-in-SQL pattern). Alternatives are only ever reached through
their parent decision, which is already redacted, so they carry no redaction
key of their own.

Statuses/criticalities are stored as their stable string *values* (never a
Python enum member name) so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares — the enum-name lesson.

No PostgreSQL mirror exists for either table yet (signed out of scope in
``tests/db/test_mirror_coverage.py`` — the tables still exist on the
PostgreSQL side for schema correspondence, they are simply not dual-written).

Every public name here is prefixed ``counterfactual_`` (or carries
``decision``/``alternative`` qualified as such) rather than the bare
``create_decision``/``get_decision``/``list_decisions`` the repository-per-table
convention would otherwise suggest: the Council engine
(:mod:`command_center.runtime.db.council`) already exports ``get_decision`` and
``list_decisions`` for its own, unrelated ``council_decision`` table onto the
same package facade, and a second definition of either name would silently
shadow the first one imported.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)

#: The accepted criticalities. ``critical`` is the only one the acceptance rule
#: gates on.
DECISION_CRITICALITIES: frozenset[str] = frozenset({"normal", "critical"})

#: The decision lifecycle. ``finalized`` is terminal.
DECISION_STATUSES: frozenset[str] = frozenset({"draft", "finalized"})

#: The allowed status edges. A ``draft`` decision may finalize; ``finalized`` is
#: terminal. The *service* additionally refuses to finalize a ``critical``
#: decision that carries fewer than :data:`MIN_ALTERNATIVES_FOR_CRITICAL`
#: alternatives — that policy is not encoded here.
DECISION_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"finalized"}),
    "finalized": frozenset(),
}

#: The acceptance rule: a ``critical`` decision must carry at least this many
#: recorded alternatives before it may finalize.
MIN_ALTERNATIVES_FOR_CRITICAL = 3

#: Fields a caller may set through :func:`update_counterfactual_decision_fields`.
#: ``status`` is never routed through here — it goes through the
#: transition-guarded :func:`finalize_counterfactual_decision`.
_UPDATABLE_DECISION_FIELDS: frozenset[str] = frozenset({"title", "description", "owner"})


class InvalidCounterfactualDecisionTransitionError(Exception):
    """Raised when a decision status change is not an allowed edge in
    :data:`DECISION_TRANSITIONS` (a backward jump, or any move out of the
    terminal ``finalized`` state)."""


class CounterfactualDecisionFinalizedError(Exception):
    """Raised when a field update, or a new alternative, is attempted against a
    decision that is already ``finalized`` — a finalized decision, and the
    alternatives ledger behind it, are frozen."""


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
# decision
# --------------------------------------------------------------------------

_DECISION_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "description",
    "criticality",
    "status",
    "chosen_option",
    "rationale",
    "owner",
    "project_ref",
    "decided_at",
    "version",
    "created_at",
    "updated_at",
)


def create_counterfactual_decision(
    db_path: Path,
    *,
    title: str,
    description: str = "",
    criticality: str = "normal",
    owner: str | None = None,
    project_ref: str | None = None,
    decision_id: str | None = None,
) -> dict:
    """Insert one ``counterfactual_decision`` row and return it.

    ``title`` is required (a decision is always named); ``criticality`` must be
    one of :data:`DECISION_CRITICALITIES`. The decision opens ``draft`` with no
    ``chosen_option``/``rationale`` — those are filled by
    :func:`finalize_counterfactual_decision`."""
    if not (title or "").strip():
        raise ValueError("decision.title must not be empty")
    if criticality not in DECISION_CRITICALITIES:
        raise ValueError(
            f"decision.criticality must be one of {sorted(DECISION_CRITICALITIES)}, "
            f"got {criticality!r}"
        )
    now = db.iso_now()
    record = {name: None for name in _DECISION_COLUMNS}
    record.update(
        {
            "id": decision_id or db.new_id(),
            "title": title,
            "description": description or "",
            "criticality": criticality,
            "status": "draft",
            "chosen_option": "",
            "rationale": "",
            "owner": owner,
            "project_ref": project_ref,
            "decided_at": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_DECISION_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _DECISION_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO counterfactual_decision ({columns}) VALUES ({placeholders})",
                record,
            )
    return record


def get_counterfactual_decision(db_path: Path, decision_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM counterfactual_decision WHERE id = ?", (decision_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_counterfactual_decisions(
    db_path: Path,
    *,
    criticality: str | None = None,
    status: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List decisions, newest first, optionally filtered by ``criticality``
    and/or ``status``. ``limit``/``offset`` page the result (stable order:
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
    if criticality is not None:
        clauses.append("criticality = ?")
        params.append(criticality)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    exclude_clause, exclude_params = _exclude_projects_clause(exclude_projects)
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM counterfactual_decision{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def update_counterfactual_decision_fields(
    db_path: Path,
    decision_id: str,
    *,
    expected_version: int,
    fields: dict,
) -> dict:
    """Compare-and-set update of a decision's mutable fields (title/description/
    owner).

    Refuses any key outside :data:`_UPDATABLE_DECISION_FIELDS`, refuses a stale
    ``version`` (:class:`db.LostUpdateError`), and refuses to touch a decision
    that is already ``finalized`` (:class:`CounterfactualDecisionFinalizedError`).
    Bumps ``version`` and ``updated_at``."""
    unknown = set(fields) - _UPDATABLE_DECISION_FIELDS
    if unknown:
        raise ValueError(f"decision update has non-updatable fields: {sorted(unknown)}")
    if not fields:
        raise ValueError("update_counterfactual_decision_fields requires at least one field")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM counterfactual_decision WHERE id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No such decision: {decision_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"decision {decision_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if row["status"] == "finalized":
                raise CounterfactualDecisionFinalizedError(
                    f"decision {decision_id!r} is finalized and cannot be modified"
                )
            payload = dict(fields)
            payload["updated_at"] = now
            set_clause = ", ".join(f"{key} = :{key}" for key in payload)
            params = dict(payload)
            params["decision_id"] = decision_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"UPDATE counterfactual_decision SET {set_clause}, version = version + 1 "
                "WHERE id = :decision_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"decision {decision_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM counterfactual_decision WHERE id = ?", (decision_id,)
            ).fetchone()
            return dict(updated)


def finalize_counterfactual_decision(
    db_path: Path,
    decision_id: str,
    *,
    expected_version: int,
    chosen_option: str,
    rationale: str = "",
) -> dict:
    """Compare-and-set transition ``draft -> finalized``, stamping
    ``chosen_option``/``rationale``/``decided_at``.

    The acceptance rule (a ``critical`` decision needs at least
    :data:`MIN_ALTERNATIVES_FOR_CRITICAL` alternatives first) is the service's
    job — this layer only guarantees a legal edge and an atomic version bump,
    the structural backstop behind the policy."""
    if not (chosen_option or "").strip():
        raise ValueError("decision.chosen_option must not be empty to finalize")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM counterfactual_decision WHERE id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No such decision: {decision_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"decision {decision_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if "finalized" not in DECISION_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidCounterfactualDecisionTransitionError(
                    f"decision {decision_id!r} cannot transition "
                    f"{row['status']!r} -> 'finalized'"
                )
            params = {
                "decision_id": decision_id,
                "expected_version": expected_version,
                "status": "finalized",
                "chosen_option": chosen_option,
                "rationale": rationale or "",
                "decided_at": now,
                "updated_at": now,
            }
            cur = conn.execute(
                "UPDATE counterfactual_decision SET status = :status, "
                "chosen_option = :chosen_option, rationale = :rationale, "
                "decided_at = :decided_at, updated_at = :updated_at, "
                "version = version + 1 "
                "WHERE id = :decision_id AND version = :expected_version",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"decision {decision_id!r} update affected {cur.rowcount} rows"
                )
            updated = conn.execute(
                "SELECT * FROM counterfactual_decision WHERE id = ?", (decision_id,)
            ).fetchone()
            return dict(updated)


# --------------------------------------------------------------------------
# alternative
# --------------------------------------------------------------------------

_ALTERNATIVE_COLUMNS: tuple[str, ...] = (
    "id",
    "decision_id",
    "option",
    "rejection_reason",
    "created_at",
)


def add_counterfactual_alternative(
    db_path: Path,
    decision_id: str,
    *,
    option: str,
    rejection_reason: str = "",
    alternative_id: str | None = None,
) -> dict:
    """Append one ``counterfactual_alternative`` row to ``decision_id`` and
    return it.

    Refuses an unknown decision (:class:`KeyError`) and a ``finalized`` one
    (:class:`CounterfactualDecisionFinalizedError`) — the ledger only grows
    while the decision is still being weighed."""
    if not (option or "").strip():
        raise ValueError("alternative.option must not be empty")
    now = db.iso_now()
    record = {
        "id": alternative_id or db.new_id(),
        "decision_id": decision_id,
        "option": option,
        "rejection_reason": rejection_reason or "",
        "created_at": now,
    }
    columns = ", ".join(_ALTERNATIVE_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _ALTERNATIVE_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status FROM counterfactual_decision WHERE id = ?", (decision_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such decision: {decision_id!r}")
            if row["status"] == "finalized":
                raise CounterfactualDecisionFinalizedError(
                    f"decision {decision_id!r} is finalized; its ledger is closed"
                )
            conn.execute(
                f"INSERT INTO counterfactual_alternative ({columns}) VALUES ({placeholders})",
                record,
            )
    return record


def list_counterfactual_alternatives(db_path: Path, decision_id: str) -> list[dict]:
    """List every alternative recorded against ``decision_id``, oldest first —
    the order the deliberation actually happened in."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM counterfactual_alternative WHERE decision_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (decision_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_counterfactual_alternatives(db_path: Path, decision_id: str) -> int:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM counterfactual_alternative WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return int(row["n"])
