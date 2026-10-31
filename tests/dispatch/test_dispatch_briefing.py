"""Tests for `DispatchDecision.briefing` — the spoken-style explanation that
turns a dispatch outcome into "not just an answer, but a chain of reasons and
alternatives" (VOYN-MIN-CONTEXT-VOICE).

The acceptance for this ticket is that complex requests turn into an
understandable instruction, so this module generates 60+ varied, genuinely
complex dispatch scenarios (multiple candidates, budgets, pins, tail risk,
kill switch, unavailable/ineligible executors) and asserts every resulting
decision's `briefing` is a plain-language sentence that actually names the
task, the outcome, and every alternative considered — never a bare code.
"""

from __future__ import annotations

from command_center.dispatch import models
from command_center.dispatch.models import (
    AgentLimit,
    DispatchPolicy,
    ExecutorProfile,
    QueuedTask,
    TailRiskScenario,
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
) -> QueuedTask:
    return QueuedTask(
        id=tid,
        project=project,
        priority=priority,
        allowed_executors=allowed,
        pinned_executor=pinned,
    )


def _plan(tasks, executors, policy, **ctx):
    ctx.setdefault("daily_spend_usd", 0.0)
    ctx.setdefault("max_daily_spend_usd", 0.0)
    ctx.setdefault("kill_switch_engaged", False)
    return plan_dispatch(tasks, executors, policy, **ctx)


def _assert_understandable(decision) -> None:
    """A briefing is "understandable instruction", not a bare code: it must
    name the task, read as prose (not just the raw reason constant), and
    mention every alternative it claims to have considered."""
    briefing = decision.briefing
    assert isinstance(briefing, str) and briefing.strip()
    assert decision.task_id in briefing
    assert briefing != decision.reason
    assert briefing.endswith(".")
    for alt in decision.alternatives:
        assert alt.executor_id in briefing
        assert alt.reason != ""
        # Every alternative's own reason must itself resolve to prose, not a
        # bare, unexplained code slipping into the spoken text.
        assert alt.explanation != alt.reason or " " in alt.explanation


PRIORITIES = ("Critical", "High", "Medium", "Low")


