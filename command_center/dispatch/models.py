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
# The task's business path (project) has a tail-risk scenario whose priced
# expected cost of error (probability x impact) currently exceeds the
# scenario's configured limit. Checked per task, before eligibility/budget.
DEFER_TAIL_RISK = "tail_risk_limit_exceeded"

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
        DEFER_TAIL_RISK,
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
    DEFER_TAIL_RISK: (
        "This business path has a tail-risk scenario whose priced expected "
        "cost of error (probability x impact) exceeds its configured limit: "
        "dispatch is refused until the scenario is revised or the limit is "
        "raised, never assigned anyway."
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
class TailRiskScenario:
    """One named tail-risk scenario: a rare-but-costly failure mode for a
    single business path (a `project_config` project id), priced from two
    documented assumptions rather than measured — `probability` (the assumed
    fraction of autonomous operations on this path that trigger the failure)
    and `impact_usd` (the assumed cost if it does: incident response,
    regulatory/legal exposure, rework, reputational cost). Their product is
    `expected_cost_usd`, the tail-risk-adjusted price of operating on this
    path; `limit_usd` is the ceiling that price must stay under for dispatch
    to remain open. `0.0` `limit_usd` means "unset" (never blocks), matching
    every other limit in this policy.
    """

    id: str
    label: str
    business_path: str
    probability: float
    impact_usd: float
    assumptions: str
    limit_usd: float = 0.0

    @property
    def expected_cost_usd(self) -> float:
        return max(0.0, min(1.0, self.probability)) * max(0.0, self.impact_usd)

    def exceeds_limit(self) -> bool:
        return self.limit_usd > 0 and self.expected_cost_usd > self.limit_usd

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "business_path": self.business_path,
            "probability": self.probability,
            "impact_usd": self.impact_usd,
            "assumptions": self.assumptions,
            "limit_usd": self.limit_usd,
            "expected_cost_usd": self.expected_cost_usd,
        }

    @classmethod
    def from_dict(cls, scenario_id: str, data: object) -> "TailRiskScenario | None":
        """Fail closed at the entry level: an unparseable scenario is dropped
        entirely (never silently coerced into one that matches every path),
        so a malformed edit shrinks the registry rather than widening it."""
        if not isinstance(data, dict):
            return None
        return cls(
            id=scenario_id,
            label=str(data.get("label") or scenario_id),
            business_path=str(data.get("business_path") or ""),
            probability=_clamp01(data.get("probability")),
            impact_usd=_non_negative_float(data.get("impact_usd"), 0.0),
            assumptions=str(data.get("assumptions") or ""),
            limit_usd=_non_negative_float(data.get("limit_usd"), 0.0),
        )


