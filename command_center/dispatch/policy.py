"""The pure dispatch-selection engine.

`plan_dispatch` is a total, deterministic, I/O-free function. It takes the
queued tasks, the executor pool, the policy and a budget/kill-switch context,
and returns a `DispatchPlan`. Because it is pure, every acceptance property —
local preference, budget-cap enforcement, kill-switch respect, SLA ordering —
is asserted directly against it with no database, no filesystem and no HTTP.

The hard guarantees, enforced structurally here:

1. **Kill switch is checked first.** If engaged, the function returns before a
   single assignment is even considered — every task is deferred with
   `DEFER_KILL_SWITCH`. There is no code path that assigns while the switch is
   engaged.
2. **Budget is never exceeded.** An executor is only assigned when the
   *projected* cumulative spend (the trailing-24h spend already incurred plus
   every assignment made so far in this plan plus this one) stays at or under
   the configured daily ceiling — and likewise under the per-agent and
   per-project ceilings. The check happens before the assignment is recorded,
   so an over-budget assignment cannot be produced.
2b. **An unknown spend is not a number.** When a daily ceiling is configured
   but the trailing-24h spend could not be established, the function returns
   before any assignment with `DEFER_SPEND_UNKNOWN` — fail closed, because
   overspending is irreversible and skipping a tick is not, and with its own
   reason code, because "we cannot see the budget" is not "the budget is
   spent". No substitute value is invented: a low one would silently fail
   *open* on the money gate, and a high one would be indistinguishable from a
   genuinely exhausted budget.
3. **SLA/priority is never bypassed.** Tasks are consumed in a fixed order:
   priority weight (desc), then SLA deadline (earliest first), then age. A
   lower-priority task can never take capacity a higher-priority task in the
   same plan could have used.
4. **No force-run.** A task that finds nothing eligible within budget stays
   queued with a typed reason. The engine never "assigns anyway".
"""

from __future__ import annotations

from command_center.dispatch.models import (
    ASSIGNED,
    DEFER_AGENT_BUDGET,
    DEFER_AGENT_CAPACITY,
    DEFER_DAILY_BUDGET,
    DEFER_KILL_SWITCH,
    DEFER_NO_AVAILABLE_EXECUTOR,
    DEFER_NO_ELIGIBLE_EXECUTOR,
    DEFER_PROJECT_BUDGET,
    DEFER_SPEND_UNKNOWN,
    DispatchDecision,
    DispatchPlan,
    DispatchPolicy,
    ExecutorProfile,
    QueuedTask,
)

# A deadline of None must sort *after* every real deadline. ISO-8601 strings
# sort lexicographically, so a high sentinel keeps None last.
_NO_DEADLINE_SENTINEL = "￿"


def _task_sort_key(task: QueuedTask, policy: DispatchPolicy) -> tuple:
    """Deterministic SLA/priority order: priority weight (desc, via negation),
    then earliest SLA deadline, then oldest task, then id for total order."""
    return (
        -policy.priority_weight(task.priority),
        task.sla_deadline or _NO_DEADLINE_SENTINEL,
        task.created_at or _NO_DEADLINE_SENTINEL,
        task.id,
    )


def _eligible_executors(
    task: QueuedTask, executors: dict[str, ExecutorProfile]
) -> tuple[list[ExecutorProfile], str | None]:
    """Return the executors permitted for `task`, and a defer reason when the
    permitted set is empty or none of it is available.

    Order of the returned list is not yet cost-ordered — the caller sorts it.
    """
    if task.pinned_executor is not None:
        permitted_ids = [task.pinned_executor]
    elif task.allowed_executors is None:
        permitted_ids = list(executors.keys())
    else:
        permitted_ids = list(task.allowed_executors)

    permitted = [executors[eid] for eid in permitted_ids if eid in executors]
    if not permitted:
        return [], DEFER_NO_ELIGIBLE_EXECUTOR

    available = [ex for ex in permitted if ex.available]
    if not available:
        return [], DEFER_NO_AVAILABLE_EXECUTOR
    return available, None


def _cost_order_key(executor: ExecutorProfile, policy: DispatchPolicy) -> tuple:
    """Local-first (cost economy) when `prefer_local`, then cheapest, then id.

    The cost matrix drives selection; `prefer_local` only decides the tie
    posture — with it on, a local executor is preferred even if a cloud one is
    nominally cheaper, which is the "economy" intent of the acceptance."""
    local_rank = 0 if (policy.prefer_local and executor.is_local) else 1
    return (local_rank, executor.cost_per_task_usd, executor.id)


