"""Emergency Conservative Mode — the SRE fail-safe for high-risk executor
launches (VOYN-MIN-EMO).

During an incident, an operator may need every executor launch across the
fleet to stop mutating state immediately, without hunting down and reverting
every individual task's capability override. This module is the single
chokepoint for that: while active, it forces `capabilities.decide()` to the
`PROFILE_READ_ONLY` capability profile for every launch, regardless of task
type, prompt intent, or any caller-supplied override.

A read-only-forced launch that does not actually need write access still
runs (it just explores/reports rather than mutates) — this is "preparation
of alternatives": the executor can still produce a plan, a diff, or a report
for a human to act on. Only a launch whose prompt plainly demands writing
(`capabilities.prompt_requires_write`) is blocked outright, because a
read-only session cannot honor it. That block is where `CRITICAL_FALLBACKS`
comes in: every task type this codebase treats as high-risk
(`capabilities.WRITE_TASK_TYPES`) has a registered, human-actionable safe
alternative, so a blocked launch's reason always names what to do instead of
just naming what it refused to do. `test_emergency_mode.py`'s
`test_every_write_task_type_has_a_fallback` makes that registration a
regression, not a hope: it fails the moment a new write task type is added
to `capabilities.py` without an accompanying fallback here.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Mapping

from command_center import capabilities

__all__ = [
    "EMERGENCY_MODE_ENV",
    "CRITICAL_FALLBACKS",
    "DEFAULT_FALLBACK",
    "is_active",
    "fallback_for",
    "decide",
]

#: Set to one of `_TRUTHY` (case-insensitive) to force every launch fleet-wide
#: into `PROFILE_READ_ONLY` — the one switch an SRE flips during an incident.
EMERGENCY_MODE_ENV = "AICC_EMERGENCY_MODE"

_TRUTHY = {"1", "true", "yes", "on"}

#: Every high-risk (write-capable) task type -> the safe, read-only-compatible
#: alternative an operator/agent should perform instead while Emergency
#: Conservative Mode is active. Kept in lockstep with
#: `capabilities.WRITE_TASK_TYPES` — see the module docstring and
#: `test_emergency_mode.test_every_write_task_type_has_a_fallback`.
CRITICAL_FALLBACKS: dict[str, str] = {
    "implementation": (
        "Prepare a reviewed implementation plan/diff for a human to apply "
        "manually instead of committing changes automatically."
    ),
    "remediation": (
        "Draft the remediation steps and supporting evidence for human "
        "execution instead of auto-remediating."
    ),
    "reconciliation": (
        "Produce a reconciliation report naming the safe candidates for a "
        "human to action instead of merging, closing, or deleting anything."
    ),
    "migration": (
        "Generate a dry-run migration plan and rollback notes for human "
        "review instead of applying the migration."
    ),
    "repair": (
        "Document the proposed repair and its evidence instead of applying "
        "the repair directly."
    ),
    "integration": (
        "Prepare an integration proposal (config diff, required approvals) "
        "for a human to wire up instead of connecting it live."
    ),
}

#: Fallback for any task type outside `CRITICAL_FALLBACKS` (an unrecognized
#: or future write-capable type) — fail-safe guidance rather than a crash.
DEFAULT_FALLBACK = (
    "Escalate to a human operator with a read-only diagnostic instead of "
    "executing the change."
)


def is_active(env: Mapping[str, str] | None = None) -> bool:
    """`True` when Emergency Conservative Mode is switched on.

    `env` defaults to `os.environ`; an explicit mapping is accepted so tests
    never need to monkeypatch process environment."""
    source = os.environ if env is None else env
    return source.get(EMERGENCY_MODE_ENV, "").strip().lower() in _TRUTHY


def fallback_for(task_type: str) -> str:
    """The safe alternative registered for `task_type`, or `DEFAULT_FALLBACK`
    when none is registered. Never raises and never returns `None` — every
    scenario has a fallback, by construction."""
    return CRITICAL_FALLBACKS.get(task_type, DEFAULT_FALLBACK)


def decide(
    task_type: str,
    prompt: str | None,
    override: str | None = None,
    *,
    active: bool | None = None,
) -> capabilities.CapabilityDecision:
    """Emergency-aware wrapper around `capabilities.decide`.

    When Emergency Conservative Mode is not active (`active` resolves to
    `False`), this is exactly `capabilities.decide(task_type, prompt,
    override)` — no behavior change outside an incident.

    When active, every launch is forced to `PROFILE_READ_ONLY` regardless of
    `override`. A launch whose prompt does not plainly require write access
    still proceeds (read-only exploration/reporting is safe); a launch whose
    prompt does require it is blocked (`ok=False`), and its `reason` has the
    task type's registered safe fallback appended so the block always names
    a safe alternative, never just a refusal.

    `active=None` (the default) reads `is_active()` from the environment;
    pass an explicit bool to keep this call pure in tests.
    """
    resolved_active = is_active() if active is None else active
    if not resolved_active:
        return capabilities.decide(task_type, prompt, override)

    decision = capabilities.decide(task_type, prompt, override=capabilities.PROFILE_READ_ONLY)
    if decision.ok:
        return decision

    fallback = fallback_for(task_type)
    reason = f"{decision.reason} Emergency Conservative Mode is active: {fallback}"
    return dataclasses.replace(decision, reason=reason)
