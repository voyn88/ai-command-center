"""Pydantic response models — the wire contract for the read-only API.

These schemas are the single source of truth both the desktop and the future
mobile shell code against, so they are deliberately explicit and typed rather
than free-form dicts. Every model here describes a *response* body; there are
no request bodies because this increment is read-only.

Redaction note: sensitive projects (BANK/LEGAL, per
``project_config.is_sensitive``) never carry raw repository/health detail on
this surface. The service layer drops those fields and sets ``redacted=True``
before a :class:`Project` is constructed — the same policy the existing
``command_center/webapi`` surface applies. The schema keeps the redacted-safe
fields optional so a redacted project is still a valid :class:`Project`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """``GET /api/health`` — liveness probe."""

    status: Literal["ok"] = "ok"
    service: str = "ai-command-center-api"
    version: str


class ProjectHealth(BaseModel):
    """Read-only health signals for one project, collected without any
    network-mutating git/gh call (see ``integration.collectors``). Every field
    degrades to ``None``/``False`` rather than raising when a checkout, the
    ``gh`` CLI, or the network is unavailable."""

    worktree_state: Literal[
        "unconfigured", "invalid_path", "not_git_repo", "ok"
    ] = "unconfigured"
    git_available: bool = False
    branch: str | None = None
    dirty: bool = False
    last_activity: str | None = None
    github_available: bool = False
    open_pr_count: int | None = None
    ci_state: str | None = None


class Project(BaseModel):
    """A registered project, joined onto its computed health and a progress
    proxy. ``project_ref`` is the ``models.PROJECT_IDS`` namespace the registry
    entry attaches to (how it joins onto the task board)."""

    id: str
    name: str
    kind: str = "other"
    project_ref: str
    healthy: bool = True
    redacted: bool = False
    progress: int | None = Field(
        default=None,
        description="done/total task percentage for this project_ref, or "
        "null when no tasks exist (a proxy, not a stored value).",
    )
    health: ProjectHealth | None = None


class ActivityItem(BaseModel):
    """One entry in the recent-activity feed. ``kind`` distinguishes a run
    event (from the runtime run read model) from a commit (from the git
    readers)."""

    kind: Literal["run", "commit"]
    project: str | None = None
    title: str
    ref: str | None = None
    ts: str | None = None


class AttentionItem(BaseModel):
    """Something that needs a human decision: a task flagged for attention or a
    run in a failed/interrupted state."""

    kind: Literal["task", "run"]
    project: str | None = None
    title: str
    detail: str | None = None
    ref: str | None = None


class AgentSummary(BaseModel):
    """Run-state counts. There is no fixed agent pool, so a "free" count is not
    a meaningful stored quantity; the derivable signal is how many runs are
    live/waiting/needing-attention right now (``read_model.run_snapshot``)."""

    running: int = 0
    queued: int = 0
    attention: int = 0
    total: int = 0


class TaskCounts(BaseModel):
    """Canonical task counts (``read_model.task_snapshot``). ``by_lane`` covers
    every canonical lane; ``sum(by_lane.values()) + other == total``."""

    total: int = 0
    by_lane: dict[str, int] = Field(default_factory=dict)
    other: int = 0
    done: int = 0
    blocked: int = 0
    active: int = 0
    attention: int = 0


class DashboardResponse(BaseModel):
    """``GET /api/dashboard`` — the one-shot aggregate the shells poll."""

    agents: AgentSummary
    task_counts: TaskCounts
    projects: list[Project]
    activity: list[ActivityItem]
    attention: list[AttentionItem]


class Task(BaseModel):
    """A single task, as stored by the tasks repository. Fields beyond the core
    identifiers are optional because the store's historical rows do not all
    carry them."""

    id: str
    project: str
    title: str
    task_type: str | None = None
    status: str | None = None
    priority: str | None = None
    owner: str | None = None
    launch_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)


class TaskList(BaseModel):
    """``GET /api/tasks`` — cross-project task list plus canonical counts over
    the *filtered* set."""

    tasks: list[Task]
    counts: TaskCounts


class TaskGraphNode(BaseModel):
    """One task in a project's dependency-levelled graph (``GET /tasks/graph``).

    ``level`` is the dependency depth (level 0 depends on nothing still open);
    ``state`` is the board's semantic state (done/running/blocked/ready/waiting);
    ``rank`` is the task's position in the *priority order* the operator controls
    — distinct from ``level``, which the dependency graph fixes."""

    id: str
    project: str
    title: str
    status: str | None = None
    priority: str | None = None
    level: int = 0
    state: str = "waiting"
    rank: int = 0
    depends_on: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)


class TaskGraph(BaseModel):
    """``GET /api/tasks/graph`` — one project's tasks as a dependency graph plus
    the current priority order (nodes are returned in priority-rank order)."""

    project: str | None = None
    nodes: list[TaskGraphNode] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    has_cycle: bool = False


class Agent(BaseModel):
    """``GET /api/agents`` — one run's current state, from the runtime run read
    model. Named "agent" because from a shell's perspective a live run *is* an
    agent doing work; ``agent`` carries the executor/agent identity when known."""

    run_id: str
    source: str | None = None
    project: str | None = None
    task_id: str | None = None
    task_type: str | None = None
    state: str | None = None
    status: str | None = None
    agent: str | None = None
    created_at: str | None = None
    started_at: str | None = None


class AgentsResponse(BaseModel):
    """``GET /api/agents`` — the run summary plus the individual live/recent
    runs. When the runtime store has not been initialised yet this degrades to
    an empty list with a zeroed summary (a documented stub shape) rather than
    erroring."""

    summary: AgentSummary
    agents: list[Agent]
    available: bool = True
