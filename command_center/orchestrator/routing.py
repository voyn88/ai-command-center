"""The executor routing matrix (BO-S2a, executor-cascade).

Route order is the recorded decision chain quality -> risk -> privacy ->
latency -> cost, and THAT ORDER is static — a table any operator can read
top to bottom and predict. Which link actually gets to run is not: a link
naming an executor absent from the worker's proven set is skipped exactly
like one this host currently knows is quota-exhausted (VOYN-W0-AICC-
EXECUTOR-QUOTA-AWARE-ROUTING — `agent_runner.executor_exhausted_until`,
checked in `worker.handlers._executor_preflight`), both without spending an
attempt. An earlier slice of this module was static in the stronger sense of
withholding codex entirely (as a comment, not an entry) until its CLI was
proven on worker-01; both codex and copilot are proven entries now, and the
routing decision that matters live is no longer "is this executor listed"
but "is this executor's account, right now, still spendable" — a fact only
runtime observation (a session-limit refusal, a monthly-quota refusal)
can supply, never a table fixed before execution. A cascade naming an
absent executor would still not fail loudly on its own — the worker's
unavailability path *retries to the next link*, so a phantom link would
otherwise silently burn one attempt of every task's budget — which is why
every executor named here must still be proven (see
`tests.orchestrator.test_routing.PROVEN_EXECUTORS`).

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

__all__ = ["ROUTING_MATRIX", "cascade_for"]

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
}


def cascade_for(task_class: str) -> list[dict[str, Any]]:
    """The cascade for a task class; unknown classes get the implementation
    route rather than a refusal — routing chooses HOW, never WHETHER."""
    return [
        dict(link)
        for link in ROUTING_MATRIX.get(task_class, ROUTING_MATRIX["implementation"])
    ]
