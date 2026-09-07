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
task class whose cascade leads with a FREE executor: ``aider`` (OSS) driving
a local Ollama model on the worker host. It is deliberately NOT admitted the
same way the paid executors are — a phantom-link-shaped hazard the module
docstring above already warns about would be compounded here by an
unproven MODEL, not just an unproven CLI. So the ``aider`` link is gated
behind its own per-class benchmark ledger (``orchestrator.local_model_
gates``): ``cascade_for`` drops the link outright for any class that has
not been PROMOTED, regardless of what the literal ``ROUTING_MATRIX`` entry
names. See ``cascade_for``'s docstring for why the filtering has to live in
code, not merely in this comment.
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

#: The task class `classify_task_class` resolves a labelled low-risk task to.
#: A plain module-level string constant (not, say, an enum) because that is
#: exactly what every other key in `ROUTING_MATRIX` already is -- consistency
#: with the existing vocabulary, not a new one.
BOUNDED_IMPLEMENTATION_TASK_CLASS = "bounded_implementation"

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
    # Same three paid links as "implementation", same order, same escalation
    # reasoning -- a class stuck at the free tier (never promoted, or
    # demoted back) is never worse off than the standard route, only better
    # once its ledger proves it out. `aider`'s link is unconditional HERE;
    # `cascade_for` is what actually enforces the promotion gate below.
    BOUNDED_IMPLEMENTATION_TASK_CLASS: [
        {
            "executor": "aider",
            "task_type": "implementation",
            "model": "ollama_chat/qwen2.5-coder:14b",
        },
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


def cascade_for(task_class: str) -> list[dict[str, Any]]:
    """The cascade for a task class; unknown classes get the implementation
    route rather than a refusal — routing chooses HOW, never WHETHER.

    For ``BOUNDED_IMPLEMENTATION_TASK_CLASS`` specifically, the ``aider``
    link is dropped unless ``local_model_gates.is_promoted`` says this class
    has cleared its benchmark bar. This is the actual enforcement of "the
    executor is benchmarked per task class before promotion" — an earlier
    revision of this module named the link in ``ROUTING_MATRIX``
    unconditionally and never filtered it here, so every
    ``bounded_implementation`` dispatch reached ``aider`` regardless of
    promotion state (independent review of PR #700 at b280dfc2, chunk
    1/6). Every other task class is returned unfiltered — this gate is
    specific to the one class it exists for.
    """
    cascade = [
        dict(link)
        for link in ROUTING_MATRIX.get(task_class, ROUTING_MATRIX["implementation"])
    ]
    if task_class == BOUNDED_IMPLEMENTATION_TASK_CLASS and not local_model_gates.is_promoted(
        BOUNDED_IMPLEMENTATION_TASK_CLASS
    ):
        cascade = [link for link in cascade if link["executor"] != "aider"]
    return cascade


#: Title-prefix labels an operator (or the task-authoring tooling) attaches
#: to mark a task as one of the low-risk classes this lane exists for --
#: docs fixes, fixture/test-data updates, small mechanical patches (rename,
#: constant bump, import cleanup). Deliberately an explicit opt-in LABEL,
#: not a body-text keyword search: a mutating task whose BODY merely
#: mentions "typo" or "docs" in passing must not be silently reclassified
#: into a cheaper, less-capable executor lane it never asked for. Matched
#: case-insensitively against the title's own prefix only.
_BOUNDED_IMPLEMENTATION_LABELS: tuple[str, ...] = (
    "[docs]",
    "[fixture]",
    "[mechanical]",
)


def classify_task_class(title: str | None, body: str | None) -> str:
    """The routing class for a task's title/body.

    Fails closed to ``"implementation"``: the only other value this can
    ever return is the literal ``BOUNDED_IMPLEMENTATION_TASK_CLASS``
    constant, and both are guaranteed ``ROUTING_MATRIX`` keys by
    construction (pinned by
    ``test_classify_task_class_only_returns_routed_classes`` in
    ``tests/orchestrator/test_routing.py``) — so a caller threading this
    straight into ``cascade_for`` (as ``planner.py`` does) can never hand it
    an unrouted class name, whatever `title`/`body` contain. `body` is
    accepted for interface symmetry with a future richer classifier and is
    deliberately NOT inspected today — see the label docstring above for
    why body text is untrusted signal here.
    """
    normalized = (title or "").strip().lower()
    if normalized.startswith(_BOUNDED_IMPLEMENTATION_LABELS):
        return BOUNDED_IMPLEMENTATION_TASK_CLASS
    return "implementation"
