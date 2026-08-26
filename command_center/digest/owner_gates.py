"""Configuration for what auto-fills «Мой день» (the owner's action list).

An *owner-gate* is a rule that says "this class of event is the owner's to
clear, not a delegate's". The auto-fill subscriber (:mod:`command_center.digest.
owner_autofill`) consults this config to decide whether an incoming domain event
should become an :class:`OwnerItem`.

Kept as data (not hard-coded predicates) so an operator can widen or narrow the
gates without touching the subscriber. Defaults are deliberately conservative
and carry **no** real project names — this is a public repo; operators point the
project gate at their own codes at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class OwnerGateConfig:
    """The owner-gate policy the auto-fill reads.

    * ``gate_projects`` — proposal projects the owner must personally clear: a
      new proposal in one of these becomes an owner item (the "owner-gate from a
      config list").
    * ``incident_severities`` — incident severities that page the owner
      (non-delegable); an incident at one of these opens an owner item.
    * ``promotions_are_owner_items`` — whether a proposal the owner *took*
      (promoted to a task) also lands on the day list as a follow-up. On by
      default: promotion is an explicit owner action.
    """

    gate_projects: frozenset[str] = field(default_factory=frozenset)
    incident_severities: frozenset[str] = field(
        default_factory=lambda: frozenset({"sev1", "sev2"})
    )
    promotions_are_owner_items: bool = True

    def with_gate_projects(self, projects: frozenset[str] | set[str]) -> "OwnerGateConfig":
        """Return a copy gated on ``projects`` (operator wiring / tests)."""
        return replace(self, gate_projects=frozenset(projects))


#: The process default. Empty project gate (an operator opts specific codes in);
#: sev1/sev2 incidents page the owner; promotions land as follow-ups.
DEFAULT_OWNER_GATES = OwnerGateConfig()
