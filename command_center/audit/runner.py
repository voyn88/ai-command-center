"""AuditRunner — run a set of checks over one target and collect findings.

The runner is the pure domain core of the Audit engine: it instantiates the
requested checks from a :class:`~command_center.audit.registry.CheckRegistry`,
runs each over a :class:`~command_center.audit.types.CheckContext`, dedups the
combined output by signature and returns the surviving
:class:`~command_center.audit.types.Finding` value objects. It **persists
nothing** — the write service (:mod:`command_center.api.audit_service`) owns
persistence, status/owner enforcement, redaction and events, exactly once.

Keeping the runner free of storage means it can be exercised with hand-built
checks and asserted on directly, and it composes cleanly under the write
service the same way the advisor's collectors compose under ``AdvisorService``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from command_center.audit.registry import CheckRegistry, default_registry
from command_center.audit.types import CheckContext, Finding


@dataclass(frozen=True, slots=True)
class CollectResult:
    """The outcome of one collection pass over a target."""

    checks: list[str]
    findings: list[Finding] = field(default_factory=list)
    deduped: int = 0


class AuditRunner:
    """Runs check passes over a :class:`CheckRegistry`."""

    def __init__(self, *, registry: CheckRegistry | None = None) -> None:
        self._registry = registry or default_registry()

    @property
    def registry(self) -> CheckRegistry:
        return self._registry

    def collect(
        self, ctx: CheckContext, *, checks: list[str] | None = None
    ) -> CollectResult:
        """Run the requested checks (default: all registered) over ``ctx`` and
        return the deduped findings. Two findings with the same signature — even
        from different checks — collapse to the first seen, so the same issue is
        never raised twice in one pass."""
        # The registry keys actually run, in order — the requested subset, or the
        # full registered set. Reported verbatim so it matches the ``checks`` the
        # write service records on the run row (the registry key is the stable
        # identifier, distinct from a check instance's ``.name``).
        wanted = list(checks) if checks is not None else self._registry.names()
        check_objs = self._registry.create(checks)
        seen: set[str] = set()
        findings: list[Finding] = []
        deduped = 0
        for check in check_objs:
            for finding in check.run(ctx):
                signature = finding.signature()
                if signature in seen:
                    deduped += 1
                    continue
                seen.add(signature)
                findings.append(finding)
        return CollectResult(
            checks=wanted,
            findings=findings,
            deduped=deduped,
        )
