"""The :class:`Collector` interface every advisor signal source implements.

A collector is a pure producer: given a read-only
:class:`~command_center.advisor.types.CollectorContext` it returns a list of
:class:`~command_center.advisor.types.Candidate` value objects and does nothing
else — no persistence, no event publishing, no scoring. That keeps a collector
trivial to test (hand it a context, assert on the candidates) and lets the
:class:`~command_center.advisor.service.AdvisorService` own the cross-cutting
concerns (dedup, scoring, auto-rules, persistence) exactly once.

``name`` is the collector's unique registry key; ``kind`` is the
``advisor_proposal`` kind every candidate it emits carries. They coincide for the
built-in collectors but are kept distinct so a future collector could emit a kind
that differs from its registry name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from command_center.advisor.types import Candidate, CollectorContext


class Collector(ABC):
    """Abstract base for every advisor collector."""

    #: Unique registry key (see :mod:`command_center.advisor.registry`).
    name: ClassVar[str]
    #: The ``advisor_proposal`` kind candidates from this collector carry.
    kind: ClassVar[str]

    @abstractmethod
    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        """Produce candidate proposals from the signals in ``ctx``. Must return a
        (possibly empty) list and must not mutate any store. An empty list is a
        first-class, expected result — "no signal worth a proposal right now"."""
        raise NotImplementedError
