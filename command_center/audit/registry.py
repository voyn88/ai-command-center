"""Check registry — the engine's extension point.

Checks are registered by name against a zero-arg factory. New signal sources (a
real SAST scanner, a licence auditor, a bespoke rule) slot in by registering a
factory — no change to the runner, the write service or the API. The runner asks
the registry to instantiate either the full default set or a named subset for a
single pass. Modelled on the advisor's ``CollectorRegistry`` so the two engines
share one obvious extension shape.
"""

from __future__ import annotations

from typing import Callable

from command_center.audit.checks.base import Check
from command_center.audit.checks.code_quality import CodeQualityCheck
from command_center.audit.checks.coverage import CoverageCheck
from command_center.audit.checks.deps import DepsCheck
from command_center.audit.checks.lint import LintCheck
from command_center.audit.checks.security import SecurityCheck

CheckFactory = Callable[[], Check]


class CheckRegistry:
    """An ordered name→factory map. Registration order is the pass order, giving
    one deterministic sequence of checks rather than a hash-order surprise."""

    def __init__(self) -> None:
        self._factories: dict[str, CheckFactory] = {}

    def register(
        self, name: str, factory: CheckFactory, *, replace: bool = False
    ) -> None:
        """Register ``factory`` under ``name``. Refuses to clobber an existing
        name unless ``replace=True`` — so a typo can't silently shadow a
        built-in, while a deliberate override stays a one-liner."""
        if not name or not name.strip():
            raise ValueError("check name must be non-empty")
        if name in self._factories and not replace:
            raise ValueError(
                f"check {name!r} already registered; pass replace=True to override"
            )
        self._factories[name] = factory

    def names(self) -> list[str]:
        """Registered check names, in registration order."""
        return list(self._factories)

    def create(self, names: list[str] | None = None) -> list[Check]:
        """Instantiate checks. ``names=None`` builds the full set in registration
        order; a subset builds exactly those, in the order given. An unknown name
        raises rather than being silently skipped."""
        wanted = self.names() if names is None else names
        checks: list[Check] = []
        for name in wanted:
            try:
                factory = self._factories[name]
            except KeyError:
                raise KeyError(
                    f"unknown check {name!r}; registered: {self.names()}"
                ) from None
            checks.append(factory())
        return checks


def default_registry() -> CheckRegistry:
    """A fresh registry with the built-in in-repo checks registered in a stable
    order (security first — the highest-signal family)."""
    registry = CheckRegistry()
    registry.register(SecurityCheck.name, SecurityCheck)
    registry.register(LintCheck.name, LintCheck)
    registry.register(CodeQualityCheck.name, CodeQualityCheck)
    registry.register(DepsCheck.name, DepsCheck)
    registry.register(CoverageCheck.name, CoverageCheck)
    return registry
