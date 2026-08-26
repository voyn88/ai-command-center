"""Service tier for the Wave-2 Audit surface (routes → **service** → repository).

The routes in :mod:`command_center.api.audit_routes` hold no logic; they call one
function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* orchestrates a run: create an ``audit_run``, drive the storage-free
  :class:`command_center.audit.AuditRunner` over the project's tree, persist each
  finding (**always** with a status and an owner), finalize the run;
* maps stored rows onto the :mod:`command_center.api.models` contract;
* applies the BANK/LEGAL redaction policy *in SQL* — a run/finding whose
  ``project_ref`` is sensitive is dropped from every list and reads as *not
  found* on detail, so its content never leaves this surface;
* publishes domain events onto the in-process bus after a write commits.

Testability seam: the registry the runner uses (``_registry``) and every backing
call (repository functions, ``create_task``, ``is_sensitive``, ``resolve_db_path``)
is referenced through a module-level name so a test can monkeypatch it. The
runtime db path resolves under the per-test ``AICC_DATA_DIR`` sandbox.

Layering note: the tasks path is reached **only** through
``tasks_repository.create_task`` — the documented single writer of
``data/tasks.json`` — never by touching the store directly here.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import audit_schemas as a
from command_center.api import models
from command_center.audit import AuditRunner, default_registry
from command_center.audit.types import CheckContext
from command_center.events import (
    AuditFindingCreated,
    AuditFindingPromotedToTask,
    AuditRunCompleted,
    default_bus,
)
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.project_config import get_project_config, is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION
from command_center.tasks_repository import create_task

# Repo root is three levels up: <root>/command_center/api/audit_service.py
ROOT = Path(__file__).resolve().parents[2]

# Registry seam (module-level so a test can substitute a registry of fake checks).
_registry = default_registry


class SensitiveProjectRefError(Exception):
    """Raised when an audit run is requested for a BANK/LEGAL project. A
    sensitive run's findings would be redacted on every read anyway, so the run
    is *rejected* (HTTP 400) rather than persisted — no sensitive project's
    findings ever land in the store in the first place."""


class FindingNotPromotableError(Exception):
    """Raised when a promote is requested on a finding that cannot be promoted:
    one already resolved (``fixed``), or one already promoted (it carries a
    ``promoted_task_id`` / sits in ``ack``). Surfaced as HTTP 409 so a resolved
    finding is never silently re-opened, and an already-promoted finding never
    spawns a *second* orphan task and then 500s on the illegal ``ack`` → ``ack``
    transition — the double-promote is rejected cleanly before any task is
    created."""


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags (the same
    lazy-migrate pattern the Wave-1 service uses)."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def _sensitive_projects() -> list[str]:
    """The redaction exclusion list handed to the repository, in a stable order —
    the same policy :func:`is_sensitive` enforces per-row, expressed so it can be
    applied inside the SQL query."""
    return sorted(SENSITIVE_PROJECT_IDS)


def _resolve_target(project: str) -> Path:
    """The directory the checks scan for ``project``: its configured
    ``repository_path`` when that is a real directory, else the app root. Never
    invents a path — an unconfigured project audits the app tree it runs from."""
    cfg = get_project_config(project)
    raw = cfg.get("repository_path")
    if raw:
        candidate = Path(raw)
        if candidate.is_dir():
            return candidate
    return ROOT


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _run_from_row(row: dict) -> models.AuditRun:
    return models.AuditRun(
        id=row["id"],
        project_ref=row["project_ref"],
        status=row["status"],
        checks=list(row.get("checks") or []),
        finding_count=int(row.get("finding_count") or 0),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        created_at=row.get("created_at"),
    )


def _finding_from_row(row: dict) -> models.AuditFinding:
    return models.AuditFinding(
        id=row["id"],
        run_id=row["run_id"],
        category=row["category"],
        severity=row["severity"],
        summary=row.get("summary") or "",
        file_path=row.get("file_path"),
        loc=row.get("loc"),
        status=row["status"],
        owner=row["owner"],
        project_ref=row.get("project_ref"),
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# Run an audit pass
# --------------------------------------------------------------------------


def run_audit(payload: a.AuditRunRequest) -> a.AuditRunResult:
    """Run one audit pass over ``payload.project``: create the run, collect
    findings via the storage-free runner, persist each (with status + owner),
    finalize the run and emit events. Rejects a sensitive project up front."""
    project = payload.project
    if is_sensitive(project):
        raise SensitiveProjectRefError(
            f"audit for sensitive project {project!r} is rejected"
        )
    path = _db_path()
    registry = _registry()
    runner = AuditRunner(registry=registry)
    ctx = CheckContext(
        root=ROOT,
        target=_resolve_target(project),
        project=project,
        db_path=path,
    )

    # The checks this pass will run — the requested subset, or the full
    # registered set — recorded on the run row up front (the runner resolves the
    # same list, and refuses an unknown name before any row is written).
    intended_checks = payload.checks if payload.checks is not None else registry.names()
    run_row = db.create_audit_run(
        path, project_ref=project, checks=intended_checks, status="running"
    )
    run_id = run_row["id"]
    # A raise anywhere between "run created (running)" and "run finalized" must
    # not leave a dangling ``running`` row: the check pass blowing up, a
    # finding-persist failing mid-loop, or the finalize itself failing. The run
    # holds ``version == 0`` throughout (findings are separate rows; only the
    # finalize bumps it), so the failed-marking CAS at ``expected_version=0`` is
    # valid for every one of those failure points.
    try:
        result = runner.collect(ctx, checks=payload.checks)

        finding_models: list[models.AuditFinding] = []
        for finding in result.findings:
            row = db.create_audit_finding(
                path,
                run_id=run_id,
                category=finding.category,
                summary=finding.summary,
                owner=finding.owner,
                severity=finding.severity,
                file_path=finding.file_path,
                loc=finding.loc,
                project_ref=project,
            )
            finding_models.append(_finding_from_row(row))
            default_bus().publish(
                AuditFindingCreated(
                    finding_id=row["id"],
                    run_id=run_id,
                    category=row["category"],
                    severity=row["severity"],
                ),
                raise_errors=False,
            )

        finalized = db.set_audit_run_status(
            path, run_id, expected_version=0, status="completed",
            finding_count=len(finding_models),
        )
    except Exception:
        # Best-effort: mark the run failed so it is never left ``running``.
        # ``finalize`` is the last statement in the try and is atomic, so on any
        # raise here the run is still ``running`` at ``version == 0``. Guard the
        # marking anyway and swallow its errors so the original failure is what
        # propagates.
        try:
            db.set_audit_run_status(
                path, run_id, expected_version=0, status="failed"
            )
        except Exception:  # noqa: BLE001 — never mask the original failure
            pass
        raise
    default_bus().publish(
        AuditRunCompleted(
            run_id=run_id,
            project_ref=project,
            status=finalized["status"],
            finding_count=len(finding_models),
        ),
        raise_errors=False,
    )
    return a.AuditRunResult(
        run=_run_from_row(finalized),
        findings=finding_models,
        deduped=result.deduped,
    )


# --------------------------------------------------------------------------
# Runs — list / get
# --------------------------------------------------------------------------


def list_runs(
    *, project: str | None = None, status: str | None = None,
    limit: int = 100, offset: int = 0,
) -> a.AuditRunList:
    rows = db.list_audit_runs(
        _db_path(),
        project=project,
        statuses=[status] if status else None,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    return a.AuditRunList(
        runs=[_run_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_run(run_id: str) -> models.AuditRun | None:
    row = db.get_audit_run(_db_path(), run_id)
    if row is None or is_sensitive(row["project_ref"]):
        # A sensitive run reads as absent — its findings must never leak.
        return None
    return _run_from_row(row)


# --------------------------------------------------------------------------
# Findings — list / status / promote
# --------------------------------------------------------------------------


def list_findings(
    *, run_id: str | None = None, status: str | None = None,
    owner: str | None = None, limit: int = 100, offset: int = 0,
) -> a.AuditFindingList:
    rows = db.list_audit_findings(
        _db_path(),
        run_id=run_id,
        status=status,
        owner=owner,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    return a.AuditFindingList(
        findings=[_finding_from_row(r) for r in rows], limit=limit, offset=offset
    )


def set_finding_status(finding_id: str, status: str) -> models.AuditFinding | None:
    """Move a finding through its triage lifecycle. Returns ``None`` if it does
    not exist or is sensitive (treated as not found)."""
    path = _db_path()
    row = db.get_audit_finding(path, finding_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    updated = db.set_audit_finding_status(
        path, finding_id, expected_version=row["version"], status=status
    )
    return _finding_from_row(updated)


def promote_finding(finding_id: str) -> a.PromoteFindingResponse | None:
    """Promote a finding into a task: create the task through the existing tasks
    path, record the link + acknowledge the finding, and emit
    :class:`AuditFindingPromotedToTask`. Returns ``None`` if the finding does not
    exist or is sensitive (treated as not found)."""
    path = _db_path()
    row = db.get_audit_finding(path, finding_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    if row["status"] == "fixed":
        raise FindingNotPromotableError(
            f"finding {finding_id!r} is {row['status']!r}, not promotable"
        )
    # Reject a double-promote *before* creating a task. An already-promoted
    # finding carries a ``promoted_task_id`` (and sits in ``ack``); re-promoting
    # it would create a SECOND orphan task and then raise on the illegal
    # ``ack`` -> ``ack`` transition (a 500). Fail closed with a clean 409 and
    # leave the board untouched.
    if row.get("promoted_task_id") or row["status"] == "ack":
        raise FindingNotPromotableError(
            f"finding {finding_id!r} is already promoted "
            f"(task {row.get('promoted_task_id')!r}); refusing to promote again"
        )
    project_ref = row.get("project_ref") or ""
    title = _task_title(row)
    # Create the board task first (single writer: tasks_repository), then record
    # the link atomically on the finding. The db transition guard is the final
    # backstop against a double-promote.
    task = create_task(
        ROOT,
        project=project_ref or "AICC",
        title=title,
        task_type="implementation",
        status="Backlog",
        goal=row.get("summary") or title,
        owner=row.get("owner") or "",
        notes=f"Promoted from audit finding {finding_id} ({row['category']}/{row['severity']})",
    )
    updated = db.promote_audit_finding(
        path, finding_id, expected_version=row["version"], task_id=task["id"]
    )
    default_bus().publish(
        AuditFindingPromotedToTask(
            finding_id=finding_id,
            task_id=task["id"],
            project_ref=row.get("project_ref"),
        ),
        raise_errors=False,
    )
    return a.PromoteFindingResponse(
        finding=_finding_from_row(updated), task_id=task["id"]
    )


def _task_title(row: dict) -> str:
    """A concise task title from a finding: ``[category] summary`` truncated so a
    long lint message does not become an unwieldy card heading."""
    summary = (row.get("summary") or "").strip() or "audit finding"
    head = summary if len(summary) <= 80 else summary[:77] + "..."
    return f"[{row['category']}] {head}"
