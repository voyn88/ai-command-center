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
1b. **Unknown budget blocks everything, the same way.** `budget_unknown=True`
   (the caller could not read the trailing-24h spend) is checked in the same
   place, before any assignment, and defers every task with
   `DEFER_COST_DATA_UNAVAILABLE`. This is deliberately a hard gate rather than
   a simulated spend figure: a faked number can be silently absorbed by a
   zero/unset daily cap or by a free executor, which would make "no cost
   data" fail *open* instead of closed.
1c. **Unknown in-flight capacity blocks everything too.** `capacity_unknown=True`
   (the caller could not read the per-executor active-run counts) is the same
   hard gate, deferring with `DEFER_CAPACITY_DATA_UNAVAILABLE`. Substituting an
   empty count map is the same porous trick as a simulated spend: it does not
   guess conservatively, it guesses *zero work in flight*, which raises the
   effective concurrency limit exactly when the runtime store cannot be
   consulted.
1d. **Budget arithmetic that cannot be performed blocks everything.** A spend
   figure or ceiling that is not usable money — non-finite (NaN/±inf) or
   negative — engages the same `DEFER_COST_DATA_UNAVAILABLE` gate. This is not
   defensive paranoia about a caller: NaN is the one input that *silently
   inverts* guarantee 2 below, because every `>` comparison against NaN is
   False, so a NaN spend total reads as "under every ceiling" for every task.
   A number that cannot be compared is not a budget, and this engine — the
   thing that makes the guarantee — is where that has to be caught.
2. **Budget is never exceeded.** An executor is only assigned when the
   *projected* cumulative spend (the trailing-24h spend already incurred plus
   every assignment made so far in this plan plus this one) stays at or under
   the configured daily ceiling — and likewise under the per-agent and
   per-project ceilings. The check happens before the assignment is recorded,
   so an over-budget assignment cannot be produced. Because the comparison is
   only meaningful on finite numbers, a non-finite per-task cost blocks the
   executor it belongs to rather than sailing past every ceiling — and the
   same holds for the *ceilings themselves*: a per-agent or per-project limit
   that is not usable money blocks what it governs instead of reading as the
   absent limit it superficially resembles. Both directions matter because
   both permissive readings are spelled `0.0` here: a zero price is free and a
   zero ceiling is unset, so corruption that decays to zero disables the
   guarantee from either end. See `DispatchPolicy` for how such a value is
   carried (as NaN) rather than normalised away.
3. **SLA/priority is never bypassed.** Tasks are consumed in a fixed order:
   priority weight (desc), then SLA deadline (earliest first), then age. A
   lower-priority task can never take capacity a higher-priority task in the
   same plan could have used.
4. **No force-run.** A task that finds nothing eligible within budget stays
   queued with a typed reason. The engine never "assigns anyway".
