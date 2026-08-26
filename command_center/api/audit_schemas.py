"""Request bodies and response wrappers for the Wave-2 Audit surface.

The *entities* returned here are the shared contract models in
:mod:`command_center.api.models` (``AuditRun``, ``AuditFinding``) — these classes
describe the **inputs** a client POSTs and the small composite responses (a run
result, a list page, a promote result) that wrap those entities.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from command_center.api.models import AuditFinding, AuditFindingStatus, AuditRun


class AuditRunRequest(BaseModel):
    """POST body for ``/audit/run``. ``project`` is required — an audit always
    targets one project. ``checks`` restricts the pass to a named subset of the
    registered checks (default: all)."""

    project: str
    checks: list[str] | None = None


class FindingStatusUpdate(BaseModel):
    """POST body for ``/audit/findings/{id}/status``: the new triage status."""

    status: AuditFindingStatus


class AuditRunResult(BaseModel):
    """The outcome of one audit pass: the finalized run plus every finding it
    persisted (each already carrying a status and an owner) and how many
    duplicate findings were collapsed."""

    run: AuditRun
    findings: list[AuditFinding]
    deduped: int = 0


class AuditRunList(BaseModel):
    """A page of audit runs plus the paging echo the client sent."""

    runs: list[AuditRun]
    limit: int
    offset: int


class AuditFindingList(BaseModel):
    """A page of audit findings."""

    findings: list[AuditFinding] = Field(default_factory=list)
    limit: int
    offset: int


class PromoteFindingResponse(BaseModel):
    """Result of promoting a finding into a task: the updated finding (now
    acknowledged and linked) and the id of the task created on the board."""

    finding: AuditFinding
    task_id: str
