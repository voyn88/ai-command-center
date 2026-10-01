"""Reputation model: a participant's trust score, from vote quality and
influence on outcomes (VOYN-MIN-LINK-REPUTE).

The Council already records every :class:`~command_center.api.models.Vote` and,
once a motion closes, the immutable :class:`~command_center.api.models.Decision`
it produced. This module turns that history into a **trust score** — a number
that says how much a voter's ballot is worth trusting — built from two signals
that are both derivable from data already on record, so nothing here invents a
new source of truth:

* **quality (alignment)** — did the voter's ``choice`` end up on the side the
  Board actually decided? A voter who consistently votes with the eventual
  outcome has demonstrated judgement; one who consistently votes against it
  has not. ``abstain`` and a ``deferred`` outcome are scored neutral — neither
  side "won", so there is nothing to have agreed or disagreed with.
* **influence** — did the voter's ballot *matter* to that outcome? A vote is
  **pivotal** when removing it from the tally would have changed the outcome
  category (approved/rejected/deferred); a pivotal vote gets full influence
  credit. A non-pivotal decisive vote still gets partial credit, shared out
  over every decisive (yes/no) vote on that motion — the more voters who agreed,
  the less any single one of them tipped the scale. ``abstain`` never
  influences a tally by construction.

Every score this module returns carries a ``basis`` and a plain-language
``explanation`` — the acceptance this feature exists to satisfy is that a vote's
trust score is *explainable*, not just a number:

* ``"outcome_alignment"`` — the vote's own motion has a decision; the score is
  computed directly from how that vote aligned with, and influenced, that
  outcome.
* ``"voter_prior"`` — the vote's motion is still open (no outcome to compare
  against yet), so the score falls back to the voter's own historical average
  across their other decided votes.
* ``"insufficient_data"`` — neither is available: the motion is undecided *and*
  the voter has no decided votes yet. ``score`` is ``None`` here; this is the
  only basis :func:`reputation_coverage` counts against the 90% acceptance bar,
  because it is the only case with nothing on record to explain a number from.

Pure functions throughout: every input is a plain dict shaped like the joined
row :func:`command_center.runtime.db.council.list_votes_with_outcomes` returns
(``choice``, ``motion_id``, ``decision_outcome``, ``decision_tally``), so this
module has no db/service dependency and is trivial to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

Basis = Literal["outcome_alignment", "voter_prior", "insufficient_data"]

#: The winning side's `choice` value for a decisive outcome; `None` for
#: `deferred`, where no side won.
_WINNING_CHOICE: dict[str, str | None] = {
    "approved": "yes",
    "rejected": "no",
    "deferred": None,
}

#: Weights blending alignment (quality) and influence into one 0-100 score.
#: Alignment is weighted higher: a voter's judgement (did they call it right)
#: matters more to trustworthiness than how close any single motion happened
#: to be.
_ALIGNMENT_WEIGHT = 0.6
_INFLUENCE_WEIGHT = 0.4


@dataclass(frozen=True, slots=True)
class VoteTrustScore:
    """The trust score for one cast vote."""

    vote_id: str
    voter_id: str
    motion_id: str
    score: float | None
    basis: Basis
    explanation: str
    votes_considered: int


@dataclass(frozen=True, slots=True)
class VoterReputation:
    """A voter's aggregate reputation across every decided motion they voted on."""

    voter_id: str
    score: float | None
    basis: Literal["history", "insufficient_data"]
    alignment_rate: float | None
    influence_rate: float | None
    votes_considered: int
    explanation: str


def _alignment(choice: str, outcome: str) -> float:
    """1.0 agreed with the winning side, 0.0 disagreed, 0.5 neutral (abstained,
    or the motion was deferred and no side won)."""
    winner = _WINNING_CHOICE.get(outcome)
    if winner is None or choice == "abstain":
        return 0.5
    return 1.0 if choice == winner else 0.0


def _tally_side(yes: int, no: int) -> str:
    if yes > no:
        return "approved"
    if no > yes:
        return "rejected"
    return "deferred"


def _is_pivotal(choice: str, tally: dict) -> bool:
    """Would removing this vote from the tally change the outcome category?
    Only a ``yes``/``no`` vote can be pivotal — removing an ``abstain`` never
    changes the yes/no counts."""
    if choice not in ("yes", "no"):
        return False
    yes, no = int(tally.get("yes", 0)), int(tally.get("no", 0))
    original = _tally_side(yes, no)
    adjusted = _tally_side(yes - 1, no) if choice == "yes" else _tally_side(yes, no - 1)
    return adjusted != original


def _influence(choice: str, tally: dict) -> float:
    """1.0 for a pivotal vote; otherwise an equal share of credit spread across
    every decisive (yes/no) vote on the motion. ``abstain`` never influences the
    tally and scores 0.0."""
    if _is_pivotal(choice, tally):
        return 1.0
    if choice not in ("yes", "no"):
        return 0.0
    decisive = int(tally.get("yes", 0)) + int(tally.get("no", 0))
    if decisive <= 0:
        return 0.0
    return round(1.0 / decisive, 4)


