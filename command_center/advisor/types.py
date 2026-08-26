"""Value objects shared across the Советник (advisor) engine.

The engine is a pipeline: **collectors** turn local, free signals into
:class:`Candidate` value objects; a :class:`~command_center.advisor.scorer.ProposalScorer`
grades each candidate; the :class:`~command_center.advisor.service.AdvisorService`
dedups them, applies the auto-rules and persists the survivors as
``advisor_proposal`` rows through the Wave-1 repository.

Nothing here touches storage or the network — these are plain, immutable
dataclasses so a collector, the scorer and the service can all be exercised in
isolation with hand-built inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


def normalize_title(title: str) -> str:
    """Case- and whitespace-insensitive normalization used to build a
    candidate's dedup signature. Two candidates whose titles differ only in
    spacing or case collapse onto the same key and are treated as duplicates."""
    return " ".join(str(title).strip().lower().split())


@dataclass(frozen=True, slots=True)
class Candidate:
    """A proposal a collector wants to raise, before scoring/persistence.

    ``signals`` is a small map of ``0.0..1.0`` measurements the scorer reads
    (``impact``, ``frequency``, ``effort``, ``risk``); a collector supplies
    whichever it can measure and the scorer defaults the rest. ``dedup_key`` is
    an optional stable signature — when empty the service derives one from
    ``kind``/``project_ref``/normalized ``title`` so two passes over the same
    unchanged signal never raise the same proposal twice. ``source`` records the
    originating collector for provenance and is **never** persisted onto the row.
    """

    kind: str
    title: str
    project_ref: str
    body: str = ""
    dedup_key: str = ""
    signals: Mapping[str, float] = field(default_factory=dict)
    source: str = ""

    def signature(self) -> str:
        """The dedup signature: ``dedup_key`` if the collector set one, else a
        deterministic key over kind, project and normalized title."""
        if self.dedup_key:
            return self.dedup_key
        return f"{self.kind}|{self.project_ref}|{normalize_title(self.title)}"


@dataclass(frozen=True, slots=True)
class CollectorContext:
    """Read-only inputs handed to every collector on a pass.

    ``db_path`` is an already-migrated runtime db path — collectors read local
    signals through the ``command_center.runtime.db`` facade functions (never raw
    SQL). ``options`` is a free-form bag the caller can use to inject an external
    signal source for the trend/competitor/ux collectors without touching code.
    """

    root: Path
    db_path: Path
    now: datetime = field(default_factory=datetime.now)
    max_runs: int = 500
    options: Mapping[str, Any] = field(default_factory=dict)
