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
DEFER_COST_DATA_UNAVAILABLE = "cost_data_unavailable"
DEFER_DAILY_BUDGET = "daily_budget_exhausted"
DEFER_AGENT_BUDGET = "agent_budget_exceeded"
DEFER_PROJECT_BUDGET = "project_budget_exceeded"
DEFER_AGENT_CAPACITY = "agent_capacity_reached"
DEFER_NO_ELIGIBLE_EXECUTOR = "no_eligible_executor"
DEFER_NO_AVAILABLE_EXECUTOR = "no_available_executor"
# The executor is flagged as an experimental zone and this agent's leadership
# standing has not yet earned access to it (VOYN-AGT-REWARD).
DEFER_EXPERIMENTAL_TIER_REQUIRED = "experimental_tier_required"

DEFER_REASONS = frozenset(
    {
        DEFER_KILL_SWITCH,
        DEFER_COST_DATA_UNAVAILABLE,
        DEFER_DAILY_BUDGET,
        DEFER_AGENT_BUDGET,
        DEFER_PROJECT_BUDGET,
        DEFER_AGENT_CAPACITY,
        DEFER_NO_ELIGIBLE_EXECUTOR,
        DEFER_NO_AVAILABLE_EXECUTOR,
        DEFER_EXPERIMENTAL_TIER_REQUIRED,
    }
)