def _score_from_outcome(choice: str, outcome: str, tally: dict) -> tuple[float, str]:
    alignment = _alignment(choice, outcome)
    influence = _influence(choice, tally)
    score = round(100 * (_ALIGNMENT_WEIGHT * alignment + _INFLUENCE_WEIGHT * influence), 1)
    if alignment == 1.0:
        agreement = "aligned with"
    elif alignment == 0.0:
        agreement = "went against"
    else:
        agreement = "was neutral to"
    pivotal_note = "cast the pivotal vote" if _is_pivotal(choice, tally) else "was not pivotal"
    explanation = (
        f"the motion was decided {outcome!r}; this {choice!r} vote {agreement} the "
        f"outcome and {pivotal_note} (alignment={alignment:.1f}, influence={influence:.2f}, "
        f"weights {_ALIGNMENT_WEIGHT:.0%}/{_INFLUENCE_WEIGHT:.0%}) -> score {score}"
    )
    return score, explanation


def compute_vote_trust_score(
    vote: dict, voter_history: Iterable[dict] = ()
) -> VoteTrustScore:
    """The trust score for one vote row (as returned by
    :func:`command_center.runtime.db.council.list_votes_with_outcomes`).

    ``voter_history`` is the same voter's *other* vote rows, used only as a
    fallback when ``vote``'s own motion has no decision yet."""
    if vote.get("decision_outcome") is not None:
        score, explanation = _score_from_outcome(
            vote["choice"], vote["decision_outcome"], vote.get("decision_tally") or {}
        )
        return VoteTrustScore(
            vote_id=vote["id"],
            voter_id=vote["voter_id"],
            motion_id=vote["motion_id"],
            score=score,
            basis="outcome_alignment",
            explanation=explanation,
            votes_considered=1,
        )
    decided_history = [
        v
        for v in voter_history
        if v.get("decision_outcome") is not None and v["motion_id"] != vote["motion_id"]
    ]
    if not decided_history:
        return VoteTrustScore(
            vote_id=vote["id"],
            voter_id=vote["voter_id"],
            motion_id=vote["motion_id"],
            score=None,
            basis="insufficient_data",
            explanation=(
                f"motion {vote['motion_id']!r} is not yet decided and voter "
                f"{vote['voter_id']!r} has no prior decided votes to base a trust "
                "score on"
            ),
            votes_considered=0,
        )
    scores = [
        _score_from_outcome(v["choice"], v["decision_outcome"], v.get("decision_tally") or {})[0]
        for v in decided_history
    ]
    average = round(sum(scores) / len(scores), 1)
    explanation = (
        f"motion {vote['motion_id']!r} is not yet decided; using voter "
        f"{vote['voter_id']!r}'s historical average trust score across "
        f"{len(scores)} previously decided vote(s) ({average})"
    )
    return VoteTrustScore(
        vote_id=vote["id"],
        voter_id=vote["voter_id"],
        motion_id=vote["motion_id"],
        score=average,
        basis="voter_prior",
        explanation=explanation,
        votes_considered=len(scores),
    )


def compute_voter_reputation(voter_id: str, decided_votes: Iterable[dict]) -> VoterReputation:
    """A voter's aggregate reputation across their decided votes (rows with a
    non-``None`` ``decision_outcome``). ``insufficient_data`` when the voter has
    none yet."""
    decided = list(decided_votes)
    if not decided:
        return VoterReputation(
            voter_id=voter_id,
            score=None,
            basis="insufficient_data",
            alignment_rate=None,
            influence_rate=None,
            votes_considered=0,
            explanation=(
                f"voter {voter_id!r} has no decided motions yet; insufficient "
                "history for a trust score"
            ),
        )
    alignments = [_alignment(v["choice"], v["decision_outcome"]) for v in decided]
    influences = [_influence(v["choice"], v.get("decision_tally") or {}) for v in decided]
    alignment_rate = round(sum(alignments) / len(alignments), 3)
    influence_rate = round(sum(influences) / len(influences), 3)
    score = round(100 * (_ALIGNMENT_WEIGHT * alignment_rate + _INFLUENCE_WEIGHT * influence_rate), 1)
    explanation = (
        f"across {len(decided)} decided motion(s), voter {voter_id!r} aligned with "
        f"the winning side {alignment_rate:.0%} of the time and averaged "
        f"{influence_rate:.2f} influence (1.0 = cast the pivotal vote every time) "
        f"-> trust score {score}"
    )
    return VoterReputation(
        voter_id=voter_id,
        score=score,
        basis="history",
        alignment_rate=alignment_rate,
        influence_rate=influence_rate,
        votes_considered=len(decided),
        explanation=explanation,
    )


def reputation_coverage(scores: Iterable[VoteTrustScore]) -> float:
    """The fraction of ``scores`` that carry an explainable trust score (any
    basis other than ``insufficient_data``) — the acceptance metric: at least
    90% of votes must clear this bar. An empty input is vacuously fully
    covered."""
    scored = list(scores)
    if not scored:
        return 1.0
    explainable = sum(1 for s in scored if s.basis != "insufficient_data")
    return explainable / len(scored)
