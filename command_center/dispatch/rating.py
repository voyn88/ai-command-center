"""The pure rating engine for the agent marketplace (VOYN-W0-AICC-AGENT-MARKETPLACE).

The owning idea's premise is that "experience" is not a number an agent is
awarded — it is a *measured* result the platform already has an obligation to
record for every attempt (cost ledger, review verdicts, escalation reasons).
This module is that recording shape (`LedgerEntry`) and the pure computation
that turns a history of entries into a rating per (agent, task class)
(`compute_ratings`), so the notion of "level" is never anything other than
proven competence on a class of work.

Two rules carry the acceptance criteria and must not be diluted by a future
edit:

1. **An attempt counts toward rating only when it is accepted** — landed as a
   real commit (`merged_sha` set) *and* independently reviewed
   (`review_verdict` in `ACCEPTED_VERDICTS`). A completed run that was never
   merged, or one an agent approved for itself, is real signal for cost and
   duration bookkeeping but is not experience: counting it would let an agent
   farm "XP" by running tasks instead of by landing them, which is exactly the
   metrics-become-the-target failure mode the idea calls out.
2. **A rating is not `confident` below a significance threshold.** The
   threshold is counted in *accepted* changes, not attempts, because the
   rating itself is defined over accepted changes. A caller (the dispatch
   policy, a marketplace screen) must treat an unconfident rating as absent —
   `usable_score` enforces this by returning `None` rather than a noisy
   number a router could act on.

Deliberately pure and I/O-free, the same way `command_center.dispatch.policy`
is: no database, no filesystem, so every property here is asserted directly
against `compute_ratings` with plain data. Reading real ledger rows (the `run`
/ `completion` / `provenance` tables) into `LedgerEntry` values is a separate,
later seam — this module only defines the shape and the arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

#: Review verdicts that count as independent acceptance. A self-approval (an
#: agent reviewing its own change) is never one of these — the verdict has to
#: come from an independent reviewer to count as evidence.
ACCEPTED_VERDICTS = frozenset({"approved", "accepted"})

#: Minimum number of *accepted* changes on a (agent, task class) pair before
#: its rating is trusted for routing. Below this, the sample is noise dressed
#: up as a level.
DEFAULT_SIGNIFICANCE_THRESHOLD = 5


@dataclass(frozen=True)
class LedgerEntry:
    """One attempt at one task, as the rating engine needs it.

    `task_class` is an opaque, caller-supplied bucket (e.g. a repository, work
    type and domain composed into one string) — this module does not invent a
    taxonomy; the tree of task classes is meant to be generated from the
    observed task space elsewhere, not declared here.
    """

    executor_id: str
    task_class: str
    merged_sha: str | None = None
    review_verdict: str | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    outcome: str = ""
    escalation_reason: str | None = None

    @property
    def accepted(self) -> bool:
        """Landed *and* independently reviewed — the only thing that is XP."""
        return bool(self.merged_sha) and self.review_verdict in ACCEPTED_VERDICTS


@dataclass(frozen=True)
class AgentRating:
    """The measured rating for one (executor, task_class) pair.

    `score` is the acceptance rate (accepted / attempted) over the whole
    history handed to `compute_ratings`, so a rework-heavy agent scores lower
    even when it eventually lands everything. `avg_cost_usd` is the mean cost
    of the *accepted* attempts only — the price actually paid for the XP,
    which is what a skill has to earn out against to stay worth routing to.
    """

    executor_id: str
    task_class: str
    attempted_count: int
    accepted_count: int
    score: float
    avg_cost_usd: float
    confident: bool


def compute_ratings(
    entries: list[LedgerEntry],
    *,
    significance_threshold: int = DEFAULT_SIGNIFICANCE_THRESHOLD,
) -> dict[tuple[str, str], AgentRating]:
    """Aggregate a ledger into one `AgentRating` per (executor_id, task_class).

    Total and pure: never raises on well-typed input, and the same entries
    always produce the same ratings. Groups with zero entries never appear
    (there is nothing to key them by), so callers only see pairs that were
    actually attempted at least once.
    """
    grouped: dict[tuple[str, str], list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.executor_id, entry.task_class)].append(entry)

    ratings: dict[tuple[str, str], AgentRating] = {}
    for key, group in grouped.items():
        executor_id, task_class = key
        attempted_count = len(group)
        accepted = [e for e in group if e.accepted]
        accepted_count = len(accepted)
        score = accepted_count / attempted_count if attempted_count else 0.0
        avg_cost_usd = (
            sum(e.cost_usd for e in accepted) / accepted_count
            if accepted_count
            else 0.0
        )
        ratings[key] = AgentRating(
            executor_id=executor_id,
            task_class=task_class,
            attempted_count=attempted_count,
            accepted_count=accepted_count,
            score=score,
            avg_cost_usd=avg_cost_usd,
            confident=accepted_count >= significance_threshold,
        )
    return ratings


def usable_score(rating: AgentRating | None) -> float | None:
    """The score a router is allowed to act on, or `None`.

    `None` covers both "never attempted" (`rating is None`) and "attempted but
    not yet significant" (`rating.confident is False`) — a caller branching on
    this never needs to separately check `confident`, so a future edit cannot
    accidentally wire the raw, unconfident `score` into a routing decision.
    """
    if rating is None or not rating.confident:
        return None
    return rating.score
