"""The injectable candidate-discovery seam (mirrors
``command_center.marketplace.installer.Installer``).

Forming a capability request and then actually reaching out into an MCP
registry, a Claude Agent Skill catalogue, a CLI-tool index, or a repository's
own ADR/runbook docs is the "search the whole available surface" half of
autonomous capability acquisition -- and it is exactly the half that must
never run unsupervised over untrusted network content inside an autonomous
pipeline that can write to the repository and open PRs. So, same as
``Installer`` did for the marketplace's fetch-and-run step: this wave nails
the **seam**, not the fetch. The default :class:`NullCandidateFinder`
performs no network access and returns no candidates; a later wave plugs in a
real finder per source kind, and because the finder is injected, that drops
in without touching :mod:`command_center.skills.service` or the API contract.

A finder is only ever handed ``approved`` :class:`SkillSource` rows -- see
``service.find_candidates``, which filters before the finder is called. A
finder cannot see a ``proposed`` or ``revoked`` source no matter what it asks
for; the allowlist is enforced by the caller, not by finder discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """Historical evidence about a candidate, if any exists.

    ``success_rate`` is the fraction (0..1) of prior tasks using this exact
    candidate whose change was accepted; ``avg_cost``/``avg_latency_seconds``
    are per-attempt averages. All three must come from real measurement (the
    source's own published stats, or this registry's own
    ``skill_outcome`` history for a previously-acquired candidate with the
    same content hash) -- never invented to make a candidate look scored.
    """

    success_rate: float
    avg_cost: float
    avg_latency_seconds: float


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    """One thing a finder proposes as able to fill a capability need."""

    name: str
    kind: str
    version: str
    content_hash: str
    source_id: str
    provenance: str = ""
    metrics: CandidateMetrics | None = None


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """A formal record of what capability is missing, formed by
    ``service.request_capability`` before any source is ever searched."""

    task_id: str
    task_class: str
    need: str
    requested_by: str
    requested_at: str


@runtime_checkable
class CandidateFinder(Protocol):
    """The seam candidate discovery runs *through*. Implementations must be
    side-effect-honest and must never propose a candidate whose ``source_id``
    is not one of the ``sources`` they were handed."""

    #: A stable, human-readable name so a real finder is distinguishable from
    #: the null default in anything that logs which finder ran.
    name: str

    def find(
        self, request: CapabilityRequest, sources: list[dict]
    ) -> list[CandidateProposal]:
        """Propose candidates for ``request`` drawn only from ``sources``
        (already filtered to ``approved`` rows by the caller)."""
        ...


@dataclass(frozen=True, slots=True)
class NullCandidateFinder:
    """The safe default: performs no network access, proposes nothing.

    This is what makes ``service.find_candidates`` safe to call with no
    finder configured -- the request is still formed and recorded, but
    nothing is fetched until a later wave wires in a real, source-kind-
    specific finder behind this same seam."""

    name: str = "null-finder"

    def find(
        self, request: CapabilityRequest, sources: list[dict]
    ) -> list[CandidateProposal]:
        return []