def _scenarios():
    """60+ complex dispatch situations: several eligible executors at
    different costs/availability, tight or exhausted budgets, per-agent and
    per-project caps, hard pins, restricted eligibility, tail-risk breaches
    and the kill switch — the same guardrails `plan_dispatch` enforces,
    combined so each scenario forces a genuine comparison among candidates.
    """
    scenarios = []

    # Group 1: three-candidate races across every priority and a spread of
    # daily budgets, some tight enough to force a defer after assignment.
    for priority in PRIORITIES:
        for max_daily in (0.0, 0.5, 1.0, 1.5, 3.0):
            policy = DispatchPolicy(
                prefer_local=True,
                local_executor_ids=frozenset({"ollama"}),
                cost_matrix={"ollama": 0.2, "claude_code": 0.4, "codex": 0.6},
            )
            executors = [
                _executor("ollama", cost=0.2, is_local=True),
                _executor("claude_code", cost=0.4),
                _executor("codex", cost=0.6),
            ]
            tasks = [_task("race", priority=priority)]
            scenarios.append((tasks, executors, policy, {"max_daily_spend_usd": max_daily}))

    # Group 2: per-agent spend/concurrency caps binding on the cheapest
    # candidate, forcing the engine onto (or past) the second cheapest.
    for cap in (0.0, 0.3, 0.5, 1.0):
        for concurrent in (0, 1):
            policy = DispatchPolicy(
                cost_matrix={"ollama": 0.2, "claude_code": 0.5},
                per_agent_limits={
                    "ollama": AgentLimit(max_concurrent=concurrent or 5, max_spend_usd=cap)
                },
            )
            executors = [
                _executor("ollama", cost=0.2, is_local=True),
                _executor("claude_code", cost=0.5),
            ]
            scenarios.append(
                (
                    [_task("agent-cap")],
                    executors,
                    policy,
                    {
                        "max_daily_spend_usd": 5.0,
                        "active_by_executor": {"ollama": 1} if concurrent else {},
                    },
                )
            )

    # Group 3: per-project spend caps, varied across three candidate pools
    # and every priority.
    for project_cap in (0.0, 0.4, 0.9, 2.0):
        for priority in PRIORITIES:
            policy = DispatchPolicy(
                cost_matrix={"ollama": 0.3, "claude_code": 0.4, "codex": 0.5},
                per_project_limits={"AICC": project_cap},
            )
            executors = [
                _executor("ollama", cost=0.3, is_local=True),
                _executor("claude_code", cost=0.4),
                _executor("codex", cost=0.5),
            ]
            scenarios.append(
                (
                    [_task("project-cap", project="AICC", priority=priority)],
                    executors,
                    policy,
                    {"max_daily_spend_usd": 5.0},
                )
            )

    # Group 4: eligibility restrictions (allowed_executors subsets) and hard
    # pins, each with a bystander candidate that must show up as rejected
    # (ineligible) rather than silently vanishing.
    for allowed in (
        frozenset({"claude_code"}),
        frozenset({"codex"}),
        frozenset({"claude_code", "codex"}),
    ):
        for pinned in (None, "codex"):
            policy = DispatchPolicy(cost_matrix={"ollama": 0.1, "claude_code": 0.3, "codex": 0.5})
            executors = [
                _executor("ollama", cost=0.1, is_local=True),
                _executor("claude_code", cost=0.3),
                _executor("codex", cost=0.5),
            ]
            scenarios.append(
                (
                    [_task("eligibility", allowed=allowed, pinned=pinned)],
                    executors,
                    policy,
                    {"max_daily_spend_usd": 5.0},
                )
            )

    # Group 5: one candidate unavailable, forcing the plan onto (or entirely
    # off of) the remaining pool.
    for unavailable in ("ollama", "claude_code", "codex"):
        executors = [
            _executor("ollama", cost=0.1, is_local=True, available=unavailable != "ollama"),
            _executor("claude_code", cost=0.3, available=unavailable != "claude_code"),
            _executor("codex", cost=0.5, available=unavailable != "codex"),
        ]
        policy = DispatchPolicy(cost_matrix={"ollama": 0.1, "claude_code": 0.3, "codex": 0.5})
        scenarios.append(
            ([_task("availability")], executors, policy, {"max_daily_spend_usd": 5.0})
        )

    # Group 6: tail-risk breach vs. clear, per business path, still with a
    # multi-candidate pool behind the gate.
    for business_path, probability, breaches in (
        ("AICC", 0.9, True),
        ("AICC", 0.001, False),
        ("OTHER", 0.9, False),  # scoped away from AICC — never blocks here
    ):
        policy = DispatchPolicy(
            cost_matrix={"ollama": 0.1, "claude_code": 0.3},
            tail_risk_scenarios={
                "scenario": TailRiskScenario(
                    id="scenario",
                    label="scenario",
                    business_path=business_path,
                    probability=probability,
                    impact_usd=100.0,
                    assumptions="test",
                    limit_usd=10.0 if breaches else 1000.0,
                )
            },
        )
        executors = [
            _executor("ollama", cost=0.1, is_local=True),
            _executor("claude_code", cost=0.3),
        ]
        scenarios.append(
            (
                [_task("tail-risk", project="AICC")],
                executors,
                policy,
                {"max_daily_spend_usd": 5.0},
            )
        )

    # Group 7: kill switch and unknown-budget, each still carrying a
    # multi-candidate pool that must defer wholesale rather than pick one.
    for kill_switch, budget_unknown in ((True, False), (False, True), (True, True)):
        policy = DispatchPolicy(cost_matrix={"ollama": 0.0, "claude_code": 0.2})
        executors = [
            _executor("ollama", cost=0.0, is_local=True),
            _executor("claude_code", cost=0.2),
        ]
        ctx = {
            "kill_switch_engaged": kill_switch,
            "budget_unknown": budget_unknown,
            "max_daily_spend_usd": 5.0,
        }
        if budget_unknown:
            ctx["daily_spend_usd"] = None
        scenarios.append(([_task("kill-or-unknown")], executors, policy, ctx))

    # Group 8: multi-task queues sharing one scarce budget across mixed
    # priorities, so some tasks in the same plan are assigned and others
    # deferred by the very budget their neighbors consumed.
    for max_daily in (0.6, 0.9, 1.2, 5.0):
        policy = DispatchPolicy(cost_matrix={"claude_code": 0.3})
        executors = [_executor("claude_code", cost=0.3)]
        tasks = [
            _task("low", priority="Low"),
            _task("high", priority="High"),
            _task("crit", priority="Critical"),
        ]
        scenarios.append((tasks, executors, policy, {"max_daily_spend_usd": max_daily}))

    return scenarios


