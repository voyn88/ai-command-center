"""Pure dataclasses and typed reason codes for the dispatch policy layer.

No I/O here — everything in this module is a value object, so the policy
engine that consumes them (`command_center.dispatch.policy`) stays pure and
hermetically testable. Every "why was this task not assigned" answer is a
member of `DeferReason`, never a free-form string, so a caller can branch on
the reason instead of parsing prose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Typed reason codes
# --------------------------------------------------------------------------

# A task WAS assigned to an executor.
ASSIGNED = "assigned"

# Deferred (stays queued). Each is a *typed* reason, never force-run.
DEFER_KILL_SWITCH = "kill_switch_engaged"
DEFER_COST_DATA_UNAVAILABLE = "cost_data_unavailable"
DEFER_CAPACITY_DATA_UNAVAILABLE = "capacity_data_unavailable"
DEFER_POLICY_DATA_UNAVAILABLE = "policy_data_unavailable"
DEFER_DAILY_BUDGET = "daily_budget_exhausted"
DEFER_AGENT_BUDGET = "agent_budget_exceeded"
DEFER_PROJECT_BUDGET = "project_budget_exceeded"
DEFER_AGENT_CAPACITY = "agent_capacity_reached"
DEFER_NO_ELIGIBLE_EXECUTOR = "no_eligible_executor"
DEFER_NO_AVAILABLE_EXECUTOR = "no_available_executor"

DEFER_REASONS = frozenset(
    {
        DEFER_KILL_SWITCH,
        DEFER_COST_DATA_UNAVAILABLE,
        DEFER_CAPACITY_DATA_UNAVAILABLE,
        DEFER_POLICY_DATA_UNAVAILABLE,
        DEFER_DAILY_BUDGET,
        DEFER_AGENT_BUDGET,
        DEFER_PROJECT_BUDGET,
        DEFER_AGENT_CAPACITY,
        DEFER_NO_ELIGIBLE_EXECUTOR,
        DEFER_NO_AVAILABLE_EXECUTOR,
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
    DEFER_CAPACITY_DATA_UNAVAILABLE: (
        "In-flight run counts could not be read: dispatch is refused until the "
        "runtime store is readable again, so per-agent concurrency limits can "
        "never be silently bypassed by a database outage."
    ),
    DEFER_POLICY_DATA_UNAVAILABLE: (
        "The dispatch policy could not be read: dispatch is refused until it "
        "is readable again, because the per-agent and per-project limits it "
        "carries are expressed by their presence, so falling back to the "
        "defaults would silently drop every configured guardrail."
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
    """Per-agent guardrails. `0`/`0.0` means "unset" (no limit).

    Which is exactly why a *corrupt* limit must not decay into `0`: "unset"
    is the most permissive value this type can hold, so silently substituting
    it turns a guardrail an operator configured into no guardrail at all. A
    configured-but-unusable spend ceiling is therefore carried as NaN (see
    :func:`_usable_amount`) and blocks the executor in the engine, and a
    configured-but-unusable concurrency ceiling falls back to the tightest
    enforceable limit rather than to none.
    """

    max_concurrent: int = 0
    max_spend_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            # `_json_safe` because a NaN ceiling must survive as legal JSON;
            # `from_dict` reads that `null` back as unusable, not as absent.
            "max_spend_usd": _json_safe(self.max_spend_usd),
        }

    @classmethod
    def from_dict(cls, data: object) -> "AgentLimit":
        if not isinstance(data, dict):
            return cls()
        return cls(
            max_concurrent=_concurrency_limit(data, "max_concurrent"),
            max_spend_usd=_spend_limit(data, "max_spend_usd"),
        )


@dataclass(frozen=True)
class DispatchPolicy:
    """The config-driven dispatch policy (like the advisor's AutoRule).

    Persisted as `data/dispatch_policy.json`. Everything is fail-closed: an
    unparseable field falls back to the safe default rather than widening a
    budget or disabling a guardrail.

    For the *money* fields that rule needs stating precisely, because their
    safe default is not their neutral one. A price and a ceiling widen in
    opposite directions — the permissive price is `0.0` (free) and the
    permissive ceiling is `0.0` (unset) — so "fall back to zero" would be the
    fail-**open** answer for both. The single rule applied here instead:

        a configured amount that is not usable money (non-finite, or
        negative) is preserved as NaN, never replaced by a number,

    so it can never read as free and never as unlimited. NaN is the right
    carrier because every ordering comparison against it is False, which makes
    it useless as a budget and therefore impossible to accidentally satisfy;
    the engine spots it (`math.isfinite`) and blocks whatever the amount
    governs. `as_dict` degrades it to JSON `null` — bare `NaN` is not legal
    JSON — and `from_dict` reads `null` back as unusable, so the refusal
    survives a policy round-trip instead of being quietly cleared the next
    time an unrelated field is edited.
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
        """The per-task price of `executor_id`: its cost-matrix entry, or
        `default_cost_usd` when the matrix names no price for it.

        An entry that *is* named but is not usable money resolves to NaN, not
        to `0.0`. `max(0.0, value)` used to be the normalisation here and it
        was the fail-open: `max(0.0, nan)` is `0.0`, so a single corrupt price
        made its executor read as **free**, and a free executor satisfies the
        daily, per-agent and per-project ceilings simultaneously no matter how
        much real money it spends — the same unbounded dispatch this ticket
        measured, arrived at from the policy file instead of the defaults.

        Re-normalised here and not only in `from_dict` because a
        `DispatchPolicy` is also constructed directly (in-process callers and
        every engine test), and the guarantee belongs to the accessor the
        engine actually calls.
        """
        value = self.cost_matrix.get(executor_id, _MISSING)
        if value is _MISSING:
            return self.default_cost_usd
        amount = _configured_amount(value)
        return self.default_cost_usd if amount is None else amount

    def priority_weight(self, priority: str) -> int:
        return self.priority_weights.get(priority, 0)

    def is_local(self, executor_id: str) -> bool:
        return executor_id in self.local_executor_ids

    def as_dict(self) -> dict:
        return {
            "prefer_local": self.prefer_local,
            # `_json_safe` on both money maps: an unusable amount is carried as
            # NaN, which `json.dump` would emit as a bare `NaN` token that no
            # RFC 8259 parser accepts. It degrades to `null`, which `from_dict`
            # reads back as unusable — so the round-trip preserves the refusal
            # rather than the unrepresentable number.
            "cost_matrix": {k: _json_safe(v) for k, v in self.cost_matrix.items()},
            "default_cost_usd": self.default_cost_usd,
            "per_agent_limits": {
                k: v.as_dict() for k, v in self.per_agent_limits.items()
            },
            "per_project_limits": {
                k: _json_safe(v) for k, v in self.per_project_limits.items()
            },
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
        cost_matrix = _amount_map(data.get("cost_matrix"))
        per_agent = {
            str(k): AgentLimit.from_dict(v)
            for k, v in _as_dict(data.get("per_agent_limits")).items()
        }
        per_project = _amount_map(data.get("per_project_limits"))
        # The finite guard is what makes this `from_dict` total as documented:
        # `int(float("nan"))` raises `ValueError` and `int(float("inf"))`
        # raises `OverflowError`, so a corrupt weight used to take down every
        # reader of the policy file — including `GET /api/v1/dispatch/policy`.
        # A weight is an ordering hint, not a guardrail, so an unusable one is
        # simply dropped.
        weights = {
            str(k): int(v)
            for k, v in _as_dict(data.get("priority_weights")).items()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
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
    # True when the trailing-24h spend could not be read (e.g. a DB outage):
    # dispatch is refused wholesale rather than guessing a spend figure that a
    # zero/unset daily cap or a free executor could silently sail past.
    budget_unknown: bool = False
    # True when the in-flight run counts could not be read. Same fail-closed
    # posture, for the same reason: an *empty* count map does not under-report
    # capacity conservatively, it under-reports the work already running, which
    # lets a plan assign on top of runs it cannot see.
    capacity_unknown: bool = False
    # True when the dispatch policy itself could not be read. Same posture, and
    # the sharpest of the three: the defaults it would otherwise fall back to
    # carry *empty* limit maps, and empty reads as "no per-agent concurrency
    # limit, no per-agent spend limit, no project ceiling" — so an unreadable
    # policy does not weaken the guardrails, it removes them.
    policy_unknown: bool = False

    @property
    def assignments(self) -> tuple[DispatchDecision, ...]:
        return tuple(d for d in self.decisions if d.assigned)

    @property
    def deferred(self) -> tuple[DispatchDecision, ...]:
        return tuple(d for d in self.decisions if not d.assigned)

    @property
    def budget_remaining_usd(self) -> float:
        if not math.isfinite(self.max_daily_spend_usd):
            # Not "unlimited" — unknowable. `inf` here would render as a null
            # remaining, i.e. exactly how an unset ceiling reads, so a corrupt
            # ceiling would be indistinguishable from a deliberately absent one.
            return float("nan")
        if self.max_daily_spend_usd <= 0:
            return float("inf")
        return self.max_daily_spend_usd - self.projected_spend_usd

    def as_dict(self) -> dict:
        remaining = self.budget_remaining_usd
        return {
            "kill_switch_engaged": self.kill_switch_engaged,
            "budget_unknown": self.budget_unknown,
            "capacity_unknown": self.capacity_unknown,
            "policy_unknown": self.policy_unknown,
            "daily_spend_usd": _json_safe(self.daily_spend_usd),
            "max_daily_spend_usd": _json_safe(self.max_daily_spend_usd),
            "projected_spend_usd": _json_safe(self.projected_spend_usd),
            "budget_remaining_usd": (
                None if remaining == float("inf") else _json_safe(remaining)
            ),
            "assignment_count": len(self.assignments),
            "deferred_count": len(self.deferred),
            "decisions": [d.as_dict() for d in self.decisions],
        }


# --------------------------------------------------------------------------
# Small coercion helpers (shared by the fail-closed `from_dict`s)
# --------------------------------------------------------------------------


def _json_safe(value: float) -> float | None:
    """`None` for a non-finite amount, the number otherwise.

    Python's `json` emits bare `NaN`/`Infinity` for these, which RFC 8259 does
    not allow — `JSON.parse` rejects it outright. A plan that reports a corrupt
    spend figure must still be *readable*, because the reason it is refusing is
    in the same response; `budget_unknown` is what carries the meaning, so the
    unrepresentable number degrades to null rather than to a parse error.
    """
    return value if math.isfinite(value) else None


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# A key that is simply absent from the policy, told apart from one that is
# present and holds `null`. The distinction is the whole point of the money
# round-trip: absent means "no ceiling configured", `null` is what `as_dict`
# writes for a ceiling that was configured and is unusable.
_MISSING = object()

# The fallback for a configured-but-unusable per-agent concurrency ceiling.
# `0` is unavailable as a fallback here because it means "no limit"; `1` is the
# tightest limit that is still enforceable, which is the conservative reading
# of "a limit was configured and we cannot tell what it was".
_UNUSABLE_CONCURRENCY = 1


def _usable_amount(value: float) -> float:
    """`value` when it is a usable amount of money, NaN otherwise.

    "Usable" is the rule `task_pipeline.daily_spend_usd` already applies to a
    provider-reported cost: finite and not negative. Everything else — NaN,
    ±inf, a negative price or ceiling — is money this system cannot compare,
    and the one thing it must never become is a plausible number.
    """
    return value if math.isfinite(value) and value >= 0 else float("nan")


def _configured_amount(value: object) -> float | None:
    """The amount a *present* policy entry configures: the number when it is
    usable money, NaN when it is present but unusable, and `None` when the
    entry is too wrongly-typed to be either — a string or a bool, which the
    caller drops to the field's default exactly as it always has.

    JSON `null` is deliberately not in that last group. It is precisely what
    `as_dict` writes for a NaN, so reading it back as *unusable* is what makes
    a refusal survive a policy round-trip instead of being cleared the next
    time an unrelated field is edited.
    """
    if value is None:
        return float("nan")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _usable_amount(float(value))


def _amount_map(data: object) -> dict[str, float]:
    """Coerce a policy money map (prices, per-project ceilings).

    Keeps every entry that names an amount — usable or not — because dropping
    an unusable one falls through to the field's permissive default: the
    default *price* for a cost matrix (re-pricing an executor whose configured
    price is corrupt) and "no ceiling" for a limits map.
    """
    coerced: dict[str, float] = {}
    for key, value in _as_dict(data).items():
        amount = _configured_amount(value)
        if amount is not None:
            coerced[str(key)] = amount
    return coerced


def _spend_limit(data: dict, key: str) -> float:
    """A per-agent spend ceiling: `0.0` (unset) when the key is absent, the
    amount when it is usable, NaN when it is present but unusable."""
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return 0.0
    amount = _configured_amount(value)
    return 0.0 if amount is None else amount


def _concurrency_limit(data: dict, key: str) -> int:
    """A per-agent concurrency ceiling: `0` (unset) when the key is absent.

    Unlike the money fields there is no NaN to carry — the field is an `int` —
    so an unusable value falls back to `_UNUSABLE_CONCURRENCY` instead. The
    finite check has to come before `int()`, which raises on NaN and ±inf; that
    raise is the reason a corrupt policy file could make every reader of it
    fail rather than fall back.
    """
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _UNUSABLE_CONCURRENCY
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return _UNUSABLE_CONCURRENCY
    return int(number)


def _non_negative_float(value: object, default: float) -> float:
    """A finite, non-negative float, or `default`.

    Used only for `default_cost_usd`, the fallback *price* — where falling back
    to the documented default is right, because that price is what an executor
    absent from the cost matrix already gets, and it is a priced fallback
    rather than a free one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return default
    return number
