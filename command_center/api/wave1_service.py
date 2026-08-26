"""Service tier for the Wave-1 write surface (routes → **service** → repository).

The routes in :mod:`command_center.api.wave1_routes` hold no logic; they call
one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :mod:`command_center.api.models` contract;
* applies the BANK/LEGAL redaction policy — a proposal whose ``project_ref`` is
  sensitive is dropped from every list and reads as *not found* on detail, so
  its title never leaves this surface (the same drop-don't-mask policy the
  read-only service already applies to projects, runs and activity);
* publishes domain events onto the in-process bus after a write commits.

Testability seam: every backing call (repository functions, ``create_task``,
``is_sensitive``, ``resolve_db_path``) is referenced through a module-level name
so a test can monkeypatch it, and the runtime db path already resolves under the
per-test ``AICC_DATA_DIR`` sandbox (see ``tests/conftest.py``).

Layering note: the tasks path is reached **only** through
``tasks_repository.create_task`` — the documented single writer of
``data/tasks.json`` — never by touching the store directly here.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import models, schemas
from command_center.api import wave1_schemas as w
from command_center.digest import DigestService
from command_center.digest import complete_owner_item as _complete_owner_item
from command_center.events import (
    DigestReady,
    OwnerItemCreated,
    ProposalCreated,
    ProposalPromotedToTask,
    default_bus,
)
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION
from command_center import task_ordering, tasks_repository
from command_center.api import service as read_service
from command_center.tasks_repository import create_task

# Repo root is three levels up: <root>/command_center/api/wave1_service.py
ROOT = Path(__file__).resolve().parents[2]


class ProposalNotPromotableError(Exception):
    """Raised when a promote is requested on a proposal that is already in a
    terminal status (``converted``/``dismissed``). Surfaced as HTTP 409 so a
    double-submit never creates a second task."""


class SensitiveProjectRefError(Exception):
    """Raised when a manual write (``POST /proposals`` / ``POST /digest``) names
    a BANK/LEGAL project. A sensitive row is redacted on every read anyway, so
    the write is *rejected* (HTTP 400) rather than persisted — its title never
    lands in the store in the first place. The advisor engine never trips this:
    it drops sensitive candidates before it ever calls this service."""


def _sensitive_projects() -> list[str]:
    """The redaction exclusion list handed to the repository, in a stable order.
    Same policy :func:`is_sensitive` enforces per-row, expressed as a set so it
    can be applied inside the SQL query."""
    return sorted(SENSITIVE_PROJECT_IDS)


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags.

    ``migrate`` is idempotent, but the extra provenance backfill it runs at the
    tail is worth skipping on the hot path, so we only call it when the recorded
    version is behind — a brand-new sandbox db (each test) migrates once, an
    already-current one does a single cheap version read."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _proposal_from_row(row: dict) -> models.Proposal:
    return models.Proposal(
        id=row["id"],
        kind=row["kind"],
        title=row.get("title") or "",
        body=row.get("body") or "",
        expected_gain=row.get("expected_gain"),
        effort=row.get("effort"),
        project_ref=row["project_ref"],
        status=row["status"],
        created_at=row.get("created_at"),
    )


def _owner_item_from_row(row: dict) -> models.OwnerItem:
    return models.OwnerItem(
        id=row["id"],
        title=row.get("title") or "",
        detail=row.get("detail"),
        due=row.get("due"),
        done=bool(row.get("done")),
        source_ref=row.get("source_ref"),
        created_at=row.get("created_at"),
    )


def _digest_from_row(row: dict) -> models.DigestItem:
    return models.DigestItem(
        id=row["id"],
        title=row.get("title") or "",
        body=row.get("body") or "",
        category=row.get("category"),
        refs=list(row.get("refs") or []),
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# Советник — advisor proposals
# --------------------------------------------------------------------------


def create_proposal(payload: w.ProposalCreate) -> models.Proposal:
    if is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"proposal for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_advisor_proposal(
        _db_path(),
        kind=payload.kind,
        title=payload.title,
        project_ref=payload.project_ref,
        body=payload.body,
        expected_gain=payload.expected_gain,
        effort=payload.effort,
    )
    default_bus().publish(
        ProposalCreated(
            proposal_id=row["id"], kind=row["kind"], project_ref=row["project_ref"]
        ),
        raise_errors=False,
    )
    return _proposal_from_row(row)


def list_proposals(
    *, project: str | None = None, status: str | None = None,
    kind: str | None = None, limit: int = 100, offset: int = 0,
) -> w.ProposalList:
    rows = db.list_advisor_proposals(
        _db_path(),
        project=project,
        statuses=[status] if status else None,
        kind=kind,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    # Redaction happens in the SQL query (``exclude_projects``), so ``limit``/
    # ``offset`` page over visible rows only — no post-filter that would
    # under-return a page when a sensitive row falls inside it (audit MED-2).
    return w.ProposalList(
        proposals=[_proposal_from_row(r) for r in rows],
        limit=limit,
        offset=offset,
    )


def get_proposal(proposal_id: str) -> models.Proposal | None:
    row = db.get_advisor_proposal(_db_path(), proposal_id)
    if row is None or is_sensitive(row["project_ref"]):
        # A sensitive proposal reads as absent — its title must never leak.
        return None
    return _proposal_from_row(row)


def promote_proposal(proposal_id: str) -> w.PromoteResponse | None:
    """Promote a proposal into a task: create the task through the existing
    tasks path, record the link + terminal status on the proposal, and emit
    :class:`ProposalPromotedToTask`. Returns ``None`` if the proposal does not
    exist or is sensitive (treated as not found)."""
    path = _db_path()
    row = db.get_advisor_proposal(path, proposal_id)
    if row is None or is_sensitive(row["project_ref"]):
        return None
    if row["status"] not in ("new", "accepted"):
        raise ProposalNotPromotableError(
            f"proposal {proposal_id!r} is {row['status']!r}, not promotable"
        )
    # Create the board task first (single writer: tasks_repository), then record
    # the link atomically on the proposal. The db transition guard is the final
    # backstop against a double-promote racing past the status check above.
    task = create_task(
        ROOT,
        project=row["project_ref"],
        title=row["title"],
        task_type="implementation",
        status="Backlog",
        goal=row.get("body") or row["title"],
        notes=f"Promoted from advisor proposal {proposal_id}",
    )
    updated = db.promote_advisor_proposal(
        path, proposal_id, expected_version=row["version"], task_id=task["id"]
    )
    default_bus().publish(
        ProposalPromotedToTask(
            proposal_id=proposal_id,
            task_id=task["id"],
            project_ref=row["project_ref"],
        ),
        raise_errors=False,
    )
    return w.PromoteResponse(proposal=_proposal_from_row(updated), task_id=task["id"])


# --------------------------------------------------------------------------
# Task priority order (VOYN-W2-TASKS)
# --------------------------------------------------------------------------


def reorder_tasks(project: str, order: list[str]) -> schemas.TaskGraph:
    """Persist a new priority order for ``project``'s tasks, or reject it.

    The invariant is enforced *inside the lock*, against the store as it is on
    disk right now: the mutator re-validates ``order`` with
    :func:`task_ordering.validated_order` (a task may never out-rank a
    dependency, and a cyclic set is refused) before stamping ``priority_rank``,
    so a concurrent edit that added a dependency cannot slip a now-illegal order
    past a check made against a stale snapshot. A :class:`task_ordering.
    ReorderError` propagates out unpersisted and the route maps it to ``409``.

    Sensitive (BANK/LEGAL) projects are refused as "not found" — their tasks are
    dropped from every read on this surface, so their order is not orderable
    here. Returns the fresh graph so the client repaints from the committed
    state rather than trusting its optimistic local order.
    """
    if is_sensitive(project):
        raise TaskReorderRejected("dependency_order", "Проект недоступен.")

    def _mutator(tasks: list[dict]) -> None:
        scoped = {
            str(t.get("id", "")): t
            for t in tasks
            if t.get("project") == project
        }
        if set(order) != set(scoped):
            raise TaskReorderRejected(
                task_ordering.CODE_UNKNOWN_TASK,
                "Список задач изменился — обновите страницу и повторите.",
            )
        try:
            legal = task_ordering.validated_order(order, scoped)
        except task_ordering.ReorderError as exc:
            raise TaskReorderRejected(exc.code, exc.message) from exc
        for rank, task_id in enumerate(legal):
            scoped[task_id]["priority_rank"] = rank

    tasks_repository.mutate_tasks(ROOT, _mutator)
    return read_service.task_graph(project)


class TaskReorderRejected(Exception):
    """Raised when a requested task order violates the dependency invariant.

    Carries the ``code`` (``dependency_order`` / ``dependency_cycle`` /
    ``unknown_task`` …) and the operator-facing ``message`` so the route can
    return a ``409`` whose body names exactly why the reorder was refused."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# «Мой день» — owner items
