"""Unit tests for the pure dispatch-selection engine (`dispatch.policy`).

Every acceptance property is asserted directly against `plan_dispatch` with no
database, no filesystem, no HTTP — the engine is pure by construction.
"""

from __future__ import annotations

from command_center.dispatch import models
from command_center.dispatch.models import (
    AgentLimit,
    DispatchPolicy,
    ExecutorProfile,
    QueuedTask,
)
from command_center.dispatch.policy import plan_dispatch


def _executor(
    eid: str, *, cost: float, is_local: bool = False, available: bool = True
) -> ExecutorProfile:
    return ExecutorProfile(
        id=eid,
        label=eid,
        kind="cli",
        is_local=is_local,
        available=available,
        cost_per_task_usd=cost,
    )


def _task(
    tid: str,
    *,
    priority: str = "Medium",
    project: str | None = "AICC",
    allowed: frozenset[str] | None = None,
    pinned: str | None = None,
    sla: str | None = None,
    created: str | None = None,
) -> QueuedTask:
    return QueuedTask(
        id=tid,
        project=project,
        priority=priority,
        allowed_executors=allowed,
        pinned_executor=pinned,
        sla_deadline=sla,
        created_at=created,
    )


def _plan(tasks, executors, policy, **ctx):
    ctx.setdefault("daily_spend_usd", 0.0)
    ctx.setdefault("max_daily_spend_usd", 0.0)
    ctx.setdefault("kill_switch_engaged", False)
    return plan_dispatch(tasks, executors, policy, **ctx)


# --------------------------------------------------------------------------
# Local preference (cost economy)
# --------------------------------------------------------------------------


def test_local_executor_is_preferred_even_when_a_cloud_executor_is_cheaper():
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [
        _executor("ollama", cost=0.5, is_local=True),
        _executor("claude_code", cost=0.1, is_local=False),
    ]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "ollama"


def test_prefer_local_off_selects_the_cheapest_executor():
    policy = DispatchPolicy(prefer_local=False, local_executor_ids=frozenset({"ollama"}))
    executors = [
        _executor("ollama", cost=0.5, is_local=True),
        _executor("claude_code", cost=0.1, is_local=False),
    ]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "claude_code"


def test_cheapest_local_wins_among_locals():
    policy = DispatchPolicy(
        prefer_local=True, local_executor_ids=frozenset({"ollama", "local_b"})
    )
    executors = [
        _executor("ollama", cost=0.9, is_local=True),
        _executor("local_b", cost=0.2, is_local=True),
        _executor("claude_code", cost=0.05, is_local=False),
    ]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "local_b"


# --------------------------------------------------------------------------
# Budget-cap enforcement — an over-budget assignment must be REFUSED
# --------------------------------------------------------------------------


def test_daily_budget_cap_refuses_an_over_budget_assignment():
    # Ceiling 1.0, already spent 0.8; the only executor costs 0.5 -> 1.3 > 1.0.
    policy = DispatchPolicy(cost_matrix={"claude_code": 0.5})
    executors = [_executor("claude_code", cost=0.5)]
    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        daily_spend_usd=0.8,
        max_daily_spend_usd=1.0,
    )

    assert plan.assignments == ()
    d = plan.decisions[0]
    assert d.assigned is False
    assert d.reason == models.DEFER_DAILY_BUDGET
    # Never force-run: projected spend is unchanged from the starting spend.
    assert plan.projected_spend_usd == 0.8


def test_budget_allows_what_fits_and_defers_the_rest_in_priority_order():
    # Ceiling 1.0. Each task costs 0.4 -> only two fit (0.8), the third defers.
    policy = DispatchPolicy(cost_matrix={"claude_code": 0.4})
    executors = [_executor("claude_code", cost=0.4)]
    tasks = [
        _task("low", priority="Low"),
        _task("crit", priority="Critical"),
        _task("high", priority="High"),
    ]
    plan = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)

    assigned = {d.task_id for d in plan.assignments}
    # The two highest-priority tasks are the ones that got the budget.
    assert assigned == {"crit", "high"}
    deferred = plan.deferred
    assert [d.task_id for d in deferred] == ["low"]
    assert deferred[0].reason == models.DEFER_DAILY_BUDGET
    assert plan.projected_spend_usd == 0.8


