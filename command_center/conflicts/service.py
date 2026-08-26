"""Service tier for the Wave-2 conflicts engine (routes → **service** →
repository → db).

The routes in :mod:`command_center.api.conflict_routes` hold no logic; they call
one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :class:`command_center.api.models.Conflict` contract;
* applies the BANK/LEGAL redaction policy — a conflict whose ``project_ref`` is
  sensitive is dropped from every list and reads as *not found* on detail, so its
  ``source_ref`` never leaves this surface (the drop-don't-mask policy the
  read-only and Wave-1 services already apply);
* **owns the resolve invariant**: a conflict may only move to ``resolved`` once
  it carries a mitigation *and* an owner. This is policy, enforced here — the
  repository is the structural backstop (legal edge + compare-and-set), never
  the arbiter of business rules.

Testability seam: every backing call (repository functions, ``is_sensitive``,
``resolve_db_path``) is referenced through a module-level name so a test can
monkeypatch it, and the runtime db path resolves under the per-test
``AICC_DATA_DIR`` sandbox (see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import conflict_schemas as s
from command_center.api import models
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/conflicts/service.py
ROOT = Path(__file__).resolve().parents[2]


class SensitiveProjectRefError(Exception):
    """Raised when a manual ``POST /conflicts`` names a BANK/LEGAL project. A
    sensitive row is redacted on every read anyway, so the write is *rejected*
    (HTTP 400) rather than persisted — its ``source_ref`` never lands in the
    store. The incident intake never trips this: it drops sensitive incidents
    before it ever calls this service."""


class ConflictNotResolvableError(Exception):
    """Raised when a resolve is requested on a conflict that does not yet carry
    both a mitigation and an owner. Surfaced as HTTP 409 — the conflict cannot be
    resolved *in its current state*, and the client must assign an owner and
    record a mitigation first."""


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


def _conflict_from_row(row: dict) -> models.Conflict:
    return models.Conflict(
        id=row["id"],
        kind=row["kind"],
        source_ref=row.get("source_ref") or "",
        severity=row["severity"],
        status=row["status"],
        owner=row.get("owner"),
        mitigation=row.get("mitigation"),
        project_ref=row.get("project_ref"),
        opened_at=row.get("opened_at"),
        resolved_at=row.get("resolved_at"),
    )


# --------------------------------------------------------------------------
# create / list / get
# --------------------------------------------------------------------------


def create_conflict(payload: s.ConflictCreate) -> models.Conflict:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"conflict for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_conflict(
        _db_path(),
        kind=payload.kind,
        source_ref=payload.source_ref,
        severity=payload.severity,
        project_ref=payload.project_ref,
        owner=payload.owner,
        mitigation=payload.mitigation,
    )
    return _conflict_from_row(row)


def list_conflicts(
    *,
    kind: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> s.ConflictList:
    rows = db.list_conflicts(
        _db_path(),
        kind=kind,
        status=status,
        owner=owner,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    # Redaction happens in the SQL query (``exclude_projects``), so ``limit``/
    # ``offset`` page over visible rows only — no post-filter that would
    # under-return a page when a sensitive row falls inside it.
    return s.ConflictList(
        conflicts=[_conflict_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_conflict(conflict_id: str) -> models.Conflict | None:
    row = db.get_conflict(_db_path(), conflict_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        # A sensitive conflict reads as absent — its source_ref must never leak.
        return None
    return _conflict_from_row(row)


# --------------------------------------------------------------------------
# workflow — assign / mitigate / resolve
# --------------------------------------------------------------------------


def assign_conflict(conflict_id: str, owner: str) -> models.Conflict | None:
    """Set (or reassign) the owner of a conflict. Returns ``None`` when the
    conflict does not exist or is sensitive (treated as not found)."""
    path = _db_path()
    row = db.get_conflict(path, conflict_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    updated = db.update_conflict_fields(
        path, conflict_id, expected_version=row["version"], fields={"owner": owner}
    )
    return _conflict_from_row(updated)


def set_mitigation(conflict_id: str, mitigation: str) -> models.Conflict | None:
    """Record a conflict's mitigation plan and, when it is still ``open``, advance
    it to ``mitigating`` in the same call (the natural open → mitigating step).
    Returns ``None`` when the conflict does not exist or is sensitive."""
    path = _db_path()
    row = db.get_conflict(path, conflict_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    updated = db.update_conflict_fields(
        path, conflict_id, expected_version=row["version"],
        fields={"mitigation": mitigation},
    )
    if updated["status"] == "open":
        updated = db.set_conflict_status(
            path, conflict_id, expected_version=updated["version"], status="mitigating"
        )
    return _conflict_from_row(updated)


def resolve_conflict(conflict_id: str) -> models.Conflict | None:
    """Resolve a conflict — but only if it already carries **both** a mitigation
    and an owner. This is the engine's core invariant, enforced here in the
    service (not the DB): a resolve that lacks either raises
    :class:`ConflictNotResolvableError` (HTTP 409) and nothing is written.

    Returns ``None`` when the conflict does not exist or is sensitive (treated as
    not found)."""
    path = _db_path()
    row = db.get_conflict(path, conflict_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    missing = []
    if not (row.get("owner") or "").strip():
        missing.append("owner")
    if not (row.get("mitigation") or "").strip():
        missing.append("mitigation")
    if missing:
        raise ConflictNotResolvableError(
            f"conflict {conflict_id!r} cannot be resolved without {' and '.join(missing)}"
        )
    updated = db.set_conflict_status(
        path, conflict_id, expected_version=row["version"], status="resolved"
    )
    return _conflict_from_row(updated)
