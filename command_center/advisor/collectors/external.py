"""Trend / competitor / UX collectors: extensible external-signal slots.

These three surfaces are, by design, driven by signals that live *outside* the
repo (market trends, competitor moves, usability research). This starter ships
**no** external source for them — and rather than fake one, each collector takes
an optional :class:`ExternalSignalSource`. When none is configured the collector
yields zero candidates cleanly; when a real source is injected later (via the
registry or the collector context) the same collector emits real candidates with
no rewrite. That is the extensibility contract: the seam exists now, the data
plugs in later, and nothing here pretends to have data it does not.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from command_center.advisor.collectors.base import Collector
from command_center.advisor.types import Candidate, CollectorContext


@runtime_checkable
class ExternalSignalSource(Protocol):
    """A pluggable source of candidates for an external-signal collector. A real
    implementation might read a curated trends file, a competitor-watch export or
    a UX-research backlog and return candidates; it is injected, never hardcoded."""

    def fetch(self, ctx: CollectorContext) -> list[Candidate]:
        ...


class ExternalSignalCollector(Collector):
    """Base for collectors whose signal comes from an injected external source.

    Resolution order for the source: the one passed to the constructor, else one
    supplied under ``"{name}_source"`` in :attr:`CollectorContext.options`. With
    neither present the collector returns ``[]`` — the honest "no external source
    configured" path, not a stub that fabricates proposals."""

    def __init__(self, source: ExternalSignalSource | None = None) -> None:
        self._source = source

    def _resolve_source(self, ctx: CollectorContext) -> ExternalSignalSource | None:
        if self._source is not None:
            return self._source
        candidate = ctx.options.get(f"{self.name}_source")
        return candidate if isinstance(candidate, ExternalSignalSource) else None

    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        source = self._resolve_source(ctx)
        if source is None:
            return []
        # Trust the source's kind intent, but stamp provenance so a mixed batch
        # is still traceable back to this collector.
        return [
            Candidate(
                kind=self.kind,
                title=c.title,
                project_ref=c.project_ref,
                body=c.body,
                dedup_key=c.dedup_key,
                signals=c.signals,
                source=self.name,
            )
            for c in source.fetch(ctx)
        ]


class TrendCollector(ExternalSignalCollector):
    name: ClassVar[str] = "trend"
    kind: ClassVar[str] = "trend"


class CompetitorCollector(ExternalSignalCollector):
    name: ClassVar[str] = "competitor"
    kind: ClassVar[str] = "competitor"


class UxCollector(ExternalSignalCollector):
    name: ClassVar[str] = "ux"
    kind: ClassVar[str] = "ux"
