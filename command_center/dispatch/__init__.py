"""Agent-dispatch policy layer (VOYN-W2-AGENT).

A **policy layer on top of** the existing launch primitives — it decides
*which executor each queued task should be assigned to*, respecting the daily
spend budget, the kill switch and SLA/priority, and never launches anything
itself. The actual process launch stays with the v2 Session Supervisor and
`task_pipeline`; this package only records the chosen executor onto the task
(through `tasks_repository`, the single writer) so the existing pipeline
launches it there.

Layering (Controller -> Service -> Repository):

* `models`      — pure dataclasses + typed reason codes (no I/O).
* `policy`      — the pure selection engine: given queued tasks, an executor
                  pool, a policy and a budget/kill-switch context, produce a
                  `DispatchPlan`. No I/O, fully unit-testable.
* `policy_config` — persistence of the config-driven `DispatchPolicy`
                  (`data/dispatch_policy.json`, file-locked atomic write — the
                  single writer of that store; named like `pipeline_settings`,
                  a config store, not an engine).
* `service`     — orchestration: gather queued tasks, the executor pool and
                  the budget/kill-switch context from the existing primitives
                  (`tasks_repository`, `executors`, `pipeline_settings`,
                  `task_pipeline.daily_spend_usd`), call the engine, and — for
                  `assign` — apply the plan via `tasks_repository`.
* `api`         — the thin FastAPI controller (`/api/v1/dispatch/*`).

Hard guarantees (acceptance): a dispatch decision can NEVER exceed a
configured budget, NEVER bypass the kill switch and NEVER reorder past
SLA/priority. When nothing eligible fits within budget the task stays queued
with a typed reason — it is never force-run.

Leadership metrics (VOYN-AGT-REWARD): `DispatchPolicy.leaderboard` scores each
executor 0-100 and maps that score onto a configurable tier ladder
(`tier_thresholds`). A better tier wins dispatch ties over a merely-cheaper
rival (`tier_priority_bonus`), unlocks executors flagged as experimental zones
(`experimental_executor_ids` / `experimental_min_tier`), and widens that
agent's own per-agent budget/concurrency limit (`tier_budget_multiplier`) —
never the global daily ceiling, which stays tier-blind. The score is
policy-configured, like the cost matrix: this layer decides what a score
unlocks, not how it is computed.
"""

from __future__ import annotations

from command_center.dispatch import models, policy, policy_config, service

__all__ = ["models", "policy", "policy_config", "service"]
