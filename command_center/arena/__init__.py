"""Арена — the agent-duel/competition engine.

Pits at least three independent solution variants for the same case against
one another and ranks them by explainability, quality, time, cost and
correctness (the acceptance criteria verbatim). Pure and storage-free, like
the Советник (advisor) engine: a caller owns persisting a
:class:`~command_center.arena.service.DuelResult`.

Public surface::

    Case, SolutionVariant                     -- the case + one contestant's attempt
    DuelScorer, VariantScore, TooFewVariantsError  -- five-axis scoring
    DuelService, DuelResult, RankedVariant     -- run a duel, get a ranking + rationale
    run_duel                                   -- the API entry point (wire in, wire out)
"""

from __future__ import annotations

from command_center.arena.api import run_duel
from command_center.arena.scorer import DuelScorer, TooFewVariantsError, VariantScore
from command_center.arena.service import DuelResult, DuelService, RankedVariant
from command_center.arena.types import Case, SolutionVariant

__all__ = [
    "Case",
    "SolutionVariant",
    "DuelScorer",
    "VariantScore",
    "TooFewVariantsError",
    "DuelService",
    "DuelResult",
    "RankedVariant",
    "run_duel",
]
