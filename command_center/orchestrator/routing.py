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
IS the cascade budget), and the worker selects the cascade link by
``attempt_no`` wrapped modulo the cascade length (see
``worker.handlers._cascade_step``) — so executor failover rides the queue's
existing retry/reap machinery (SRV-06) with no new tables and no new loop,
and the audit trail is the existing ``work_event`` attempt history. A
redrive (``queue_redrive``) widens ``max_attempts`` without resetting
``attempt_count``, so attempt_no <-> cascade step stays a bijection only
within one pass through the cascade; the wrap is what makes a redrive's
fresh attempts walk the cascade again instead of dead-ending on the last
link every time (VOYN-W0-AICC-REDRIVE-CLAMP-RESETS-TO-LAST-LINK).
"""

from __future__ import annotations

from typing import Any

from command_center.orchestrator import local_model_gates

__all__ = [
    "ROUTING_MATRIX",
    "BOUNDED_IMPLEMENTATION_TASK_CLASS",
    "cascade_for",
    "classify_task_class",
]

#: The task class an author opts a low-risk task into (AICC Fleet decision
#: 2026-09-03): docs/fixture/mechanical patches, benchmark-gated onto the
#: free local-model executor (`agent_runner.build_aider_command` + Ollama)
#: before it may run there for real. See `classify_task_class` and
#: `cascade_for`.
BOUNDED_IMPLEMENTATION_TASK_CLASS = "bounded_implementation"

#: Exact, bracketed title prefixes that opt a task into
#: `BOUNDED_IMPLEMENTATION_TASK_CLASS`. Deliberately a small fixed vocabulary
#: matched only against the TITLE, never the body -- see
#: `classify_task_class`.
_BOUNDED_TASK_CLASS_TITLE_PREFIXES: tuple[str, ...] = (
    "[docs]",
    "[fixture]",
    "[mechanical]",
)

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
        # No copilot link. It was the third account ("capacity neither of the
        # other two can consume"), but ADR-0010 keeps copilot OFF the isolated
        # worker principal -- its login credential carries GitHub/repository
        # authority (`agent_runner.PRINCIPAL_EXECUTOR_BINARIES`) -- and the
        # whole fleet runs isolated since 2026-09-08. A link the worker refuses
        # at preflight is exactly the phantom link this module's docstring
        # warns about: live, it burned the third and last attempt of every
        # task whose first two failed ("isolated copilot cli unavailable",
        # 48 dead attempts in 20 minutes). Restore it only together with an
        # accepted ADR-0010 revision that stages copilot under isolation.
    ],
    "review": [
        # codex first: it is the only review pool currently reachable on the
        # fleet (copilot is org-blocked, the Claude subscription window is
        # exhausted), and it bills a separate account. `independent_review`
        # resolves to the read-only profile, so codex reviews under
        # `--sandbox read-only` -- a model-only reviewer that never writes.
        {"executor": "codex", "task_type": "review"},
        # copilot: see the implementation cascade -- refused under principal
        # isolation, so it would only burn a review attempt.
        {"executor": "claude", "task_type": "review"},
    ],
    # AICC Fleet decision 2026-09-03: aider + local Ollama as a free,
    # bounded-implementation executor for low-risk task classes (docs,
    # fixtures, small mechanical patches). It LEADS the cascade -- free
    # capacity should be tried before any metered/quota'd account -- but
    # only once its measured quality has cleared the benchmark bar
    # (`cascade_for` strips this link out entirely while unpromoted, rather
    # than leaving a phantom entry that would burn an attempt on a executor
    # the worker refuses). The remaining links are the same escalation chain
    # `implementation` uses, so an unpromoted (or a failed-aider) dispatch
    # degrades to exactly the existing behaviour.
    BOUNDED_IMPLEMENTATION_TASK_CLASS: [
        {"executor": "aider", "task_type": "implementation"},
        {"executor": "claude", "task_type": "implementation"},
        {"executor": "codex", "task_type": "implementation"},
        {"executor": "copilot", "task_type": "implementation"},
    ],
}


def cascade_for(task_class: str) -> list[dict[str, Any]]:
    """The cascade for a task class; unknown classes get the implementation
    route rather than a refusal — routing chooses HOW, never WHETHER.

    `BOUNDED_IMPLEMENTATION_TASK_CLASS`'s `aider` link is additionally gated
    on `local_model_gates.is_promoted`: while unpromoted, the link is
    filtered out of the returned cascade entirely (not merely deprioritised),
    so the worker's "refuse any executor absent from a healthy state" path
    never has to consume an attempt discovering that a benchmark-gated
    executor is not yet cleared for real dispatch -- the same phantom-link
    hazard this module's docstring names for an absent-CLI executor, closed
    the same way: the offending link is never in the cascade at all.
    """
    cascade = [
        dict(link)
        for link in ROUTING_MATRIX.get(task_class, ROUTING_MATRIX["implementation"])
    ]
    if task_class == BOUNDED_IMPLEMENTATION_TASK_CLASS and not local_model_gates.is_promoted(
        task_class
    ):
        cascade = [link for link in cascade if link["executor"] != "aider"]
    return cascade


def classify_task_class(title: str, body: str) -> str:
    """The dispatch task class for a backlog task, from its TITLE alone.

    Opting into `BOUNDED_IMPLEMENTATION_TASK_CLASS` is a task AUTHOR
    decision, not an inference: only an exact bracketed title prefix
    (`_BOUNDED_TASK_CLASS_TITLE_PREFIXES`) routes a task into the
    benchmark-gated local-model lane. `body` is accepted for a stable,
    forward-compatible signature but deliberately never scanned -- matching
    against free-form body text would let incidental body wording downgrade
    a mutating task into the weaker executor lane without its author ever
    having asked for that. Every other title, including one that merely
    mentions "docs" or "mechanical" without the exact bracketed prefix,
    classifies as plain `"implementation"`.
    """
    del body  # never scanned, by design — see the docstring above
    normalized = title.strip().lower()
    if normalized.startswith(_BOUNDED_TASK_CLASS_TITLE_PREFIXES):
        return BOUNDED_IMPLEMENTATION_TASK_CLASS
    return "implementation"
