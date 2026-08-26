"""Collector registry — the engine's extension point.

Collectors are registered by name against a zero-arg factory. New signal
sources (a real trend feed, a competitor watch, a web collector) slot in by
registering a factory — no change to the scorer, the service or the API. The
service asks the registry to instantiate either the full default set or a named
subset for a single pass.
"""

from __future__ import annotations

from typing import Callable

from command_center.advisor.collectors.base import Collector
from command_center.advisor.collectors.external import (
    CompetitorCollector,
    TrendCollector,
    UxCollector,
)
from command_center.advisor.collectors.feedback import FeedbackCollector
from command_center.advisor.collectors.optimization import OptimizationCollector

CollectorFactory = Callable[[], Collector]


class CollectorRegistry:
    """An ordered name→factory map. Registration order is the pass order, giving
    one deterministic sequence of collectors rather than a hash-order surprise."""

    def __init__(self) -> None:
        self._factories: dict[str, CollectorFactory] = {}

    def register(
        self, name: str, factory: CollectorFactory, *, replace: bool = False
    ) -> None:
        """Register ``factory`` under ``name``. Refuses to clobber an existing
        name unless ``replace=True`` — so a typo can't silently shadow a
        built-in, while a deliberate override (e.g. a trend collector wired to a
        real source) stays a one-liner."""
        if not name or not name.strip():
            raise ValueError("collector name must be non-empty")
        if name in self._factories and not replace:
            raise ValueError(f"collector {name!r} already registered; pass replace=True to override")
        self._factories[name] = factory

    def names(self) -> list[str]:
        """Registered collector names, in registration order."""
        return list(self._factories)

    def create(self, names: list[str] | None = None) -> list[Collector]:
        """Instantiate collectors. ``names=None`` builds the full set in
        registration order; a subset builds exactly those, in the order given.
        An unknown name raises rather than being silently skipped."""
        wanted = self.names() if names is None else names
        collectors: list[Collector] = []
        for name in wanted:
            try:
                factory = self._factories[name]
            except KeyError:
                raise KeyError(f"unknown collector {name!r}; registered: {self.names()}") from None
            collectors.append(factory())
        return collectors


def default_registry() -> CollectorRegistry:
    """A fresh registry with the built-in local collectors registered in a
    stable order. Trend/competitor/ux are registered too — with no external
    source they contribute zero candidates until one is wired in."""
    registry = CollectorRegistry()
    registry.register(OptimizationCollector.name, OptimizationCollector)
    registry.register(FeedbackCollector.name, FeedbackCollector)
    registry.register(TrendCollector.name, TrendCollector)
    registry.register(CompetitorCollector.name, CompetitorCollector)
    registry.register(UxCollector.name, UxCollector)
    return registry