def test_zero_ceiling_means_no_budget_limit():
    policy = DispatchPolicy(cost_matrix={"claude_code": 5.0})
    executors = [_executor("claude_code", cost=5.0)]
    plan = _plan([_task("t1")], executors, policy, max_daily_spend_usd=0.0)

    assert plan.assignments[0].assigned_executor == "claude_code"


# --------------------------------------------------------------------------
# Unknown budget (cost data unavailable) blocks everything, like the kill
# switch — never a simulated spend figure a zero cap or free executor could
# silently absorb.
# --------------------------------------------------------------------------


def test_budget_unknown_defers_everything_even_with_zero_ceiling():
    # The exact configuration that used to fail OPEN: no cap configured (the
    # default) and a free local executor available.
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    tasks = [_task("t1", priority="Critical"), _task("t2", priority="High")]
    plan = _plan(tasks, executors, policy, max_daily_spend_usd=0.0, budget_unknown=True)

    assert plan.budget_unknown is True
    assert plan.assignments == ()
    assert all(d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions)


def test_budget_unknown_defers_everything_with_a_nonzero_ceiling_and_free_executor():
    # The other configuration that used to fail OPEN: a real cap is
    # configured, but the only eligible executor costs $0.0, so "assume the
    # ceiling is hit" (projected == max) never actually exceeds it.
    policy = DispatchPolicy(cost_matrix={"ollama": 0.0})
    executors = [_executor("ollama", cost=0.0)]
    plan = _plan(
        [_task("t1")], executors, policy, max_daily_spend_usd=5.0, budget_unknown=True
    )

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_COST_DATA_UNAVAILABLE


def test_kill_switch_takes_priority_over_budget_unknown_in_the_reason():
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.0)]
    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        kill_switch_engaged=True,
        budget_unknown=True,
    )

    assert plan.kill_switch_engaged is True
    assert plan.budget_unknown is True
    assert plan.decisions[0].reason == models.DEFER_KILL_SWITCH


# --------------------------------------------------------------------------
# Kill switch is respected — nothing is assigned while engaged
# --------------------------------------------------------------------------


def test_kill_switch_defers_everything_and_assigns_nothing():
    policy = DispatchPolicy()
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    tasks = [_task("t1", priority="Critical"), _task("t2", priority="High")]
    plan = _plan(tasks, executors, policy, kill_switch_engaged=True)

    assert plan.kill_switch_engaged is True
    assert plan.assignments == ()
    assert all(d.reason == models.DEFER_KILL_SWITCH for d in plan.decisions)
    # A free local executor is available and budget is unlimited, yet nothing
    # was assigned: the kill switch is checked before any assignment.
    assert plan.projected_spend_usd == plan.daily_spend_usd


# --------------------------------------------------------------------------
# SLA / priority ordering is never bypassed
# --------------------------------------------------------------------------


def test_priority_orders_scarce_capacity():
    # One free slot's worth of budget; the Critical task must take it.
    policy = DispatchPolicy(cost_matrix={"claude_code": 1.0})
    executors = [_executor("claude_code", cost=1.0)]
    tasks = [
        _task("a", priority="Low"),
        _task("b", priority="Medium"),
        _task("c", priority="Critical"),
    ]
    plan = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)

    assert [d.task_id for d in plan.assignments] == ["c"]


def test_sla_deadline_breaks_ties_within_same_priority():
    policy = DispatchPolicy(cost_matrix={"claude_code": 1.0})
    executors = [_executor("claude_code", cost=1.0)]
    tasks = [
        _task("later", priority="High", sla="2026-09-01T00:00:00"),
        _task("sooner", priority="High", sla="2026-08-15T00:00:00"),
    ]
    plan = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)

    assert [d.task_id for d in plan.assignments] == ["sooner"]


