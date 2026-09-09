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


# --------------------------------------------------------------------------
# Budget arithmetic that cannot be performed. A NaN does not *fail* the
# ceiling comparisons, it *satisfies* all of them, so it is the one input that
# silently inverts the engine's "budget is never exceeded" guarantee.
# --------------------------------------------------------------------------


def test_a_non_finite_spend_total_blocks_everything():
    """The measured fail-open, at engine level: a NaN trailing-24h spend used
    to sail past a real ceiling for every task, because `NaN + cost > ceiling`
    is False. It must engage the cost-data gate instead."""
    policy = DispatchPolicy(cost_matrix={"claude_code": 50.0})
    executors = [_executor("claude_code", cost=50.0)]
    tasks = [_task("t1"), _task("t2"), _task("t3")]

    for bad in (float("nan"), float("inf"), float("-inf")):
        plan = _plan(
            tasks, executors, policy, daily_spend_usd=bad, max_daily_spend_usd=5.0
        )
        assert plan.budget_unknown is True, bad
        assert plan.assignments == (), bad
        assert all(
            d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions
        ), bad


def test_a_non_finite_ceiling_blocks_everything():
    """A corrupt ceiling is not an absent one. `max_daily_spend_usd` is only
    enforced when `> 0`, which is False for NaN — so a NaN ceiling would read
    as "no cap configured" and buy unlimited spend."""
    policy = DispatchPolicy(cost_matrix={"claude_code": 50.0})
    executors = [_executor("claude_code", cost=50.0)]

    for bad in (float("nan"), float("inf"), float("-inf")):
        plan = _plan(
            [_task("t1")],
            executors,
            policy,
            daily_spend_usd=0.0,
            max_daily_spend_usd=bad,
        )
        assert plan.budget_unknown is True, bad
        assert plan.assignments == (), bad


def test_a_non_finite_per_task_cost_blocks_only_its_own_executor():
    """A cost that cannot be compared cannot be shown to fit, so the executor
    carrying it is blocked — but the gate is per-executor, not a whole-plan
    refusal, so a healthy alternative is still assignable."""
    policy = DispatchPolicy(prefer_local=False)
    executors = [
        _executor("broken", cost=float("nan")),
        _executor("claude_code", cost=1.0),
    ]
    plan = _plan(
        [_task("t1")], executors, policy, daily_spend_usd=0.0, max_daily_spend_usd=10.0
    )

    assert plan.assignments[0].assigned_executor == "claude_code"


def test_a_non_finite_cost_on_the_only_executor_defers_rather_than_assigns():
    policy = DispatchPolicy(prefer_local=False)
    executors = [_executor("broken", cost=float("nan"))]
    plan = _plan(
        [_task("t1")], executors, policy, daily_spend_usd=0.0, max_daily_spend_usd=10.0
    )

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_DAILY_BUDGET


def test_a_non_finite_cost_is_blocked_even_with_no_ceiling_configured():
    """The `max_daily_spend_usd > 0` guard means an unset ceiling skips the
    comparison entirely, so the finite-cost check must not live behind it."""
    policy = DispatchPolicy(prefer_local=False)
    executors = [_executor("broken", cost=float("inf"))]
    plan = _plan(
        [_task("t1")], executors, policy, daily_spend_usd=0.0, max_daily_spend_usd=0.0
    )

    assert plan.assignments == ()


def test_a_gated_plan_serializes_as_valid_json():
    """A refusal has to stay readable: `json` emits bare `NaN`, which RFC 8259
    forbids and `JSON.parse` rejects, so a corrupt spend figure would turn the
    plan endpoint's 200 into an unparseable body — hiding the very reason it
    is refusing."""
    import json

    policy = DispatchPolicy(cost_matrix={"claude_code": 50.0})
    executors = [_executor("claude_code", cost=50.0)]
    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        daily_spend_usd=float("nan"),
        max_daily_spend_usd=float("nan"),
    )

    body = json.dumps(plan.as_dict(), allow_nan=False)  # raises if NaN leaked
    parsed = json.loads(body)
    assert parsed["budget_unknown"] is True
    assert parsed["daily_spend_usd"] is None
    assert parsed["max_daily_spend_usd"] is None
    assert parsed["budget_remaining_usd"] is None


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


