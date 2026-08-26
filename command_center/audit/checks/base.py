"""The :class:`Check` interface every audit check implements.

A check is a pure producer: given a read-only
:class:`~command_center.audit.types.CheckContext` it returns a list of
:class:`~command_center.audit.types.Finding` value objects and does nothing else
— no persistence, no event publishing. That keeps a check trivial to test (hand
it a context, assert on the findings) and lets the write service
(:mod:`command_center.api.audit_service`) own the cross-cutting concerns (dedup,
persistence, status/owner enforcement, events) exactly once.

``name`` is the check's unique registry key; ``category`` is the
``audit_finding`` category every finding it emits carries.

Robustness contract: a check must **never raise** because its underlying tool is
absent or misbehaves. Reusing external tooling (ruff, coverage data) is a
best-effort signal — when the tool cannot run, the check returns an empty list
(or a single ``info`` finding describing the gap), so one unavailable tool never
fails the whole audit pass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from command_center.audit.types import CheckContext, Finding


class Check(ABC):
    """Abstract base for every audit check."""

    #: Unique registry key (see :mod:`command_center.audit.registry`).
    name: ClassVar[str]
    #: The ``audit_finding`` category findings from this check carry.
    category: ClassVar[str]

    @abstractmethod
    def run(self, ctx: CheckContext) -> list[Finding]:
        """Produce findings from the signals reachable via ``ctx``. Must return a
        (possibly empty) list and must not mutate any store. An empty list is a
        first-class, expected result — "nothing worth a finding right now"."""
        raise NotImplementedError
