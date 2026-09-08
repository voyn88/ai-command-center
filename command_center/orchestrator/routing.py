"""The executor routing matrix (BO-S2a, executor-cascade).

Route order is the recorded decision chain quality -> risk -> privacy ->
latency -> cost. The first slice is deliberately STATIC and deliberately
HONEST: it names only executors that actually exist on the worker hosts
today. A cascade naming an absent executor would not fail loudly — the
worker's unavailability path *retries to the next link*, so a phantom link
silently burns one attempt of every task's budget. That is why codex is a
COMMENT, not an entry, until its CLI is proven on worker-01.

Cascade mechanics live where the state already is: the planner writes the
cascade into the payload, ``max_attempts`` = its length (the attempt budget
IS the cascade budget), and the worker selects ``cascade[attempt_no - 1]``
(clamped) — so executor failover rides the queue's existing retry/reap
machinery (SRV-06) with no new tables and no new loop, and the audit trail
is the existing ``work_event`` attempt history (attempt_no <-> cascade step
is a bijection until the clamp).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ROUTING_MATRIX",
    "MODEL_ONLY_REVIEW_EXECUTORS",
    "cascade_for",
    "model_only_review_cascade",
    "verification_review_cascade",
]

#: task class -> ordered cascade. Each link: executor + the agent_run fields
#: it pins. 'claude' is the headless CLI the worker's agent_runner already
#: drives — the one executor proven on the fleet.
ROUTING_MATRIX: dict[str, list[dict[str, Any]]] = {
    "implementation": [
        {"executor": "claude", "task_type": "implementation"},
        # Escalation link: a DIFFERENT ACCOUNT, not merely a second attempt.
        #
        # This used to be a duplicate `claude` entry ("same executor, stronger
        # effort profile"). Live measurement on 2026-08-23 showed why that was
        # the wrong escalation: the fleet's Claude credential is a Max
        # *subscription* with a 5-hour rolling cap, and 142 of 167 parked
        # `task_status_failed` tasks were literally "You've hit your session
        # limit" -- not task defects. A second Claude attempt escalates into
        # the same exhausted pool and cannot succeed for the same reason the
        # first failed. Codex bills against a separate account, so it is
        # capacity the Claude cap cannot consume.
        #
        # The phantom-link hazard this module's docstring warns about is
        # answered structurally, not by comment: the worker refuses any
        # executor absent from `agent_runner.COMMAND_BUILDERS`, and codex is
        # in that table only because its argv builder exists
        # (`build_codex_command`) and the CLI is installed on worker-01.
        {"executor": "codex", "task_type": "implementation"},
        # Third account, same reasoning one step further: if both the Claude
        # window and the Codex account are exhausted, Copilot's GitHub
        # subscription is capacity neither can consume. Three links also means
        # `max_attempts` is 3 (the attempt budget IS the cascade length), so a
        # task gets one genuine try per independent quota pool rather than
        # three tries at one pool.
        {"executor": "copilot", "task_type": "implementation"},
    ],
    "review": [
        # codex first: it is the only review pool currently reachable on the
        # fleet (copilot is org-blocked, the Claude subscription window is
        # exhausted), and it bills a separate account. `independent_review`
        # resolves to the read-only profile, so codex reviews under
        # `--sandbox read-only` -- a model-only reviewer that never writes.
        {"executor": "codex", "task_type": "review"},
        {"executor": "copilot", "task_type": "review"},
        {"executor": "claude", "task_type": "review"},
    ],
}


def cascade_for(task_class: str) -> list[dict[str, Any]]:
    """The cascade for a task class; unknown classes get the implementation
    route rather than a refusal — routing chooses HOW, never WHETHER."""
    return [
        dict(link)
        for link in ROUTING_MATRIX.get(task_class, ROUTING_MATRIX["implementation"])
    ]


#: The review route's executors that also serve the two read/model-only
#: verdict roles below. Restated here (not just "review") because
#: `ROUTING_MATRIX["review"]` is the escalation cascade's vocabulary
#: (executor + generic "review" task_type); a verdict role additionally pins
#: a specific `task_type` (`independent_review` / `verification_review`),
#: which the worker maps to a strictly different tool profile (see
#: `agent_runner.MODEL_ONLY_TASK_TYPES` / `READ_ONLY_TASK_TYPES`). Filtering to
#: this set (rather than every review executor) is itself part of the
#: contract: an executor absent here has no argv builder for the model-only /
#: read-only profile and must not silently enter a verdict role.
MODEL_ONLY_REVIEW_EXECUTORS = frozenset({"copilot", "claude", "codex", "openai_http"})


def model_only_review_cascade() -> list[dict[str, Any]]:
    """The **reviewer** role: the review route, retyped `independent_review`
    (MODEL_ONLY — the worker strips every tool). Renders a verdict from the
    diff/prompt alone, never the tree."""
    route = cascade_for("review")
    return [
        {**link, "task_type": "independent_review", "capability": "model_only"}
        for link in route
        if isinstance(link, dict) and link.get("executor") in MODEL_ONLY_REVIEW_EXECUTORS
    ]


def verification_review_cascade() -> list[dict[str, Any]]:
    """The **verifier** role: same executor route as the reviewer, retyped
    `verification_review` (read-only — Claude: Read/Grep/Glob; Codex:
    `--sandbox read-only`) instead of MODEL_ONLY's zero tools, since
    verification is exactly the task that must read the tree to confirm or
    refute a finding."""
    route = cascade_for("review")
    return [
        {**link, "task_type": "verification_review", "capability": "read_only"}
        for link in route
        if isinstance(link, dict) and link.get("executor") in MODEL_ONLY_REVIEW_EXECUTORS
    ]
