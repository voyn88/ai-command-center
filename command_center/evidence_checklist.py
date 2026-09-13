"""Fail-closed evidence-sufficiency checklist for critical operations.

A critical operation -- one that mutates a host, a repository, or a service
in a way that is expensive or impossible to undo -- may proceed only once
every item on its checklist is affirmatively satisfied. There is no
default-allow: an operation with no registered checklist, or a checklist
item that is missing, unknown, or merely unset, refuses rather than
proceeds. Evidence is never inferred from the absence of a problem; it must
be supplied.

This module only evaluates evidence. It never performs the operation it
gates, and it never inspects live state on its own -- the caller gathers
evidence and hands it over, the same separation `command_center.delivery_gate`
uses for delivery decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ChecklistItem:
    """One named piece of evidence and whether it was satisfied."""

    name: str
    satisfied: bool
    detail: str = ""


@dataclass(frozen=True)
class ChecklistDecision:
    operation: str
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_checklist(
    operation: str, items: Iterable[ChecklistItem]
) -> ChecklistDecision:
    """Evaluate immutable evidence without performing `operation`.

    An operation with an empty checklist is not "trivially safe" -- it is
    unproven, so it fails closed exactly like a checklist with unsatisfied
    items. Every unsatisfied item is reported, not just the first, so a
    caller sees the whole gap in one refusal instead of fixing evidence one
    round-trip at a time.
    """
    observed = list(items)
    if not observed:
        return ChecklistDecision(
            operation=operation,
            allowed=False,
            reasons=(f"{operation}: no_checklist_defined",),
        )
    reasons = tuple(
        f"{item.name}: {item.detail}" if item.detail else item.name
        for item in observed
        if not item.satisfied
    )
    return ChecklistDecision(operation=operation, allowed=not reasons, reasons=reasons)


class EvidenceInsufficient(RuntimeError):
    """Raised by `require_sufficient_evidence` when a checklist refuses."""

    def __init__(self, decision: ChecklistDecision):
        self.decision = decision
        joined = "; ".join(decision.reasons) or "no evidence recorded"
        super().__init__(
            f"critical operation {decision.operation!r} refused for "
            f"insufficient evidence: {joined}"
        )


def require_sufficient_evidence(
    operation: str, items: Iterable[ChecklistItem]
) -> ChecklistDecision:
    """Evaluate `operation`'s checklist and raise unless every item passed."""
    decision = evaluate_checklist(operation, items)
    if not decision.allowed:
        raise EvidenceInsufficient(decision)
    return decision


def checklist_from_registry(
    operation: str,
    registry: Mapping[str, tuple[str, ...]],
    evidence: Mapping[str, object],
) -> tuple[ChecklistItem, ...]:
    """Build checklist items for `operation` from a static required-item
    registry and a mapping of item name to supplied evidence.

    An item absent from `evidence`, or present with a false/blank value, is
    unsatisfied -- the registry defines what proof a critical operation
    needs, but nothing here ever infers that proof was given. A string value
    is treated as the proof detail itself (satisfied iff non-blank); any
    other value is satisfied iff truthy.
    """
    items = []
    for name in registry.get(operation, ()):
        value = evidence.get(name)
        if isinstance(value, str):
            items.append(ChecklistItem(name, bool(value.strip()), value))
        else:
            items.append(ChecklistItem(name, bool(value)))
    return tuple(items)