# Human-readable one-liners, kept next to the codes so both the API and the
# operator UI render the same explanation.
REASON_EXPLANATIONS: dict[str, str] = {
    ASSIGNED: "Assigned to the cheapest eligible executor within budget.",
    DEFER_KILL_SWITCH: (
        "Kill switch engaged (master switch off): no automatic dispatch."
    ),
    DEFER_COST_DATA_UNAVAILABLE: (
        "Trailing-24h spend could not be read: dispatch is refused until cost "
        "data is available again, so budget guardrails can never be silently "
        "bypassed by a database outage."
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
    DEFER_EXPERIMENTAL_TIER_REQUIRED: (
        "This executor is an experimental zone: it takes work only once the "
        "agent's leaderboard standing clears the configured minimum tier."
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
# Leadership metrics (VOYN-AGT-REWARD): the best-performing agents win
# dispatch ties, unlock experimental executor zones, and get roomier budgets.
# The score itself (0-100) is policy-configured, like the cost matrix — this
# layer only decides what a given score *does*, not how it was computed.
# --------------------------------------------------------------------------

# Tier name -> minimum leaderboard score (0-100) required to hold that tier.
# Also doubles as the tier's rank: a higher minimum is a better tier.
DEFAULT_TIER_THRESHOLDS: dict[str, float] = {
    "elite": 85.0,
    "trusted": 60.0,
    "standard": 0.0,
}

# Tier -> extra weight added when ranking executors for the *same* task, so a
# better-standing agent wins the tie over a merely-cheaper one.
DEFAULT_TIER_PRIORITY_BONUS: dict[str, int] = {
    "elite": 2,
    "trusted": 1,
    "standard": 0,
}

# Tier -> multiplier applied to that agent's own `AgentLimit` (never below
# 1.0 — this rewards, it never shrinks a configured limit).
DEFAULT_TIER_BUDGET_MULTIPLIER: dict[str, float] = {
    "elite": 2.0,
    "trusted": 1.5,
    "standard": 1.0,
}

# The tier an executor must hold to be assigned an "experimental zone" task.
DEFAULT_EXPERIMENTAL_MIN_TIER = "trusted"


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
    # Executor id -> leaderboard score (0-100). Config-driven, like the cost
    # matrix: this policy layer decides what the score unlocks, not how it is
    # computed.
    leaderboard: dict[str, float] = field(default_factory=dict)
    tier_thresholds: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TIER_THRESHOLDS)
    )
    tier_priority_bonus: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_TIER_PRIORITY_BONUS)
    )
    tier_budget_multiplier: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TIER_BUDGET_MULTIPLIER)
    )
    # Executors flagged as an "experimental zone": only reachable by an agent
    # whose tier clears `experimental_min_tier`.
    experimental_executor_ids: frozenset[str] = field(default_factory=frozenset)
    experimental_min_tier: str = DEFAULT_EXPERIMENTAL_MIN_TIER
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

    def _effective_tier_thresholds(self) -> dict[str, float]:
        return self.tier_thresholds if self.tier_thresholds else dict(
            DEFAULT_TIER_THRESHOLDS
        )

    def score_for(self, executor_id: str) -> float:
        value = self.leaderboard.get(executor_id)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, min(100.0, float(value)))
        return 0.0

    def tier_for(self, executor_id: str) -> str:
        """The best tier whose minimum score `executor_id`'s standing clears."""
        score = self.score_for(executor_id)
        ordered = sorted(
            self._effective_tier_thresholds().items(),
            key=lambda item: item[1],
            reverse=True,
        )
        for name, minimum in ordered:
            if score >= minimum:
                return name
        return ordered[-1][0]

    def _tier_rank(self, tier: str) -> float:
        """A tier's rank, higher is better. An unrecognized tier name is
        fail-closed to the strictest configured bar rather than 0 — a typo in
        `experimental_min_tier` must never silently admit every agent."""
        thresholds = self._effective_tier_thresholds()
        if tier in thresholds:
            return thresholds[tier]
        if tier in DEFAULT_TIER_THRESHOLDS:
            return DEFAULT_TIER_THRESHOLDS[tier]
        return max(thresholds.values())

    def priority_bonus_for(self, executor_id: str) -> int:
        return self.tier_priority_bonus.get(self.tier_for(executor_id), 0)

    def budget_multiplier_for(self, executor_id: str) -> float:
        return max(
            1.0, self.tier_budget_multiplier.get(self.tier_for(executor_id), 1.0)
        )

    def meets_experimental_bar(self, executor_id: str) -> bool:
        return self._tier_rank(self.tier_for(executor_id)) >= self._tier_rank(
            self.experimental_min_tier
        )

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
            "leaderboard": dict(self.leaderboard),
            "tier_thresholds": dict(self.tier_thresholds),
            "tier_priority_bonus": dict(self.tier_priority_bonus),
            "tier_budget_multiplier": dict(self.tier_budget_multiplier),
            "experimental_executor_ids": sorted(self.experimental_executor_ids),
            "experimental_min_tier": self.experimental_min_tier,
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
        leaderboard = {
            str(k): max(0.0, min(100.0, float(v)))
            for k, v in _as_dict(data.get("leaderboard")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        tier_thresholds = {
            str(k): max(0.0, float(v))
            for k, v in _as_dict(data.get("tier_thresholds")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        } or dict(DEFAULT_TIER_THRESHOLDS)
        tier_priority_bonus = {
            str(k): int(v)
            for k, v in _as_dict(data.get("tier_priority_bonus")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        } or dict(DEFAULT_TIER_PRIORITY_BONUS)
        tier_budget_multiplier = {
            str(k): max(1.0, float(v))
            for k, v in _as_dict(data.get("tier_budget_multiplier")).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        } or dict(DEFAULT_TIER_BUDGET_MULTIPLIER)
        experimental_ids = data.get("experimental_executor_ids")
        experimental_set = (
            frozenset(str(x) for x in experimental_ids)
            if isinstance(experimental_ids, (list, tuple, set, frozenset))
            else frozenset()
        )
        experimental_min_tier = data.get("experimental_min_tier")
        if not isinstance(experimental_min_tier, str) or not experimental_min_tier:
            experimental_min_tier = DEFAULT_EXPERIMENTAL_MIN_TIER
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
            leaderboard=leaderboard,
            tier_thresholds=tier_thresholds,
            tier_priority_bonus=tier_priority_bonus,
            tier_budget_multiplier=tier_budget_multiplier,
            experimental_executor_ids=experimental_set,
            experimental_min_tier=experimental_min_tier,
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
    # True when the trailing-24h spend could not be read (e.g. a DB outage):
    # dispatch is refused wholesale rather than guessing a spend figure that a
    # zero/unset daily cap or a free executor could silently sail past.
    budget_unknown: bool = False

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
            "budget_unknown": self.budget_unknown,
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
