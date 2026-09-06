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

``bounded_implementation`` (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR) is the one
entry that is no longer purely static: its ``aider`` link is filtered out by
``local_model_gates.is_promoted`` unless that task class has cleared its
benchmark bar (owner decision 2026-09-03 — "benchmarked per task class before
promotion"). ``cascade_for`` therefore does one small file read per call, the
only I/O in an otherwise pure module; every other entry, and every other link
in this one, is untouched by it. The claude/codex/copilot tail is never
filtered — a benchmark gate can only ever REMOVE the free local-model link,
never the proven cloud fallback a task still needs to complete.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from command_center import agent_runner
from command_center.orchestrator import local_model_gates

__all__ = ["ROUTING_MATRIX", "cascade_for", "classify_task_class"]

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
    # Bounded-implementation lane (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR): the
    # planner routes low-risk work here (see `classify_task_class`) instead
    # of `implementation`. aider leads because it is free and structurally
    # incapable of the mutations the other links can make (no shell tool at
    # all — see `agent_runner.build_aider_command`), but it is a benchmark-
    # gated link, not an unconditional one: `cascade_for` drops it unless
    # `local_model_gates.is_promoted("bounded_implementation", ...)`. The
    # exact same claude/codex/copilot tail as `implementation` follows it, so
    # a task this lane misclassifies, or that aider genuinely cannot finish,
    # still gets the full proven cascade rather than dead-ending.
    "bounded_implementation": [
        {"executor": "aider", "task_type": "implementation"},
        {"executor": "claude", "task_type": "implementation"},
        {"executor": "codex", "task_type": "implementation"},
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


def cascade_for(task_class: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    """The cascade for a task class; unknown classes get the implementation
    route rather than a refusal — routing chooses HOW, never WHETHER.

    `root` is a test seam (points `local_model_gates` at an isolated data
    dir); every real caller omits it and gets `agent_runner.ROOT`, the same
    checkout every other promotion/config file in this project reads from.
    """
    resolved_root = root if root is not None else agent_runner.ROOT
    cascade = [
        dict(link)
        for link in ROUTING_MATRIX.get(task_class, ROUTING_MATRIX["implementation"])
    ]
    return [
        link
        for link in cascade
        if link.get("executor") != "aider"
        or local_model_gates.is_promoted(task_class, resolved_root)
    ]


#: Title/body substrings that mark a task as eligible for the bounded-
#: implementation lane (docs, fixtures, small mechanical patches — the owner
#: decision's own examples). Advisory input to routing, not the technical
#: gate: `local_model_gates.is_promoted` is what actually decides whether
#: `bounded_implementation`'s aider link survives `cascade_for`, and the
#: cascade always keeps the full claude/codex/copilot tail regardless — so a
#: task misclassified as bounded still completes on the proven chain, it
#: just tries the free lane first once that lane is promoted.
_BOUNDED_IMPLEMENTATION_MARKERS = (
    "docs:",
    "doc:",
    "documentation:",
    "readme",
    "changelog",
    "fixture",
    "typo",
    "chore:",
)


def classify_task_class(title: str, body: str) -> str:
    """The planner's task-class vocabulary lookup: `bounded_implementation`
    for low-risk work whose title/body names one of the markers above,
    `implementation` otherwise. Kept a pure function of the task's own text
    (no new backlog column) so it stays trivially testable and every
    classification is explainable from the same words a human reads."""
    haystack = f"{title}\n{body}".lower()
    if any(marker in haystack for marker in _BOUNDED_IMPLEMENTATION_MARKERS):
        return "bounded_implementation"
    return "implementation"