def test_tasks_without_sla_sort_after_those_with_one():
    policy = DispatchPolicy(cost_matrix={"claude_code": 1.0})
    executors = [_executor("claude_code", cost=1.0)]
    tasks = [
        _task("no_sla", priority="High", sla=None, created="2026-01-01T00:00:00"),
        _task("has_sla", priority="High", sla="2026-12-31T00:00:00"),
    ]
    plan = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)

    assert [d.task_id for d in plan.assignments] == ["has_sla"]


# --------------------------------------------------------------------------
# Per-agent and per-project guardrails
# --------------------------------------------------------------------------


def test_per_agent_spend_limit_is_enforced():
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6},
        per_agent_limits={"claude_code": AgentLimit(max_spend_usd=1.0)},
    )
    executors = [_executor("claude_code", cost=0.6)]
    # Two tasks at 0.6 each = 1.2 > the agent's 1.0 cap: only one fits.
    tasks = [_task("t1", priority="High"), _task("t2", priority="Medium")]
    plan = _plan(tasks, executors, policy)

    assert [d.task_id for d in plan.assignments] == ["t1"]
    deferred = plan.deferred
    assert deferred[0].reason == models.DEFER_AGENT_BUDGET


def test_per_agent_concurrency_limit_accounts_for_running_work():
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.0},
        per_agent_limits={"claude_code": AgentLimit(max_concurrent=1)},
    )
    executors = [_executor("claude_code", cost=0.0)]
    # One run already active -> the single concurrency slot is taken.
    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        active_by_executor={"claude_code": 1},
    )

    assert plan.assignments == ()
    assert plan.deferred[0].reason == models.DEFER_AGENT_CAPACITY


def test_per_project_spend_limit_is_enforced():
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6},
        per_project_limits={"AICC": 1.0},
    )
    executors = [_executor("claude_code", cost=0.6)]
    tasks = [
        _task("t1", priority="High", project="AICC"),
        _task("t2", priority="Medium", project="AICC"),
    ]
    plan = _plan(tasks, executors, policy)

    assert [d.task_id for d in plan.assignments] == ["t1"]
    assert plan.deferred[0].reason == models.DEFER_PROJECT_BUDGET


# --------------------------------------------------------------------------
# Eligibility / availability / pins — typed reasons, never force-run
# --------------------------------------------------------------------------


def test_no_permitted_executor_defers_with_typed_reason():
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.0)]
    plan = _plan([_task("t1", allowed=frozenset({"codex"}))], executors, policy)

    assert plan.assignments == ()
    assert plan.deferred[0].reason == models.DEFER_NO_ELIGIBLE_EXECUTOR


def test_permitted_but_unavailable_executor_defers_distinctly():
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.0, available=False)]
    plan = _plan([_task("t1", allowed=frozenset({"claude_code"}))], executors, policy)

    assert plan.deferred[0].reason == models.DEFER_NO_AVAILABLE_EXECUTOR


def test_hard_pin_restricts_to_the_pinned_executor():
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [
        _executor("ollama", cost=0.0, is_local=True),
        _executor("codex", cost=0.0),
    ]
    plan = _plan([_task("t1", pinned="codex")], executors, policy)

    assert plan.assignments[0].assigned_executor == "codex"


def test_plan_is_deterministic_for_identical_input():
    policy = DispatchPolicy(cost_matrix={"claude_code": 0.4})
    executors = [_executor("claude_code", cost=0.4)]
    tasks = [_task("b", priority="High"), _task("a", priority="High")]

    first = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)
    second = _plan(tasks, executors, policy, max_daily_spend_usd=1.0)

    assert [d.as_dict() for d in first.decisions] == [
        d.as_dict() for d in second.decisions
    ]