# The top-5 tail-risk scenarios this system ships with by construction (the
# acceptance this module exists to satisfy): each names a business path
# (project id from `project_config.DISPLAY_NAMES`), the probability/impact
# assumptions used to price it, and the limit that blocks dispatch on that
# path once the priced cost breaches it. Numbers are deliberately headroomed
# under their limit — a *default* policy must stay open, not silently wall
# off a project — so tightening the limit or revising an assumption upward
# (e.g. after a real incident) is what trips the gate, never the baseline.
DEFAULT_TAIL_RISK_SCENARIOS: tuple[TailRiskScenario, ...] = (
    TailRiskScenario(
        id="aml-autoscore-false-negative",
        label="Autonomous AML risk-scoring change ships a silent false negative",
        business_path="AML",
        probability=0.01,
        impact_usd=250_000.0,
        assumptions=(
            "1-in-100 autonomous merges touching risk_store/rule_engine weaken "
            "a red-flag rule without a human catching it before the next "
            "scheduled review; impact is a single missed-alert compliance "
            "incident (regulatory response, backfill review, remediation)."
        ),
        limit_usd=5_000.0,
    ),
    TailRiskScenario(
        id="esf-destructive-migration",
        label="Autonomous destructive migration on the shared ESF platform",
        business_path="ESF",
        probability=0.02,
        impact_usd=40_000.0,
        assumptions=(
            "2% of autonomous ESF schema/migration changes cause a multi-day "
            "outage for the shared corporate platform; impact is incident "
            "response plus customer SLA credits."
        ),
        limit_usd=2_000.0,
    ),
    TailRiskScenario(
        id="business-billing-misprice",
        label="Autonomous change to billing/pricing logic misprices customers",
        business_path="BUSINESS",
        probability=0.015,
        impact_usd=60_000.0,
        assumptions=(
            "1.5% of autonomous BUSINESS-project pricing/billing changes ship "
            "a mispricing bug before the next release; impact is refunds, "
            "churn and support load from the affected billing cycle."
        ),
        limit_usd=2_500.0,
    ),
    TailRiskScenario(
        id="aicc-self-hosting-runaway-remediation",
        label="Self-hosting remediation loop breaks the AICC delivery pipeline",
        business_path="AICC",
        probability=0.03,
        impact_usd=15_000.0,
        assumptions=(
            "3% of autonomous changes to AICC's own orchestrator/dispatch code "
            "(higher change velocity than other paths) break CI/CD for every "
            "project it delivers; impact is recovery engineering time plus "
            "delivery downtime across all projects."
        ),
        limit_usd=1_500.0,
    ),
    TailRiskScenario(
        id="product-uncaught-regression",
        label="Autonomous change ships an uncaught customer-facing regression",
        business_path="PRODUCT",
        probability=0.02,
        impact_usd=30_000.0,
        assumptions=(
            "2% of autonomous PRODUCT changes ship a regression that isn't "
            "caught before the next release window; impact is support load, "
            "refund/credit exposure and brand damage from the affected "
            "release."
        ),
        limit_usd=2_000.0,
    ),
)


def _default_tail_risk_registry() -> dict[str, TailRiskScenario]:
    return {s.id: s for s in DEFAULT_TAIL_RISK_SCENARIOS}


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
    # scenario id -> TailRiskScenario. Empty/garbage falls back to the
    # top-5 default registry (see `_default_tail_risk_registry`), the same
    # "never boot with an unset baseline" treatment `priority_weights` gets.
    tail_risk_scenarios: dict[str, TailRiskScenario] = field(
        default_factory=_default_tail_risk_registry
    )
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

    def tail_risk_block(self, business_path: str | None) -> "TailRiskScenario | None":
        """The scenario blocking `business_path`, or None if it's clear.

        Deterministic (sorted by scenario id) so a business path matching
        more than one breaching scenario always reports the same one."""
        if not business_path:
            return None
        for scenario in sorted(self.tail_risk_scenarios.values(), key=lambda s: s.id):
            if scenario.business_path == business_path and scenario.exceeds_limit():
                return scenario
        return None

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
            "tail_risk_scenarios": {
                k: v.as_dict() for k, v in self.tail_risk_scenarios.items()
            },
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
        scenarios = {
            scenario.id: scenario
            for scenario_id, raw in _as_dict(data.get("tail_risk_scenarios")).items()
            if (scenario := TailRiskScenario.from_dict(str(scenario_id), raw))
            is not None
        } or _default_tail_risk_registry()
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
            tail_risk_scenarios=scenarios,
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
    # Set only when reason == DEFER_TAIL_RISK: which scenario blocked it.
    blocked_scenario_id: str | None = None

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
            "blocked_scenario_id": self.blocked_scenario_id,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class DispatchPlan:
    """The whole plan: one decision per task plus the budget arithmetic."""

    decisions: tuple[DispatchDecision, ...]
    kill_switch_engaged: bool
    # None exactly when `budget_unknown` is True: the trailing-24h spend could
    # not be read, so there is no real figure to report. A caller that reads
    # this field without checking `budget_unknown` must see "no data" (None),
    # never a fabricated `0.0` that reads as "nothing spent today".
    daily_spend_usd: float | None
    max_daily_spend_usd: float
    projected_spend_usd: float | None
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
    def budget_remaining_usd(self) -> float | None:
        if self.projected_spend_usd is None:
            return None
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
            "budget_remaining_usd": (
                None if remaining is None or remaining == float("inf") else remaining
            ),
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


def _clamp01(value: object) -> float:
    """A probability: coerced into `[0, 1]`; anything unparseable is `0.0`
    (fail closed toward *not* pricing in a risk that was never configured)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))