def test_capacity_unknown_defers_everything_even_when_budget_allows():
    # In-flight run counts unreadable: an *empty* count map would say "nothing
    # is running" and let the per-agent concurrency limit be spent all over
    # again on top of runs nobody can see, so the gate blocks instead.
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    plan = _plan(
        [_task("t1"), _task("t2")], executors, policy, capacity_unknown=True
    )

    assert plan.capacity_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_CAPACITY_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_an_empty_active_map_is_not_a_substitute_for_unknown_capacity():
    # The regression this gate exists for, shown as a contrast. With a
    # per-agent limit of 1 and one run genuinely in flight, the truthful count
    # map defers. Passing `{}` — exactly what the old swallow-into-an-empty-map
    # read returned on an unreadable store — assigns instead. That is the
    # effective concurrency limit being *raised* by a failed read, which is why
    # the read must not degrade silently into "nothing is running".
    policy = DispatchPolicy(
        prefer_local=True,
        local_executor_ids=frozenset({"ollama"}),
        per_agent_limits={"ollama": AgentLimit(max_concurrent=1, max_spend_usd=0.0)},
    )
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    tasks = [_task("t1")]

    truthful = _plan(tasks, executors, policy, active_by_executor={"ollama": 1})
    assert truthful.assignments == ()
    assert truthful.decisions[0].reason == models.DEFER_AGENT_CAPACITY

    pretend_idle = _plan(tasks, executors, policy, active_by_executor={})
    assert len(pretend_idle.assignments) == 1  # the fail-open, demonstrated

    # With the gate engaged instead, the same unreadable store defers.
    gated = _plan(
        tasks, executors, policy, active_by_executor={}, capacity_unknown=True
    )
    assert gated.assignments == ()
    assert gated.decisions[0].reason == models.DEFER_CAPACITY_DATA_UNAVAILABLE


def test_budget_unknown_takes_priority_over_capacity_unknown_in_the_reason():
    policy = DispatchPolicy()
    plan = _plan(
        [_task("t1")],
        [_executor("claude_code", cost=0.0)],
        policy,
        budget_unknown=True,
        capacity_unknown=True,
    )

    assert plan.budget_unknown is True
    assert plan.capacity_unknown is True
    assert plan.decisions[0].reason == models.DEFER_COST_DATA_UNAVAILABLE


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
# A guardrail that cannot be evaluated blocks what it governs. Both permissive
# readings in this engine are spelled `0.0` — a zero price is *free*, a zero
# ceiling is *unset* — so corruption that decays to zero disables the budget
# guarantee from whichever end it lands on.
# --------------------------------------------------------------------------


def test_a_corrupt_price_in_the_policy_cannot_buy_an_unbounded_plan():
    """The reported defect, reproduced from the policy file rather than from
    the defaults, and end to end through `cost_for`.

    A NaN price used to be normalised by `max(0.0, value)` into `0.0`, which
    is not a degraded price but the strongest one available: free. Every
    ceiling — daily, per-agent, per-project — is satisfied by it forever, so
    one corrupt cost-matrix entry assigned the entire queue against a ceiling
    that was configured, non-zero and perfectly readable.
    """
    policy = DispatchPolicy.from_dict(
        {"prefer_local": False, "cost_matrix": {"claude_code": float("nan")}}
    )
    executors = [_executor("claude_code", cost=policy.cost_for("claude_code"))]
    tasks = [_task(f"t{i}", priority="High") for i in range(5)]

    plan = _plan(tasks, executors, policy, max_daily_spend_usd=5.0)

    assert plan.assignments == ()
    assert all(d.reason == models.DEFER_DAILY_BUDGET for d in plan.decisions)


def test_a_corrupt_per_project_ceiling_defers_that_project():
    """`project_cap > 0` is False for NaN and `spent + cost > inf` is False
    for `inf`, so an unusable per-project ceiling read as "no ceiling" from
    either direction."""
    for bad in (float("nan"), float("inf")):
        policy = DispatchPolicy(
            prefer_local=False,
            cost_matrix={"claude_code": 1.0},
            per_project_limits={"AICC": bad},
        )
        executors = [_executor("claude_code", cost=1.0)]
        plan = _plan([_task("t1", project="AICC")], executors, policy)

        assert plan.assignments == (), bad
        assert plan.decisions[0].reason == models.DEFER_PROJECT_BUDGET, bad


def test_a_corrupt_per_project_ceiling_does_not_block_other_projects():
    """The refusal is scoped to what the corrupt ceiling governs — it is a
    per-project limit, not a whole-plan gate like `budget_unknown`."""
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"claude_code": 1.0},
        per_project_limits={"AICC": float("nan")},
    )
    executors = [_executor("claude_code", cost=1.0)]
    plan = _plan(
        [_task("t1", project="AICC"), _task("t2", project="AIOS")], executors, policy
    )

    assigned = {d.task_id for d in plan.assignments}
    assert assigned == {"t2"}


def test_a_corrupt_per_agent_spend_ceiling_defers_that_executor():
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"claude_code": 1.0},
        per_agent_limits={"claude_code": AgentLimit(max_spend_usd=float("inf"))},
    )
    executors = [_executor("claude_code", cost=1.0)]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_AGENT_BUDGET