def plan_dispatch(
    tasks: list[QueuedTask],
    executors: list[ExecutorProfile],
    policy: DispatchPolicy,
    *,
    daily_spend_usd: float | None,
    max_daily_spend_usd: float,
    kill_switch_engaged: bool,
    active_by_executor: dict[str, int] | None = None,
) -> DispatchPlan:
    """Produce the dispatch plan. Pure and total; see module docstring for the
    guarantees this function structurally enforces.

    `daily_spend_usd=None` means the trailing-24h spend is not known. It is
    carried as "unknown" all the way here rather than being substituted with a
    number, because every substitute lies: a low one fails open on the money
    gate, a high one is indistinguishable from a real exhausted budget.
    """
    active_by_executor = dict(active_by_executor or {})
    executor_by_id = {ex.id: ex for ex in executors}
    spend_known = daily_spend_usd is not None

    def _all_deferred(reason: str, *, projected: float | None) -> DispatchPlan:
        return DispatchPlan(
            decisions=tuple(
                DispatchDecision(
                    task_id=t.id,
                    project=t.project,
                    priority=t.priority,
                    reason=reason,
                )
                for t in sorted(tasks, key=lambda t: _task_sort_key(t, policy))
            ),
            kill_switch_engaged=kill_switch_engaged,
            daily_spend_usd=daily_spend_usd,
            max_daily_spend_usd=max_daily_spend_usd,
            projected_spend_usd=projected,
            plan_cost_usd=0.0,
        )

    # (1) Kill switch first: no assignment is even considered.
    if kill_switch_engaged:
        return _all_deferred(DEFER_KILL_SWITCH, projected=daily_spend_usd)

    # (1b) The money gate needs a number it does not have. Fail closed, and
    #      with its own reason: an unreadable spend is not an exhausted budget.
    #      Only when a ceiling exists — with none configured there is no
    #      comparison to make and the unknown spend constrains nothing.
    if not spend_known and max_daily_spend_usd > 0:
        return _all_deferred(DEFER_SPEND_UNKNOWN, projected=None)

    # (3) SLA/priority order.
    ordered = sorted(tasks, key=lambda t: _task_sort_key(t, policy))

    # Reaching here with an unknown spend implies no daily ceiling, so the
    # baseline never enters a decision — it only feeds the reported projection,
    # which stays `None` to avoid presenting `0 + plan` as a real total.
    baseline = daily_spend_usd if spend_known else 0.0
    projected = baseline
    agent_spend: dict[str, float] = {}
    agent_assigned: dict[str, int] = {}
    project_spend: dict[str, float] = {}
    decisions: list[DispatchDecision] = []

    for task in ordered:
        candidates, empty_reason = _eligible_executors(task, executor_by_id)
        if empty_reason is not None:
            decisions.append(
                DispatchDecision(
                    task_id=task.id,
                    project=task.project,
                    priority=task.priority,
                    reason=empty_reason,
                )
            )
            continue

        candidates = sorted(candidates, key=lambda ex: _cost_order_key(ex, policy))

        chosen: ExecutorProfile | None = None
        # Track the binding constraint of the *cheapest* rejected candidate so
        # the defer reason is the most economically relevant one.
        blocking_reason: str | None = None
        for executor in candidates:
            cost = executor.cost_per_task_usd
            reason = _budget_block(
                executor=executor,
                cost=cost,
                task=task,
                policy=policy,
                projected=projected,
                max_daily_spend_usd=max_daily_spend_usd,
                agent_spend=agent_spend,
                agent_assigned=agent_assigned,
                active_by_executor=active_by_executor,
                project_spend=project_spend,
            )
            if reason is None:
                chosen = executor
                break
            if blocking_reason is None:
                blocking_reason = reason

        if chosen is None:
            decisions.append(
                DispatchDecision(
                    task_id=task.id,
                    project=task.project,
                    priority=task.priority,
                    reason=blocking_reason or DEFER_DAILY_BUDGET,
                )
            )
            continue

        # (2) Record the assignment and advance every accumulator, so the next
        #     task's budget checks see this commitment.
        cost = chosen.cost_per_task_usd
        projected += cost
        agent_spend[chosen.id] = agent_spend.get(chosen.id, 0.0) + cost
        agent_assigned[chosen.id] = agent_assigned.get(chosen.id, 0) + 1
        if task.project is not None:
            project_spend[task.project] = project_spend.get(task.project, 0.0) + cost
        decisions.append(
            DispatchDecision(
                task_id=task.id,
                project=task.project,
                priority=task.priority,
                reason=ASSIGNED,
                assigned_executor=chosen.id,
                estimated_cost_usd=cost,
            )
        )

    return DispatchPlan(
        decisions=tuple(decisions),
        kill_switch_engaged=False,
        daily_spend_usd=daily_spend_usd,
        max_daily_spend_usd=max_daily_spend_usd,
        projected_spend_usd=projected if spend_known else None,
        plan_cost_usd=projected - baseline,
    )


def _budget_block(
    *,
    executor: ExecutorProfile,
    cost: float,
    task: QueuedTask,
    policy: DispatchPolicy,
    projected: float,
    max_daily_spend_usd: float,
    agent_spend: dict[str, float],
    agent_assigned: dict[str, int],
    active_by_executor: dict[str, int],
    project_spend: dict[str, float],
) -> str | None:
    """Return the typed defer reason that blocks assigning `executor` to
    `task`, or None if it is within every budget/capacity guardrail.

    Checked in the same fail-closed order the acceptance cares about: the
    global daily ceiling first (the kill-switch's budget sibling), then the
    per-agent concurrency/spend guardrails, then the per-project ceiling.
    """
    # Global daily spend ceiling. `<= ceiling` after adding this cost.
    if max_daily_spend_usd > 0 and (projected + cost) > max_daily_spend_usd:
        return DEFER_DAILY_BUDGET

    limit = policy.per_agent_limits.get(executor.id)
    if limit is not None:
        if limit.max_concurrent > 0:
            running = active_by_executor.get(executor.id, 0)
            planned = agent_assigned.get(executor.id, 0)
            if running + planned >= limit.max_concurrent:
                return DEFER_AGENT_CAPACITY
        if limit.max_spend_usd > 0:
            spent = agent_spend.get(executor.id, 0.0)
            if (spent + cost) > limit.max_spend_usd:
                return DEFER_AGENT_BUDGET

    if task.project is not None:
        project_cap = policy.per_project_limits.get(task.project, 0.0)
        if project_cap > 0:
            spent = project_spend.get(task.project, 0.0)
            if (spent + cost) > project_cap:
                return DEFER_PROJECT_BUDGET

    return None
