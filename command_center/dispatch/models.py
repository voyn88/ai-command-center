"""Pure dataclasses and typed reason codes for the dispatch policy layer.

No I/O here — everything in this module is a value object, so the policy
engine that consumes them (`command_center.dispatch.policy`) stays pure and
hermetically testable. Every "why was this task not assigned" answer is a
member of `DeferReason`, never a free-form string, so a caller can branch on
the reason instead of parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Typed reason codes
# --------------------------------------------------------------------------

# A task WAS assigned to an executor.
ASSIGNED = "assigned"

# Deferred (stays queued). Each is a *typed* reason, never force-run.
DEFER_KILL_SWITCH = "kill_switch_engaged"
DEFER_DAILY_BUDGET = "daily_budget_exhausted"
DEFER_AGENT_BUDGET = "agent_budget_exceeded"
DEFER_PROJECT_BUDGET = "project_budget_exceeded"
DEFER_AGENT_CAPACITY = "agent_capacity_reached"
DEFER_NO_ELIGIBLE_EXECUTOR = "no_eligible_executor"
DEFER_NO_AVAILABLE_EXECUTOR = "no_available_executor"
# Distinct from DEFER_DAILY_BUDGET on purpose: the trailing-24h spend could not
# be computed as a trustworthy number (see `task_pipeline.SpendUnknownError`),
# not computed and found at/over the ceiling. Every task defers rather than
# assigning against a guessed number, but the *reason* stays honest.
DEFER_SPEND_UNKNOWN = "daily_spend_unknown"

DEFER_REASONS = frozenset(
    {
        DEFER_KILL_SWITCH,
        DEFER_DAILY_BUDGET,
        DEFER_AGENT_BUDGET,
        DEFER_PROJECT_BUDGET,
        DEFER_AGENT_CAPACITY,
        DEFER_NO_ELIGIBLE_EXECUTOR,
        DEFER_NO_AVAILABLE_EXECUTOR,
        DEFER_SPEND_UNKNOWN,
    }
)

# `DispatchPlan.spend_status`: whether `daily_spend_usd` on the plan is a real,
# trustworthy trailing-24h figure or a placeholder because it could not be
# computed. Never inferred from the number itself (a placeholder 0.0 must not
# be read as "nothing spent") — always carried as its own explicit field.
SPEND_STATUS_KNOWN = "known"
SPEND_STATUS_UNKNOWN = "unknown"

# Human-readable one-liners, kept next to the codes so both the API and the
# operator UI render the same explanation.
REASON_EXPLANATIONS: dict[str, str] = {
    ASSIGNED: "Assigned to the cheapest eligible executor within budget.",
    DEFER_KILL_SWITCH: (
        "Kill switch engaged (master switch off): no automatic dispatch."
    ),
    DEFER_DAILY_BUDGET: (
        "Assigning any eligible executor would exceed the daily spend budget."
    ),
    DEFER_AGENT_BUDGET: (
        "Every eligible executor is at or over its per-agent spend limit."
    ),
    DEFER_PROJECT_BUDGET: (
        "Assigning would exceed the project's spend limit."
    ),
    DEFER_AGENT_CAPACITY: (
        "Every eligible executor is at its per-agent concurrency limit."
    ),
    DEFER_NO_ELIGIBLE_EXECUTOR: (
        "No executor is permitted for this task by project/pin policy."
    ),
    DEFER_NO_AVAILABLE_EXECUTOR: "No permitted executor is currently available.",
    DEFER_SPEND_UNKNOWN: (
        "Trailing 24h spend could not be computed as a trustworthy number "
        "(corrupt cost data or a database fault): deferring rather than "
        "assigning against a guessed figure or a false budget-exhausted verdict."
    ),
}


def explanation_for(reason: str) -> str:
    return REASON_EXPLANATIONS.get(reason, reason)


# --------------------------------------------------------------------------
# Priority ordering (SLA/priority is never bypassed)
# --------------------------------------------------------------------------

# Higher weight == scheduled first. Matches `models.TASK_PRIORITIES`.
DEFAULT_PRIORITY_WEIGHTS: dict[str, int] = {
    "Critical": 40,
    "High": 30,
    "Medium": 20,
    "Low": 10,
}

# Executors treated as local (cost economy). Cloud executors are everything
# else. The cost matrix is what actually drives selection; this set only marks
# the "local first" tie-break and is overridable via policy.
DEFAULT_LOCAL_EXECUTOR_IDS = frozenset({"ollama"})

# Fallback per-task cost when the cost matrix names no price for an executor.
DEFAULT_COST_USD = 1.0


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorProfile:
    """One candidate executor as the policy engine sees it.

    `cost_per_task_usd` is resolved from the policy's cost matrix by the
    service before the engine runs, so the engine itself never reaches into
    configuration."""

    id: str
    label: str
    kind: str  # "cli" | "chat" | "human" | "remote"
    is_local: bool
    available: bool
    cost_per_task_usd: float


@dataclass(frozen=True)
class QueuedTask:
    """A task waiting to be dispatched, reduced to only what the policy needs."""

    id: str
    project: str | None
    priority: str
    # The executors this task is *permitted* to run on (project policy). None
    # means "unconstrained"; an empty frozenset means "explicitly nothing".
    allowed_executors: frozenset[str] | None = None
    # A hard pin (e.g. `executor_pinned`): if set, only this executor is
    # eligible.
    pinned_executor: str | None = None
    # ISO-8601 SLA deadline (earliest first); None sorts last.
    sla_deadline: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class AgentLimit:
    """Per-agent guardrails. `0`/`0.0` means "unset" (no limit)."""

    max_concurrent: int = 0
    max_spend_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "max_spend_usd": self.max_spend_usd,
        }

    @classmethod
    def from_dict(cls, data: object) -> "AgentLimit":
        if not isinstance(data, dict):
            return cls()
        return cls(
            max_concurrent=_non_negative_int(data.get("max_concurrent"), 0),
            max_spend_usd=_non_negative_float(data.get("max_spend_usd"), 0.0),
        )


@dataclass(frozen=True)
class DispatchPolicy:
    """The config-driven dispatch policy (like the advisor's AutoRule).

    Persisted as `data/dispatch_policy.json`. Everything is fail-closed: an
    unparseable field falls back to the safe default rather than widening a
    budget or disabling a guardrail.
    """

    prefer_local: bool = True
    cost_matrix: dict[str, float] = field(default_factory=dict)
    default_cost_usd: float = DEFAULT_COST_USD
    per_agent_limits: dict[str, AgentLimit] = field(default_factory=dict)
    # project id -> max spend (USD) for this dispatch window. 0.0 == unset.
    per_project_limits: dict[str, float] = field(default_factory=dict)
    priority_weights: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_PRIORITY_WEIGHTS)
    )
    local_executor_ids: frozenset[str] = DEFAULT_LOCAL_EXECUTOR_IDS
    updated_at: str | None = None
    updated_by: str | None = None

    def cost_for(self, executor_id: str) -> float:
        value = self.cost_matrix.get(executor_id)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, float(value))
        return self.default_cost_usd

    def priority_weight(self, priority: str) -> int:
        return self.priority_weights.get(priority, 0)

    def is_local(self, executor_id: str) -> bool:
        return executor_id in self.local_executor_ids

    def as_dict(self) -> dict:
        return {
            "prefer_local": self.prefer_local,
            "cost_matrix": dict(self.cost_matrix),
            "default_cost_usd": self.default_cost_usd,
            "per_agent_limits": {
                k: v.as_dict() for k, v in self.per_agent_limits.items()
            },
            "per_project_limits": dict(self.per_project_limits),
            "priority_weights": dict(self.priority_weights),
            "local_executor_ids": sorted(self.local_executor_ids),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DispatchPolicy":
        """Total and fail-closed: anything that is not a well-typed dict of
        recognized values yields the safe defaults for that field."""
        if not isinstance(data, dict):
            return cls()
        cost_matrix = {
            str(k): max(0.0, float(v))
            for k, v in _as_dict(data.get("cost_matrix")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        per_agent = {
            str(k): AgentLimit.from_dict(v)
            for k, v in _as_dict(data.get("per_agent_limits")).items()
        }
        per_project = {
            str(k): max(0.0, float(v))
            for k, v in _as_dict(data.get("per_project_limits")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        weights = {
            str(k): int(v)
            for k, v in _as_dict(data.get("priority_weights")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        } or dict(DEFAULT_PRIORITY_WEIGHTS)
        local_ids = data.get("local_executor_ids")
        local_set = (
            frozenset(str(x) for x in local_ids)
            if isinstance(local_ids, (list, tuple, set, frozenset))
            else DEFAULT_LOCAL_EXECUTOR_IDS
        )
        return cls(
            prefer_local=data.get("prefer_local", True) is not False,
            cost_matrix=cost_matrix,
            default_cost_usd=_non_negative_float(
                data.get("default_cost_usd"), DEFAULT_COST_USD
            ),
            per_agent_limits=per_agent,
            per_project_limits=per_project,
            priority_weights=weights,
            local_executor_ids=local_set,
            updated_at=data.get("updated_at"),
            updated_by=data.get("updated_by"),
        )


@dataclass(frozen=True)
class DispatchDecision:
    """The outcome for exactly one task."""

    task_id: str
    project: str | None
    priority: str
    reason: str  # ASSIGNED or a DEFER_* code
    assigned_executor: str | None = None
    estimated_cost_usd: float = 0.0

    @property
    def assigned(self) -> bool:
        return self.reason == ASSIGNED and self.assigned_executor is not None

    @property
    def explanation(self) -> str:
        return explanation_for(self.reason)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "priority": self.priority,
            "reason": self.reason,
            "assigned": self.assigned,
            "assigned_executor": self.assigned_executor,
            "estimated_cost_usd": self.estimated_cost_usd,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class DispatchPlan:
    """The whole plan: one decision per task plus the budget arithmetic."""

    decisions: tuple[DispatchDecision, ...]
    kill_switch_engaged: bool
    daily_spend_usd: float
    max_daily_spend_usd: float
    projected_spend_usd: float
    # SPEND_STATUS_KNOWN or SPEND_STATUS_UNKNOWN. When unknown, `daily_spend_usd`
    # / `projected_spend_usd` are placeholders (not a real trailing-24h figure)
    # and every decision defers with DEFER_SPEND_UNKNOWN — read this field
    # first, never infer "known" from the numbers looking plausible.
    spend_status: str = SPEND_STATUS_KNOWN

    @property
    def assignments(self) -> tuple[DispatchDecision, ...]:
        return tuple(d for d in self.decisions if d.assigned)

    @property
    def deferred(self) -> tuple[DispatchDecision, ...]:
        return tuple(d for d in self.decisions if not d.assigned)

    @property
    def budget_remaining_usd(self) -> float:
        if self.max_daily_spend_usd <= 0:
            return float("inf")
        return self.max_daily_spend_usd - self.projected_spend_usd

    def as_dict(self) -> dict:
        remaining = self.budget_remaining_usd
        return {
            "kill_switch_engaged": self.kill_switch_engaged,
            "spend_status": self.spend_status,
            "daily_spend_usd": self.daily_spend_usd,
            "max_daily_spend_usd": self.max_daily_spend_usd,
            "projected_spend_usd": self.projected_spend_usd,
            "budget_remaining_usd": (None if remaining == float("inf") else remaining),
            "assignment_count": len(self.assignments),
            "deferred_count": len(self.deferred),
            "decisions": [d.as_dict() for d in self.decisions],
        }


# --------------------------------------------------------------------------
# Small coercion helpers (shared by the fail-closed `from_dict`s)
# --------------------------------------------------------------------------


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _non_negative_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return number if number >= 0 else default


def _non_negative_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if number >= 0 else default