def test_a_negative_per_agent_concurrency_ceiling_is_not_read_as_unset():
    """`max_concurrent > 0` skips a negative value into "no limit". Reachable
    only by constructing the policy directly — which in-process callers and
    every test here do — which is exactly why the engine re-checks instead of
    trusting `from_dict` to have normalised."""
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"claude_code": 1.0},
        per_agent_limits={"claude_code": AgentLimit(max_concurrent=-1)},
    )
    executors = [_executor("claude_code", cost=1.0)]
    plan = _plan([_task("t1")], executors, policy)

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_AGENT_CAPACITY


def test_a_negative_price_cannot_refund_the_projected_total():
    """A negative cost does not merely under-report: it *lowers* the running
    projection, so each assignment would buy headroom for the next one."""
    policy = DispatchPolicy(prefer_local=False)
    executors = [_executor("claude_code", cost=-10.0)]
    plan = _plan(
        [_task("t1"), _task("t2")], executors, policy, max_daily_spend_usd=5.0
    )

    assert plan.assignments == ()
    assert plan.projected_spend_usd == 0.0


def test_well_formed_ceilings_still_admit_and_still_bind():
    """Control for all of the above: the guards reject only unusable values,
    they have not turned every configured ceiling into a refusal."""
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"claude_code": 1.0},
        per_project_limits={"AICC": 1.5},
        per_agent_limits={
            "claude_code": AgentLimit(max_concurrent=5, max_spend_usd=2.0)
        },
    )
    executors = [_executor("claude_code", cost=1.0)]
    plan = _plan(
        [_task("t1", project="AICC"), _task("t2", project="AICC")],
        executors,
        policy,
        max_daily_spend_usd=10.0,
    )

    # The first fits under the $1.50 project cap; the second would take it to
    # $2.00 and is deferred for that reason, not refused wholesale.
    assert [d.task_id for d in plan.assignments] == ["t1"]
    assert plan.decisions[1].reason == models.DEFER_PROJECT_BUDGET


def test_a_negative_spend_figure_or_ceiling_engages_the_cost_data_gate():
    """`max_daily_spend_usd` is only enforced when `> 0`, so a negative
    ceiling reads as "no cap configured"; a negative trailing spend would
    instead hand the plan headroom that was never there. Neither is a budget,
    so both take the gate that already exists for budget data we do not have.
    """
    policy = DispatchPolicy(prefer_local=False, cost_matrix={"claude_code": 50.0})
    executors = [_executor("claude_code", cost=50.0)]

    for spend, ceiling in ((0.0, -1.0), (-5.0, 10.0)):
        plan = _plan(
            [_task("t1")],
            executors,
            policy,
            daily_spend_usd=spend,
            max_daily_spend_usd=ceiling,
        )
        assert plan.budget_unknown is True, (spend, ceiling)
        assert plan.assignments == (), (spend, ceiling)
        assert plan.decisions[0].reason == models.DEFER_COST_DATA_UNAVAILABLE


# --------------------------------------------------------------------------
# Unreadable policy is the third data gate (VOYN-W0-AICC-DISPATCH-FAILCLOSED-FALSE)
# --------------------------------------------------------------------------


def test_policy_unknown_defers_everything_even_when_budget_allows():
    # The policy could not be loaded. There is budget to spare and a free
    # executor, so nothing *else* would stop these tasks — which is the point:
    # only an explicit gate can, exactly as for the other two data reads.
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    plan = _plan([_task("t1"), _task("t2")], executors, policy, policy_unknown=True)

    assert plan.policy_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_POLICY_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_default_limits_are_not_a_substitute_for_an_unreadable_policy():
    # The regression this gate exists for, shown as a contrast — the policy
    # analogue of `test_an_empty_active_map_is_not_a_substitute_for_unknown_capacity`.
    #
    # An operator has pinned `ollama` to one concurrent run. Loading the real
    # policy defers the second task. Falling back to a *default* DispatchPolicy
    # — what an unreadable policy file used to produce — assigns both, because
    # its `per_agent_limits` is empty and empty means "no limit". The failed
    # read does not weaken the guardrail, it deletes it.
    configured = DispatchPolicy(
        prefer_local=True,
        local_executor_ids=frozenset({"ollama"}),
        per_agent_limits={"ollama": AgentLimit(max_concurrent=1, max_spend_usd=0.0)},
    )
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    tasks = [_task("t1"), _task("t2")]

    truthful = _plan(tasks, executors, configured)
    assert len(truthful.assignments) == 1
    assert truthful.decisions[1].reason == models.DEFER_AGENT_CAPACITY

    # The fail-open, demonstrated: the defaults impose nothing at all.
    defaulted = _plan(tasks, executors, DispatchPolicy())
    assert len(defaulted.assignments) == 2

    # With the gate engaged, the same unreadable policy assigns nothing.
    gated = _plan(tasks, executors, DispatchPolicy(), policy_unknown=True)
    assert gated.assignments == ()
    assert gated.decisions[0].reason == models.DEFER_POLICY_DATA_UNAVAILABLE