# --------------------------------------------------------------------------


def create_owner_item(payload: w.OwnerItemCreate) -> models.OwnerItem:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"owner item for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_owner_item(
        _db_path(),
        title=payload.title,
        detail=payload.detail,
        due=payload.due,
        source_ref=payload.source_ref,
        project_ref=payload.project_ref,
        done=payload.done,
    )
    default_bus().publish(
        OwnerItemCreated(item_id=row["id"], title=row["title"]),
        raise_errors=False,
    )
    return _owner_item_from_row(row)


def list_owner_items(
    *, done: bool | None = None, limit: int = 100, offset: int = 0
) -> w.OwnerItemList:
    rows = db.list_owner_items(
        _db_path(), done=done, exclude_projects=_sensitive_projects(),
        limit=limit, offset=offset,
    )
    return w.OwnerItemList(
        items=[_owner_item_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_owner_item(item_id: str) -> models.OwnerItem | None:
    row = db.get_owner_item(_db_path(), item_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        # A sensitive owner item reads as absent — the same drop-don't-mask
        # policy proposals use; its title never leaves this surface.
        return None
    return _owner_item_from_row(row)


# --------------------------------------------------------------------------
# Дайджест — digest items
# --------------------------------------------------------------------------


def create_digest_item(payload: w.DigestItemCreate) -> models.DigestItem:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"digest entry for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_digest_item(
        _db_path(),
        title=payload.title,
        body=payload.body,
        category=payload.category,
        refs=payload.refs,
        project_ref=payload.project_ref,
    )
    default_bus().publish(
        DigestReady(digest_id=row["id"], category=row.get("category")),
        raise_errors=False,
    )
    return _digest_from_row(row)


def list_digest_items(
    *, category: str | None = None, limit: int = 100, offset: int = 0
) -> w.DigestItemList:
    rows = db.list_digest_items(
        _db_path(), category=category, exclude_projects=_sensitive_projects(),
        limit=limit, offset=offset,
    )
    return w.DigestItemList(
        items=[_digest_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_digest_item(item_id: str) -> models.DigestItem | None:
    row = db.get_digest_item(_db_path(), item_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    return _digest_from_row(row)


# --------------------------------------------------------------------------
# Дайджест build / «Мой день» complete — delegate to the digest engine
# --------------------------------------------------------------------------
#
# The morning-digest assembly and the owner-item auto-fill/complete logic live
# in ``command_center.digest`` (the Wave-1 «Мой день»/Дайджест engine). This
# service maps its rows onto the API contract, keeping the controller thin and
# the domain engine free of HTTP concerns.


def build_digest() -> w.DigestItemList:
    """Build (idempotently rebuild) today's morning digest and return it in
    order. Backs ``POST /digest/build``."""
    rows = DigestService(root=ROOT).build()
    return w.DigestItemList(
        items=[_digest_from_row(r) for r in rows], limit=len(rows), offset=0
    )


def list_digest_today() -> w.DigestItemList:
    """Today's already-built digest in assembly order. Backs ``GET /digest/today``."""
    rows = DigestService(root=ROOT).today()
    return w.DigestItemList(
        items=[_digest_from_row(r) for r in rows], limit=len(rows), offset=0
    )


def complete_owner_item(item_id: str) -> models.OwnerItem | None:
    """Mark an owner item done; ``None`` if it does not exist. Backs
    ``POST /owner-items/{id}/complete``."""
    row = _complete_owner_item(item_id, root=ROOT)
    return _owner_item_from_row(row) if row is not None else None
