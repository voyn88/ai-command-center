"""Request bodies and thin response wrappers for the Wave-1 write surface.

The *entities* returned on this surface are the shared contract models in
:mod:`command_center.api.models` (``Proposal``, ``OwnerItem``, ``DigestItem``) —
these classes only describe the **inputs** a client POSTs and the small
composite responses (a list page, a promote result) that wrap those entities.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from command_center.api.models import DigestItem, OwnerItem, Proposal, ProposalKind


class TaskReorderRequest(BaseModel):
    """POST body for setting a project's task priority order (VOYN-W2-TASKS).

    ``order`` is the full list of task ids in the desired priority sequence
    (highest priority first). The server validates it against the dependency
    invariant before persisting, so an order that would out-rank a dependency —
    or a set containing a cycle — is rejected with ``409`` and nothing is
    written. ``project`` scopes the reorder to one project's namespace."""

    project: str
    order: list[str] = Field(default_factory=list)


class ProposalCreate(BaseModel):
    """POST body for creating an advisor proposal. ``kind`` and ``project_ref``
    are required (a proposal is always classified and always belongs to a
    project); the id/status/timestamp are assigned by the server."""

    kind: ProposalKind
    title: str
    project_ref: str
    body: str = ""
    expected_gain: str | None = None
    effort: str | None = None


class OwnerItemCreate(BaseModel):
    """POST body for creating a «Мой день» item. ``project_ref`` is optional —
    when it names a BANK/LEGAL project the write is rejected (redaction), and
    when set it lets the read path drop the row if that project is sensitive."""

    title: str
    detail: str | None = None
    due: str | None = None
    source_ref: str | None = None
    project_ref: str | None = None
    done: bool = False


class DigestItemCreate(BaseModel):
    """POST body for creating a Дайджест entry. ``project_ref`` is optional —
    when it names a BANK/LEGAL project the write is rejected (redaction)."""

    title: str
    body: str = ""
    category: str | None = None
    refs: list[str] = Field(default_factory=list)
    project_ref: str | None = None


class ProposalList(BaseModel):
    """A page of advisor proposals plus the paging echo the client sent."""

    proposals: list[Proposal]
    limit: int
    offset: int


class OwnerItemList(BaseModel):
    """A page of owner items."""

    items: list[OwnerItem]
    limit: int
    offset: int


class DigestItemList(BaseModel):
    """A page of digest entries."""

    items: list[DigestItem]
    limit: int
    offset: int


class PromoteResponse(BaseModel):
    """Result of promoting a proposal into a task: the updated proposal (now
    ``status='converted'``) and the id of the task created on the board."""

    proposal: Proposal
    task_id: str