def test_policy_unknown_takes_priority_over_the_two_runtime_store_gates():
    # The policy names the ceilings the other two gates check against, so
    # "the budget could not be read" is not a well-posed statement while the
    # policy itself is unknown. The kill switch still outranks it.
    plan = _plan(
        [_task("t1")],
        [_executor("claude_code", cost=0.0)],
        DispatchPolicy(),
        policy_unknown=True,
        budget_unknown=True,
        capacity_unknown=True,
    )
    assert plan.decisions[0].reason == models.DEFER_POLICY_DATA_UNAVAILABLE

    with_switch = _plan(
        [_task("t1")],
        [_executor("claude_code", cost=0.0)],
        DispatchPolicy(),
        kill_switch_engaged=True,
        policy_unknown=True,
    )
    assert with_switch.decisions[0].reason == models.DEFER_KILL_SWITCH


def test_a_policy_gated_plan_serializes_as_valid_json():
    import json

    plan = _plan(
        [_task("t1")], [_executor("ollama", cost=0.0)], DispatchPolicy(),
        policy_unknown=True,
    )
    payload = json.loads(json.dumps(plan.as_dict()))
    assert payload["policy_unknown"] is True
    assert payload["assignment_count"] == 0
    assert payload["decisions"][0]["reason"] == "policy_data_unavailable"
    assert payload["decisions"][0]["explanation"]


# --------------------------------------------------------------------------
# Unreadable settings block everything, and outrank even the kill switch —
# the switch's own value lives in the document that could not be read.
# --------------------------------------------------------------------------


def test_settings_unknown_defers_everything():
    """Same hard gate as the other three, in the configuration that used to
    fail open: no cap configured (the default) and a free local executor."""
    policy = DispatchPolicy(prefer_local=True, local_executor_ids=frozenset({"ollama"}))
    executors = [_executor("ollama", cost=0.0, is_local=True)]
    tasks = [_task("t1", priority="Critical"), _task("t2", priority="High")]

    plan = _plan(
        tasks, executors, policy, max_daily_spend_usd=0.0, settings_unknown=True
    )

    assert plan.settings_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_SETTINGS_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_settings_unknown_outranks_the_kill_switch_in_the_reported_reason():
    """The ordering is the fix, not a cosmetic preference.

    `kill_switch_engaged` is derived from `settings.enabled`, so when the
    settings document is unreadable the caller's `enabled` is an artefact of
    the fallback rather than an operator decision. Reporting the kill switch
    would name a cause nobody chose and send the operator to the remedy —
    turn the master switch back on — that overwrites their spend ceiling with
    `0.0`, i.e. no cap at all.
    """
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.5)]

    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        kill_switch_engaged=True,
        settings_unknown=True,
    )

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_SETTINGS_DATA_UNAVAILABLE
    # And the flag itself is not asserted either: whether the switch is
    # engaged is exactly what is unknown.
    assert plan.kill_switch_engaged is False
    assert plan.settings_unknown is True


def test_settings_unknown_outranks_every_other_data_gate():
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.5)]

    plan = _plan(
        [_task("t1")],
        executors,
        policy,
        settings_unknown=True,
        policy_unknown=True,
        budget_unknown=True,
        capacity_unknown=True,
    )

    assert plan.decisions[0].reason == models.DEFER_SETTINGS_DATA_UNAVAILABLE
    # Every engaged gate is still reported, so nothing is hidden by the
    # ordering — only the single headline reason is chosen.
    assert plan.policy_unknown is True
    assert plan.budget_unknown is True
    assert plan.capacity_unknown is True


def test_settings_unknown_is_transmitted_in_the_plan_dict():
    policy = DispatchPolicy()
    executors = [_executor("claude_code", cost=0.5)]

    parsed = _plan(
        [_task("t1")], executors, policy, settings_unknown=True
    ).as_dict()

    assert parsed["settings_unknown"] is True
    assert parsed["kill_switch_engaged"] is False
    assert models.DEFER_SETTINGS_DATA_UNAVAILABLE in models.DEFER_REASONS
    # The typed reason carries its own explanation, like every other one.
    assert parsed["decisions"][0]["explanation"] != models.DEFER_SETTINGS_DATA_UNAVAILABLE
