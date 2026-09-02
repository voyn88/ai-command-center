# Agent Incentive Policy (VOYN-AGT-REWARD)

BizDev motivation model: the best-performing agents get priority tasks,
access to experimental zones, and a bigger budget. This is not a proposal —
it is a policy enforced structurally by the dispatch engine, the same engine
that already enforces the daily spend cap, the kill switch and SLA ordering.

## Where it lives

`command_center/dispatch/policy.py` (`plan_dispatch`) is the single place a
task is matched to an executor (an "agent" — `claude_code`, `codex`,
`ollama`, a human queue, …). It is pure and unit-tested
(`tests/dispatch/test_policy.py`); no dispatch decision is made anywhere
else. The leaderboard input itself is a field on the config-driven
`DispatchPolicy` (`data/dispatch_policy.json`, see `docs/AUTHORITY_MAP.md`),
edited the same way the cost matrix and budget caps already are:
`PUT /api/v1/dispatch/policy`.

## The mechanism

1. **Score.** `leaderboard: {executor_id -> 0-100}` is a leadership metric —
   however the business wants to compute it (task success rate, review
   quality, speed, cost-efficiency). This policy layer does not compute the
   score; it decides what the score is worth.
2. **Tier.** `tier_thresholds` maps score ranges onto named tiers
   (default `elite ≥ 85`, `trusted ≥ 60`, `standard ≥ 0`). An agent's tier is
   recomputed from its current score on every dispatch — standing is never
   sticky.
3. **Priority tasks.** `tier_priority_bonus` gives a better tier the tie
   ahead of a merely-cheaper rival when more than one executor is eligible
   for the same task. Priority/SLA ordering across *tasks* is untouched
   (guarantee 3 in `policy.py`) — this only changes which agent wins among
   candidates for one task.
4. **Experimental zones.** `experimental_executor_ids` flags specific
   executors (a new model, an incubating provider) as off-limits by default.
   `experimental_min_tier` is the bar an agent's own standing must clear to
   be assigned that zone's work — access is earned, never granted by
   project/pin policy alone. A task that only has experimental-and-ungated
   executors available stays queued with the typed reason
   `experimental_tier_required`; it is never silently skipped or force-run.
5. **Budget.** `tier_budget_multiplier` widens an agent's *own*
   `per_agent_limits` (max concurrent runs, max spend) — never below 1.0, so
   this only rewards, it never shrinks a configured limit. The **global**
   daily spend ceiling is never widened by tier: a reward can earn an agent
   more of its own room, but it can never itself become a budget breach.

## Defaults

With no leaderboard configured, every agent scores 0 and sits at the
`standard` tier with a 1.0x multiplier and zero priority bonus — dispatch
behaves exactly as it did before this policy existed. The reward model is
opt-in per deployment, applied by writing scores into
`data/dispatch_policy.json`.