def test_sixty_plus_complex_scenarios_produce_understandable_briefings():
    scenarios = _scenarios()
    assert len(scenarios) >= 60, f"only {len(scenarios)} scenarios — need 60+"

    total_decisions = 0
    total_with_alternatives = 0
    for tasks, executors, policy, ctx in scenarios:
        plan = _plan(tasks, executors, policy, **ctx)
        assert len(plan.decisions) == len(tasks)
        for decision in plan.decisions:
            _assert_understandable(decision)
            total_decisions += 1
            if decision.alternatives:
                total_with_alternatives += 1

    # The whole point of this ticket: a meaningful share of these complex,
    # multi-candidate scenarios must actually surface alternatives, not just
    # a single unexplained outcome.
    assert total_with_alternatives >= 30


def test_assigned_briefing_names_winner_and_every_rejected_alternative():
    policy = DispatchPolicy(
        prefer_local=True,
        local_executor_ids=frozenset({"ollama"}),
        cost_matrix={"ollama": 0.2, "claude_code": 0.1, "codex": 0.6},
        per_agent_limits={"claude_code": AgentLimit(max_spend_usd=0.0)},
    )
    executors = [
        _executor("ollama", cost=0.2, is_local=True),
        _executor("claude_code", cost=0.1),
        _executor("codex", cost=0.6),
    ]
    plan = _plan([_task("t1")], executors, policy, max_daily_spend_usd=5.0)

    decision = plan.decisions[0]
    assert decision.assigned_executor == "ollama"  # local-preferred over cheaper claude_code
    alt_ids = {alt.executor_id for alt in decision.alternatives}
    assert alt_ids == {"claude_code", "codex"}

    briefing = decision.briefing
    assert "ollama" in briefing
    assert "claude_code" in briefing
    assert "codex" in briefing
    assert "2 other alternatives considered" in briefing


def test_deferred_briefing_lists_each_candidates_own_blocking_reason():
    policy = DispatchPolicy(
        cost_matrix={"claude_code": 0.6, "codex": 0.9},
        per_agent_limits={
            "claude_code": AgentLimit(max_spend_usd=0.5),
            "codex": AgentLimit(max_spend_usd=0.5),
        },
    )
    executors = [
        _executor("claude_code", cost=0.6),
        _executor("codex", cost=0.9),
    ]
    plan = _plan([_task("t1")], executors, policy, max_daily_spend_usd=5.0)

    decision = plan.decisions[0]
    assert decision.assigned is False
    assert len(decision.alternatives) == 2
    assert all(alt.reason == models.DEFER_AGENT_BUDGET for alt in decision.alternatives)

    briefing = decision.briefing
    assert "stays queued" in briefing
    assert "claude_code" in briefing
    assert "codex" in briefing
    assert "considered and rejected" in briefing


def test_no_alternatives_when_the_plan_never_reaches_candidate_comparison():
    policy = DispatchPolicy(cost_matrix={"claude_code": 0.1})
    executors = [_executor("claude_code", cost=0.1)]
    plan = _plan(
        [_task("t1")], executors, policy, kill_switch_engaged=True, max_daily_spend_usd=5.0
    )

    decision = plan.decisions[0]
    assert decision.alternatives == ()
    assert decision.briefing == (
        f"Task t1 (Medium priority) stays queued. {models.explanation_for(models.DEFER_KILL_SWITCH)}"
    )
