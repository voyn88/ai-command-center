"""Continuous self-assessment and role rotation for the executor cascades
(VOYN-MIN-AGT-EVO2).

``routing.ROUTING_MATRIX`` is deliberately static: a human names which
executor plays which position in a task class's escalation chain, and
``routing.py``'s own docstring calls that "deliberately STATIC and
deliberately HONEST" — a phantom link burns a real attempt, so the matrix
names only executors proven on the fleet. This module does not weaken that.
It answers a narrower question the static matrix cannot: *given what actually
happened*, is the recorded order still the best one?

Two pieces:

* :func:`score_executors` turns a history of attempt outcomes into a ranking,
  per task class, of how often each executor's link in the chain actually
  succeeded — the self-assessment.
* :func:`recommend_cascade` proposes a reordering of an existing cascade's
  links (never adds or removes a link, never introduces an executor absent
  from the current cascade) by that ranking — the role rotation. It is a
  RECOMMENDATION, not a mutation: nothing here writes to
  ``routing.ROUTING_MATRIX``. Applying a recommendation is a reviewed code
  change to that table, the same as any other edit to a critical chain,
  because an executor that regresses after the assessment window closes must
  be caught by a human reading a diff, not silently re-promoted by the next
  score.

The quarterly cadence the acceptance criterion asks for
("each quarter, the best configuration per domain is updated") is
:func:`is_reassessment_due`: it compares the calendar quarter of the last
assessment against now, so a scheduler (a cron-style job, or a person running
a report) can ask "is it time?" without tracking anything beyond the last
assessment's own timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping

__all__ = [
    "AttemptOutcome",
    "ExecutorScore",
    "Quarter",
    "quarter_of",
    "is_reassessment_due",
    "score_executors",
    "recommend_cascade",
    "quarterly_self_assessment",
]

#: Below this many observed attempts for an executor in a task class, its
#: success rate is noise rather than a signal worth rotating roles over. A
#: single lucky (or unlucky) attempt must not reorder a critical chain.
DEFAULT_MIN_SAMPLES = 20


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One executed cascade link, as recorded after the fact.

    ``task_class`` matches a key of ``routing.ROUTING_MATRIX`` (the domain);
    ``executor`` matches a link's ``"executor"`` value. The source of these is
    intentionally left open — a caller reading ``work_result``/``completion``
    history maps each row to one ``AttemptOutcome``, but this module has no
    database dependency of its own, the same seam ``routing.cascade_for``
    already uses.
    """

    task_class: str
    executor: str
    succeeded: bool
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutorScore:
    """One executor's track record for one task class."""

    executor: str
    task_class: str
    attempts: int
    successes: int

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


@dataclass(frozen=True, slots=True)
class Quarter:
    """A calendar quarter, comparable and hashable — the unit the acceptance
    criterion's "each quarter" is measured in."""

    year: int
    number: int  # 1..4


def quarter_of(when: date | datetime) -> Quarter:
    """The calendar quarter containing ``when``."""
    return Quarter(year=when.year, number=(when.month - 1) // 3 + 1)


def is_reassessment_due(
    last_assessed_at: date | datetime | None, now: date | datetime
) -> bool:
    """Whether a new self-assessment should run.

    ``True`` when no assessment has ever run, or the calendar quarter has
    rolled over since the last one — never on elapsed days, so a quarter with
    a short first month (an assessment run on the last day of Q1) does not
    demand a second run days later while still inside Q1's boundary.
    """
    if last_assessed_at is None:
        return True
    return quarter_of(last_assessed_at) != quarter_of(now)


def score_executors(
    outcomes: Iterable[AttemptOutcome], *, task_class: str
) -> list[ExecutorScore]:
    """Rank executors by observed success rate within ``task_class``.

    Ordered best first: success rate descending, ties broken by more evidence
    (attempts descending), remaining ties broken by executor name so the
    result is fully deterministic. Outcomes for other task classes are
    ignored — a domain's chain is assessed on its own evidence, not another
    domain's.
    """
    tallies: dict[str, list[int]] = {}
    for outcome in outcomes:
        if outcome.task_class != task_class:
            continue
        tally = tallies.setdefault(outcome.executor, [0, 0])
        tally[0] += 1
        tally[1] += 1 if outcome.succeeded else 0

    scores = [
        ExecutorScore(
            executor=executor,
            task_class=task_class,
            attempts=attempts,
            successes=successes,
        )
        for executor, (attempts, successes) in tallies.items()
    ]
    scores.sort(key=lambda s: (-s.success_rate, -s.attempts, s.executor))
    return scores


def recommend_cascade(
    outcomes: Iterable[AttemptOutcome],
    task_class: str,
    current_cascade: list[dict],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> list[dict] | None:
    """Propose a reordering of ``current_cascade`` by self-assessed success
    rate, or ``None`` when the evidence is not yet strong enough to trust.

    Deliberately conservative, matching ``routing.py``'s own honesty rule:

    * Never invents a link. The returned cascade is a permutation of
      ``current_cascade`` — same executors, same per-link ``task_type``,
      nothing added or removed. A link naming an executor this module has
      never scored keeps the whole recommendation withheld (``None``) rather
      than guessing where an unscored executor belongs.
    * Never rotates on thin evidence. Every executor in ``current_cascade``
      must have at least ``min_samples`` observed attempts in ``task_class``,
      or the recommendation is withheld.
    * Never touches ``current_cascade`` in place — a fresh list of fresh
      dicts, the same non-aliasing contract ``routing.cascade_for`` gives its
      callers.
    """
    scores_by_executor: Mapping[str, ExecutorScore] = {
        score.executor: score for score in score_executors(outcomes, task_class=task_class)
    }

    for link in current_cascade:
        score = scores_by_executor.get(link["executor"])
        if score is None or score.attempts < min_samples:
            return None

    ranked = sorted(
        current_cascade,
        key=lambda link: (
            -scores_by_executor[link["executor"]].success_rate,
            -scores_by_executor[link["executor"]].attempts,
            link["executor"],
        ),
    )
    return [dict(link) for link in ranked]


def quarterly_self_assessment(
    outcomes: Iterable[AttemptOutcome],
    routing_matrix: Mapping[str, list[dict]],
    *,
    now: date | datetime,
    last_assessed_at: date | datetime | None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """The quarterly cadence the acceptance criterion asks for: once per
    quarter, produce the best-known configuration for every domain.

    Returns a report rather than applying anything:

    ``{"due": bool, "quarter": Quarter | None,
       "recommendations": {task_class: cascade | None}}``

    ``due`` is ``False`` (with an empty ``recommendations``) when the current
    quarter already had its assessment — the cadence gate. When due, every
    task class in ``routing_matrix`` gets an entry; a task class whose
    evidence is not yet strong enough maps to ``None`` (see
    :func:`recommend_cascade`), so "we assessed and nothing changed" stays
    distinguishable from "we have not assessed yet".
    """
    if not is_reassessment_due(last_assessed_at, now):
        return {"due": False, "quarter": None, "recommendations": {}}

    outcomes = list(outcomes)
    recommendations = {
        task_class: recommend_cascade(
            outcomes, task_class, cascade, min_samples=min_samples
        )
        for task_class, cascade in routing_matrix.items()
    }
    return {"due": True, "quarter": quarter_of(now), "recommendations": recommendations}
