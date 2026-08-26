"""Response models for the Postgres-backed autonomous delivery backlog.

Deliberately its own contract, not a reuse of :mod:`command_center.api.schemas`
``Task``: that model describes the SQLite-backed per-project task tracker
(``tasks_repository``), a different schema for a different thing (one user's
local task list vs. the distributed multi-repository autonomous delivery
queue in the server's ``backlog_task`` table). Naming the same shape twice
would be the plausible-but-wrong equivalence the reuse rule warns against —
these are not the same entity.
"""

from __future__ import annotations

from pydantic import BaseModel


class BacklogTask(BaseModel):
    task_id: str
    wave: str
    priority: str | None
    status: str
    kind: str
    title: str
    repo: str | None
    revision: int


class BacklogTaskList(BaseModel):
    tasks: list[BacklogTask]
    limit: int
    offset: int
    total: int


class BacklogStatusCounts(BaseModel):
    """One row per status the backlog's status machine allows, zero-filled —
    a status with no tasks right now still appears as 0, so a client does not
    have to know the vocabulary to render every column."""

    counts: dict[str, int]
    total: int


class BacklogEvent(BaseModel):
    event: str
    outcome: str
    reason: str | None
    actor: str
    detail: dict | None
    created_at: str


class BacklogEvidence(BaseModel):
    kind: str
    value: str
    recorded_at: str


class BacklogTaskDetail(BaseModel):
    task: BacklogTask
    events: list[BacklogEvent]
    evidence: list[BacklogEvidence]
