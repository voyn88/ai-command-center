"""The injectable executor boundary for skill acquisition.

Materialising a real skill (an MCP server, an Agent Skill bundle, a CLI tool)
eventually means fetching an artefact from an external, untrusted source and
running it. That is exactly the supply-chain surface the owner idea calls out:
a downloaded "skill" is untrusted *data*, not instructions, and must never be
handed network access, secrets, or push rights, nor be allowed to influence
the acceptance verdict of the pipeline that fetched it.

What this module nails down is the **seam**, the same shape as
:mod:`command_center.marketplace.installer`: the service never acquires
anything itself — it delegates to a :class:`SkillExecutor`, an injected object
whose single :meth:`SkillExecutor.acquire` method turns a candidate
:class:`~command_center.api.models.SkillItem` into an
:class:`AcquisitionOutcome`. The lifecycle transition (claim, then
acquired/failed) and its audit log are produced by the service around that
call and are entirely real; only the act of materialising the skill sits
behind the seam.

Safety note (real sandboxing is a later wave)
----------------------------------------------
The default :class:`NullSkillExecutor` deliberately performs **no** code
execution, and **no** network/disk fetch: it records intent and returns.
Tests inject it (or a recording double), so the suite exercises the true
claim -> acquire -> finalize lifecycle and its audit trail without ever
downloading or running anything. A future wave will add a real executor that
unpacks a verified, signed, version+hash-pinned artefact into an isolated
sandbox with no ambient filesystem/network/secret authority; until then,
wiring a code-executing or network-reaching executor into this seam is out of
scope and intentionally absent. Because the executor is injected, that future
implementation drops in without touching the service, the repository, or the
API contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from command_center.api import models


@dataclass(frozen=True, slots=True)
class AcquisitionOutcome:
    """What a :class:`SkillExecutor` reports back for the acquisition log.

    ``detail`` is a short human-readable note ("verified pinned hash",
    "dry-run") and ``metadata`` any structured facts the executor wants
    preserved on the log line (kept to plain strings for this wave). Neither
    is trusted to change the lifecycle — the service decides the transition;
    the executor only describes what it did.
    """

    detail: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SkillExecutor(Protocol):
    """The seam the service acquires *through*.

    Implementations must be side-effect-honest: whatever they do (nothing, in
    this wave) they summarise in the returned :class:`AcquisitionOutcome`.
    They must not mutate the item or touch the store — persistence and the
    lifecycle transition are the service's job. Raising signals a real
    acquisition failure; the service reverts the claim to ``candidate`` and
    logs the failure rather than leaving the item stuck ``acquiring``.
    """

    #: A stable, human-readable name recorded on every acquisition-log line so
    #: a test double and a future real executor are distinguishable in the
    #: trail.
    name: str

    def acquire(self, item: models.SkillItem) -> AcquisitionOutcome:
        """Materialise ``item`` (wave-dependent) and return an
        :class:`AcquisitionOutcome` describing it."""
        ...


@dataclass(frozen=True, slots=True)
class NullSkillExecutor:
    """The safe default: records intent, executes nothing, fetches nothing.

    This is what makes the acquisition path testable and safe by default — the
    claim/finalize lifecycle and the audit log around it are real, while the
    act of materialising a skill stays a no-op until the sandboxing wave lands
    (see the module docstring's safety note)."""

    name: str = "null-skill-executor"

    def acquire(self, item: models.SkillItem) -> AcquisitionOutcome:
        return AcquisitionOutcome(
            detail=(
                f"no-op acquisition of {item.kind} {item.name!r} "
                f"v{item.version or '0'} ({item.content_hash[:12]})"
            ),
            metadata={"mode": "null", "network": "denied", "safe": "true"},
        )
