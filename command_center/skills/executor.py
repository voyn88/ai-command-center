"""The injectable materialisation/isolation seam for the acquire path
(mirrors ``command_center.marketplace.installer.Installer`` exactly).

Acquiring a real skill eventually means pulling a verified artefact (the
exact pinned version+hash) into an isolated sandbox with no ambient network,
no secrets, and no push credential -- the supply-chain-safety spine the owner
idea calls the "key danger that defines the whole design". What this wave
nails down is the seam: ``service.acquire_skill`` never materialises anything
itself, it delegates to an injected :class:`SkillExecutor`, whose single
:meth:`SkillExecutor.acquire` method turns a candidate into a
:class:`SkillExecutionOutcome`. The lifecycle transition and its audit-log
line are produced by the service around that call and are entirely real;
only the act of materialising the skill sits behind the seam.

Safety note (real sandboxing is a later wave)
----------------------------------------------
The default :class:`NullSkillExecutor` deliberately performs **no** network
access, reads **no** secret, and requests **no** push/write credential: it
records intent and returns. Tests inject it (or a recording double), so the
suite exercises the true lifecycle + audit trail without ever downloading or
running third-party content. A future wave adds a real executor that unpacks
a hash-verified artefact into an OS-level sandbox (container/gVisor, no
ambient filesystem/network/credential authority, resource and time limits);
until then, wiring a network- or secret-touching executor into this seam is
out of scope and intentionally absent. Because the executor is injected, that
future implementation drops in without touching the service, the repository,
or the API contract -- exactly as the marketplace's ``Installer`` seam was
built to allow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from command_center.api import models


@dataclass(frozen=True, slots=True)
class SkillExecutionOutcome:
    """What a :class:`SkillExecutor` reports back for the acquisition-log
    line. Neither ``detail`` nor ``metadata`` is trusted to change the
    lifecycle -- the service decides the transition; the executor only
    describes what it did."""

    detail: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SkillExecutor(Protocol):
    """The seam the acquire path materialises *through*."""

    #: A stable, human-readable name recorded on every acquisition-log line
    #: (the ``executor`` column) so a test double and a future real
    #: implementation are distinguishable in the trail.
    name: str

    def acquire(self, skill: models.SkillItem) -> SkillExecutionOutcome:
        """Materialise ``skill`` (pinned by ``version``+``content_hash``) in
        isolation and return a :class:`SkillExecutionOutcome` describing it.
        Raising signals a real failure; the service then leaves the skill
        ``candidate`` and writes no log line."""
        ...


@dataclass(frozen=True, slots=True)
class NullSkillExecutor:
    """The safe default: records intent, touches no network, no secret, no
    push credential.

    This is what makes ``service.acquire_skill`` safe to call with no
    executor configured -- the lifecycle transition and the audit log around
    it are real, while the act of materialising third-party content stays a
    no-op until the sandboxing wave lands (see the module docstring's safety
    note)."""

    name: str = "null-skill-executor"

    def acquire(self, skill: models.SkillItem) -> SkillExecutionOutcome:
        return SkillExecutionOutcome(
            detail=(
                f"isolated no-op acquisition of {skill.kind} {skill.name!r} "
                f"v{skill.version} ({skill.content_hash[:12]}...)"
            ),
            metadata={
                "mode": "null",
                "network": "denied",
                "secrets": "denied",
                "push": "denied",
            },
        )
