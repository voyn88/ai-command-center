"""The injectable installer boundary for the Marketplace install path.

Installing a real module/add-on eventually means fetching an artefact and
running it inside a sandbox. That is a *later* wave. What this wave nails down
is the **seam**: the service never installs anything itself — it delegates to an
:class:`Installer`, an injected object whose single :meth:`Installer.install`
method turns a listing into an :class:`InstallOutcome`. The lifecycle change and
its audit log are produced by the service around that call and are entirely
real; only the act of materialising code is behind the seam.

Safety note (real sandboxing is a later wave)
---------------------------------------------
The default :class:`NullInstaller` deliberately performs **no** code execution
and **no** network/disk fetch: it records intent and returns. Tests inject it
(or a recording double), so the suite exercises the true lifecycle + log without
ever downloading or running anything. A future wave will add a real installer
that unpacks a verified, signed artefact into an OS-level sandbox with no ambient
filesystem/network authority; until then, wiring a code-executing installer into
this seam is out of scope and intentionally absent. Because the installer is
injected, that future implementation drops in without touching the service, the
repository, or the API contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from command_center.api import models


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What an :class:`Installer` reports back for the audit trail.

    ``detail`` is a short human-readable note ("resolved from channel X",
    "dry-run") and ``metadata`` any structured facts the installer wants
    preserved on the log line (kept to plain strings for this wave). Neither is
    trusted to change the lifecycle — the service decides the transition; the
    installer only describes what it did.
    """

    detail: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Installer(Protocol):
    """The seam the service installs *through*.

    Implementations must be side-effect-honest: whatever they do (nothing, in
    this wave) they summarise in the returned :class:`InstallOutcome`. They must
    not mutate the listing or touch the store — persistence and the lifecycle
    transition are the service's job.
    """

    #: A stable, human-readable name recorded on every install-log line so a
    #: test double and a future real installer are distinguishable in the trail.
    name: str

    def install(self, item: models.MarketItem) -> InstallOutcome:
        """Perform the (wave-dependent) installation work for ``item`` and
        return an :class:`InstallOutcome` describing it. Raising signals a real
        failure; the service then leaves the listing ``listed`` and writes no
        log line."""
        ...


@dataclass(frozen=True, slots=True)
class NullInstaller:
    """The safe default: records intent, executes nothing, fetches nothing.

    This is what makes the install path testable and safe by default — the
    lifecycle transition and the audit log around it are real, while the act of
    materialising code stays a no-op until the sandboxing wave lands (see the
    module docstring's safety note)."""

    name: str = "null-installer"

    def install(self, item: models.MarketItem) -> InstallOutcome:
        return InstallOutcome(
            detail=f"no-op install of {item.kind} {item.name!r} v{item.version or '0'}",
            metadata={"mode": "null", "safe": "true"},
        )