# --------------------------------------------------------------------------
# Leadership metrics (VOYN-AGT-REWARD): the best-standing agents win
# dispatch ties, unlock experimental zones and get roomier budgets.
# --------------------------------------------------------------------------


def test_elite_executor_wins_the_tie_over_a_cheaper_lower_tier_rival():
    # claude_code is cheaper, but codex has earned elite standing.
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.1, "codex": 0.5},
        leaderboard={"codex": 90.0, "claude_code": 10.0},
    )
    executors = [
        _executor("claude_code", cost=0.1),
        _executor("codex", cost=0.5),
    ]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "codex"


def test_equal_standing_falls_back_to_cost_ordering():
    policy = DispatchPolicy(cost_matrix={"claude_code": 0.1, "codex": 0.5})
    executors = [
        _executor("claude_code", cost=0.1),
        _executor("codex", cost=0.5),
    ]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "claude_code"


def test_experimental_executor_requires_the_configured_tier():
    policy = DispatchPolicy(
        cost_matrix={"codex": 0.0},
        leaderboard={"codex": 10.0},
        experimental_executor_ids=frozenset({"codex"}),
        experimental_min_tier="trusted",
    )
    executors = [_executor("codex", cost=0.0)]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments == ()
    assert plan.deferred[0].reason == models.DEFER_EXPERIMENTAL_TIER_REQUIRED


def test_experimental_executor_is_assignable_once_it_earns_the_tier():
    policy = DispatchPolicy(
        cost_matrix={"codex": 0.0},
        leaderboard={"codex": 75.0},
        experimental_executor_ids=frozenset({"codex"}),
        experimental_min_tier="trusted",
    )
    executors = [_executor("codex", cost=0.0)]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments[0].assigned_executor == "codex"


def test_experimental_tier_gate_applies_even_to_a_hard_pin():
    policy = DispatchPolicy(
        cost_matrix={"codex": 0.0},
        leaderboard={"codex": 10.0},
        experimental_executor_ids=frozenset({"codex"}),
        experimental_min_tier="trusted",
    )
    executors = [_executor("codex", cost=0.0)]
    plan = _plan([_task("t1", pinned="codex")], executors, policy)

    assert plan.assignments == ()
    assert plan.deferred[0].reason == models.DEFER_EXPERIMENTAL_TIER_REQUIRED


def test_elite_tier_widens_the_agents_own_spend_and_concurrency_limits():
    # Base cap is 1.0 with a 2.0x elite multiplier -> effective cap 2.0, so
    # both 0.6 tasks fit where a non-elite agent would only fit one.
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6},
        leaderboard={"claude_code": 95.0},
        per_agent_limits={"claude_code": AgentLimit(max_spend_usd=1.0)},
    )
    executors = [_executor("claude_code", cost=0.6)]
    tasks = [_task("t1", priority="High"), _task("t2", priority="Medium")]
    plan = _plan(tasks, executors, policy)

    assert {d.task_id for d in plan.assignments} == {"t1", "t2"}


def test_standard_tier_keeps_the_configured_limit_unchanged():
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6},
        per_agent_limits={"claude_code": AgentLimit(max_spend_usd=1.0)},
    )
    executors = [_executor("claude_code", cost=0.6)]
    tasks = [_task("t1", priority="High"), _task("t2", priority="Medium")]
    plan = _plan(tasks, executors, policy)

    assert [d.task_id for d in plan.assignments] == ["t1"]
    assert plan.deferred[0].reason == models.DEFER_AGENT_BUDGET


def test_tier_multiplier_never_widens_the_global_daily_ceiling():
    # Elite standing widens the *agent's own* cap, but the global daily
    # ceiling (guarantee 2) must still hold regardless of tier.
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6},
        leaderboard={"claude_code": 100.0},
    )
    executors = [_executor("claude_code", cost=0.6)]
    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        daily_spend_usd=0.5,
        max_daily_spend_usd=1.0,
    )

    assert plan.assignments == ()
    assert plan.deferred[0].reason == models.DEFER_DAILY_BUDGET
