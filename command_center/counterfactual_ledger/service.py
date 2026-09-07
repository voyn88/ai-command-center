"""Service tier for the Counterfactual Ledger (routes → **service** →
repository → db).

The routes in :mod:`command_center.api.counterfactual_ledger_routes` hold no
logic; they call one function here per endpoint. This module is the only
place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :mod:`command_center.api.models` contract;
* applies the BANK/LEGAL redaction policy — a decision whose ``project_ref`` is
  sensitive is dropped from every list and reads as *not found* on detail, and
  a manual write naming a sensitive project is rejected, so its title/rationale
  never leaves this surface (the drop-don't-mask policy the Wave-1/2/3 services
  already apply);
* **owns the acceptance rule**: a ``critical`` decision may only finalize once
  it carries at least
  :data:`command_center.runtime.db.counterfactual_ledger.MIN_ALTERNATIVES_FOR_CRITICAL`
  recorded alternatives. This is policy, enforced here — the repository is the
  structural backstop (legal edge + compare-and-set), never the arbiter of
  business rules.

Testability seam: every backing call (repository functions, ``is_sensitive``,
``resolve_db_path``) is referenced through a module-level name so a test can
monkeypatch it, and the runtime db path resolves under the per-test
``AICC_DATA_DIR`` sandbox (see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import counterfactual_ledger_schemas as s
from command_center.api import models
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.counterfactual_ledger import MIN_ALTERNATIVES_FOR_CRITICAL
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/counterfactual_ledger/service.py
ROOT = Path(__file__).resolve().parents[2]


class SensitiveProjectRefError(Exception):
    """Raised when a manual ``POST /decisions`` names a BANK/LEGAL project. A
    sensitive row is redacted on every read anyway, so the write is *rejected*
    (HTTP 400) rather than persisted — its title/rationale never lands in the
    store."""


class DecisionNotFinalizableError(Exception):
    """Raised when ``/decisions/{id}/finalize`` is requested on a ``critical``
    decision that carries fewer than :data:`MIN_ALTERNATIVES_FOR_CRITICAL`
    recorded alternatives. Surfaced as HTTP 409 — the decision cannot be
    finalized *in its current state*, and the client must record more
    alternatives first."""


def _sensitive_projects() -> list[str]:
    """The redaction exclusion list handed to the repository, in a stable order —
    the same policy :func:`is_sensitive` enforces per-row, expressed as a set so
    it can be applied inside the SQL query."""
    return sorted(SENSITIVE_PROJECT_IDS)


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags. ``migrate``
    is idempotent; the version pre-check keeps the hot path a single cheap read on
    an already-current db while a brand-new sandbox db (each test) migrates once."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def _decision_from_row(row: dict) -> models.Decision:
    return models.Decision(
        id=row["id"],
        title=row["title"],
        description=row.get("description") or "",
        criticality=row["criticality"],
        status=row["status"],
        chosen_option=row.get("chosen_option") or "",
        rationale=row.get("rationale") or "",
        owner=row.get("owner"),
        project_ref=row.get("project_ref"),
        decided_at=row.get("decided_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _alternative_from_row(row: dict) -> models.Alternative:
    return models.Alternative(
        id=row["id"],
        decision_id=row["decision_id"],
        option=row["option"],
        rejection_reason=row.get("rejection_reason") or "",
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# create / list / get
# --------------------------------------------------------------------------


def create_decision(payload: s.DecisionCreate) -> models.Decision:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"decision for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_counterfactual_decision(
        _db_path(),
        title=payload.title,
        description=payload.description,
        criticality=payload.criticality,
        owner=payload.owner,
        project_ref=payload.project_ref,
    )
    return _decision_from_row(row)


def list_decisions(
    *,
    criticality: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> s.DecisionList:
    rows = db.list_counterfactual_decisions(
        _db_path(),
        criticality=criticality,
        status=status,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    # Redaction happens in the SQL query (``exclude_projects``), so ``limit``/
    # ``offset`` page over visible rows only — no post-filter that would
    # under-return a page when a sensitive row falls inside it.
    return s.DecisionList(
        decisions=[_decision_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_decision(decision_id: str) -> models.Decision | None:
    row = db.get_counterfactual_decision(_db_path(), decision_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        # A sensitive decision reads as absent — its title/rationale must never leak.
        return None
    return _decision_from_row(row)


# --------------------------------------------------------------------------
# alternatives
# --------------------------------------------------------------------------


def add_alternative(
    decision_id: str, payload: s.AlternativeCreate
) -> models.Alternative | None:
    """Record one alternative against ``decision_id``. Returns ``None`` when the
    decision does not exist or is sensitive (treated as not found).

    Raises :class:`db.CounterfactualDecisionFinalizedError` when the decision is
    already ``finalized`` — the ledger only grows while a decision is still
    being weighed."""
    path = _db_path()
    row = db.get_counterfactual_decision(path, decision_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    created = db.add_counterfactual_alternative(
        path,
        decision_id,
        option=payload.option,
        rejection_reason=payload.rejection_reason,
    )
    return _alternative_from_row(created)


def list_alternatives(decision_id: str) -> s.AlternativeList | None:
    """List every alternative recorded against ``decision_id``. Returns ``None``
    when the decision does not exist or is sensitive (treated as not found)."""
    row = db.get_counterfactual_decision(_db_path(), decision_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    rows = db.list_counterfactual_alternatives(_db_path(), decision_id)
    return s.AlternativeList(alternatives=[_alternative_from_row(r) for r in rows])


# --------------------------------------------------------------------------
# workflow — finalize
# --------------------------------------------------------------------------


def finalize_decision(
    decision_id: str, payload: s.DecisionFinalize
) -> models.Decision | None:
    """Finalize a decision — but only if it is ``normal`` criticality, or is
    ``critical`` and already carries at least
    :data:`MIN_ALTERNATIVES_FOR_CRITICAL` recorded alternatives. This is the
    engine's core acceptance rule, enforced here in the service (not the DB): a
    finalize that fails it raises :class:`DecisionNotFinalizableError` (HTTP
    409) and nothing is written.

    Returns ``None`` when the decision does not exist or is sensitive (treated
    as not found)."""
    path = _db_path()
    row = db.get_counterfactual_decision(path, decision_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    if row["criticality"] == "critical":
        recorded = db.count_counterfactual_alternatives(path, decision_id)
        if recorded < MIN_ALTERNATIVES_FOR_CRITICAL:
            raise DecisionNotFinalizableError(
                f"decision {decision_id!r} is critical and carries only "
                f"{recorded} alternative(s); at least {MIN_ALTERNATIVES_FOR_CRITICAL} "
                "are required before it can be finalized"
            )
    updated = db.finalize_counterfactual_decision(
        path,
        decision_id,
        expected_version=row["version"],
        chosen_option=payload.chosen_option,
        rationale=payload.rationale,
    )
    return _decision_from_row(updated)
