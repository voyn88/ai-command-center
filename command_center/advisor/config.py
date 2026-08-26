"""Auto-rules: whether a scored candidate becomes a draft or is auto-promoted.

The engine's default posture is **conservative**: with no rules configured every
candidate is persisted as a draft proposal (``status='new'``) that waits in the
Советник inbox for the owner to accept or dismiss. Auto-promotion — creating a
board task straight from a candidate, no human in the loop — happens only when an
explicit :class:`AutoRule` matches, so turning it on is a deliberate,
per-deployment decision rather than a default the owner has to notice and switch
off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from command_center.advisor.scorer import Score
from command_center.advisor.types import Candidate

Action = Literal["draft", "promote"]


@dataclass(frozen=True, slots=True)
class AutoRule:
    """One auto-promotion rule. A candidate is auto-promoted when its ``kind`` is
    in ``kinds`` (empty ``kinds`` means *any* kind) **and** its scored
    ``priority`` is at least ``min_priority``. Rules are evaluated in order and
    the first match decides — a rule can only ever *raise* a candidate to
    ``promote``; the absence of a match leaves it a ``draft``."""

    kinds: frozenset[str] = frozenset()
    min_priority: float = 1.0

    def matches(self, candidate: Candidate, score: Score) -> bool:
        if self.kinds and candidate.kind not in self.kinds:
            return False
        return score.priority >= self.min_priority


@dataclass(frozen=True, slots=True)
class AdvisorConfig:
    """Engine policy. ``auto_rules`` is empty by default, so :meth:`decide`
    returns ``"draft"`` for everything — the conservative default. ``dedup_statuses``
    is the set of existing-proposal statuses a new candidate is deduplicated
    against (open states only: a dismissed/converted proposal never blocks a
    fresh raise of the same signal)."""

    auto_rules: tuple[AutoRule, ...] = ()
    dedup_statuses: tuple[str, ...] = ("new", "accepted")
    max_candidates_per_pass: int = 200

    def decide(self, candidate: Candidate, score: Score) -> Action:
        """Return ``"promote"`` if any rule matches, else ``"draft"``."""
        for rule in self.auto_rules:
            if rule.matches(candidate, score):
                return "promote"
        return "draft"


#: The default, conservative policy (no auto-promotion).
DEFAULT_CONFIG = AdvisorConfig()