"""

from __future__ import annotations

import math

from command_center.dispatch.models import (
    ASSIGNED,
    DEFER_AGENT_BUDGET,
    DEFER_AGENT_CAPACITY,
    DEFER_CAPACITY_DATA_UNAVAILABLE,
    DEFER_COST_DATA_UNAVAILABLE,
    DEFER_DAILY_BUDGET,
    DEFER_KILL_SWITCH,
    DEFER_NO_AVAILABLE_EXECUTOR,
    DEFER_NO_ELIGIBLE_EXECUTOR,
    DEFER_PROJECT_BUDGET,
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
    daily_spend_usd: float,
    max_daily_spend_usd: float,
    kill_switch_engaged: bool,
    budget_unknown: bool = False,
    capacity_unknown: bool = False,
    active_by_executor: dict[str, int] | None = None,
) -> DispatchPlan:
    """Produce the dispatch plan. Pure and total; see module docstring for the
    guarantees this function structurally enforces."""
    active_by_executor = dict(active_by_executor or {})
    executor_by_id = {ex.id: ex for ex in executors}

    # Budget arithmetic that cannot be performed is budget data we do not have,
    # so it takes the gate that already exists for exactly that. Folded in here
    # rather than trusted to the caller because `plan_dispatch` is what promises
    # the ceiling holds; a NaN would not trip any `>` check further down, it
    # would quietly satisfy all of them. A *negative* figure is refused by the
    # same rule (`_usable`): a negative ceiling reads as unset via `> 0`, and a
    # negative trailing spend would hand the plan headroom it never had.
    if not _usable(daily_spend_usd) or not _usable(max_daily_spend_usd):
        budget_unknown = True

    # (1) Kill switch / unreadable guardrail inputs first: no assignment is
    #     even considered. Checked ahead of the per-task loop, exactly like the
    #     kill switch, so a caller can never accidentally leave a code path
    #     that assigns while the trailing-24h spend or the in-flight run counts
    #     are unreadable. The reason reported is the most fundamental of the
    #     engaged gates, in that order.
    if kill_switch_engaged or budget_unknown or capacity_unknown:
        if kill_switch_engaged:
            reason = DEFER_KILL_SWITCH
        elif budget_unknown:
            reason = DEFER_COST_DATA_UNAVAILABLE
        else:
            reason = DEFER_CAPACITY_DATA_UNAVAILABLE
        decisions = tuple(
            DispatchDecision(
                task_id=t.id,
                project=t.project,
                priority=t.priority,
                reason=reason,
            )
            for t in sorted(tasks, key=lambda t: _task_sort_key(t, policy))
        )
        return DispatchPlan(
            decisions=decisions,
            kill_switch_engaged=kill_switch_engaged,
            budget_unknown=budget_unknown,
            capacity_unknown=capacity_unknown,
            daily_spend_usd=daily_spend_usd,
            max_daily_spend_usd=max_daily_spend_usd,
            projected_spend_usd=daily_spend_usd,
        )

    # (3) SLA/priority order.
    ordered = sorted(tasks, key=lambda t: _task_sort_key(t, policy))

    projected = daily_spend_usd
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
        projected_spend_usd=projected,
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
    # A cost that is not usable money cannot be shown to fit any ceiling — a
    # non-finite one would defeat every comparison below rather than fail one,
    # and a negative one would *refund* the projected total — so the executor
    # carrying it is blocked outright.
    if not _usable(cost):
        return DEFER_DAILY_BUDGET

    # Global daily spend ceiling. `<= ceiling` after adding this cost.
    if max_daily_spend_usd > 0 and (projected + cost) > max_daily_spend_usd:
        return DEFER_DAILY_BUDGET

    limit = policy.per_agent_limits.get(executor.id)
    if limit is not None:
        # A negative concurrency ceiling is not "unset", it is a ceiling that
        # cannot be evaluated; `> 0` would silently read it as the former.
        if limit.max_concurrent < 0:
            return DEFER_AGENT_CAPACITY
        if limit.max_concurrent > 0:
            running = active_by_executor.get(executor.id, 0)
            planned = agent_assigned.get(executor.id, 0)
            if running + planned >= limit.max_concurrent:
                return DEFER_AGENT_CAPACITY
        # Same reasoning one field over, and this is the one a corrupt policy
        # file actually reaches: an unusable spend ceiling must not fall
        # through the `> 0` test into "no per-agent budget configured".
        if not _usable(limit.max_spend_usd):
            return DEFER_AGENT_BUDGET
        if limit.max_spend_usd > 0:
            spent = agent_spend.get(executor.id, 0.0)
            if (spent + cost) > limit.max_spend_usd:
                return DEFER_AGENT_BUDGET

    if task.project is not None:
        # A project with no entry gets `0.0` — genuinely unset, and finite, so
        # it passes the check below and skips the ceiling as intended.
        project_cap = policy.per_project_limits.get(task.project, 0.0)
        if not _usable(project_cap):
            return DEFER_PROJECT_BUDGET
        if project_cap > 0:
            spent = project_spend.get(task.project, 0.0)
            if (spent + cost) > project_cap:
                return DEFER_PROJECT_BUDGET

    return None


def _usable(amount: float) -> bool:
    """Whether `amount` is money this engine can compare: finite and not
    negative.

    The engine re-checks rather than trusting `DispatchPolicy.from_dict` to
    have normalised, because a `DispatchPolicy` is constructible directly and
    this function is where the budget guarantee is actually made. Mirrors
    `dispatch.models._usable_amount`, which is the same rule stated on the
    config side.
    """
    return math.isfinite(amount) and amount >= 0
