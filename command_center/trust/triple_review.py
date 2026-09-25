"""Triple-council review for critical decisions (VOYN-MIN-CI) — a Human+AI
3-role system: every critical decision passes through three independent,
named evaluations before it is allowed to proceed automatically.

Roles are fixed by construction — this is deliberately *not* a general voter
roster like :mod:`command_center.council` (an open Board where any number of
members vote yes/no/abstain). A critical decision always gets exactly these
three seats, each with a distinct job:

* ``executor``  — the agent (or human) that would actually carry out the
  decision. Its evaluation is the doer's case *for* the decision.
* ``audit``     — an independent AI reviewer checking the decision against
  policy and consistency.
* ``stress``    — an independent AI reviewer whose only job is to attack the
  decision: worst case, failure modes, adversarial framing.

Each role always produces a :class:`RoleVerdict` carrying both a verdict and
an explanation — the acceptance is "3 explanations", not just 3 votes, so a
verdict without a non-empty explanation is refused at construction, before it
can ever be recorded or fed into consensus.

Nothing here touches storage or an API — like :mod:`command_center.council`,
this is a pure domain seam a caller (a service, a test) drives directly with
hand-built verdicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["executor", "audit", "stress"]
Verdict = Literal["approve", "reject", "needs_changes"]
Outcome = Literal["approved", "rejected", "escalated"]

#: The three fixed seats, in the order they are always reported — executor
#: first (the proposal), then the two independent AI reviews.
ROLES: tuple[Role, ...] = ("executor", "audit", "stress")

_VERDICTS: tuple[Verdict, ...] = ("approve", "reject", "needs_changes")


class MissingExplanationError(Exception):
    """Raised when a role's evaluation has no (or blank) explanation. Every
    critical-decision verdict must carry a non-empty rationale — the
    acceptance's "3 explanations" — so an unexplained verdict is refused
    before it can be recorded."""


class DuplicateRoleError(Exception):
    """Raised when the same role appears twice in one review — each of the
    three seats evaluates independently and exactly once per decision."""


class IncompleteReviewError(Exception):
    """Raised when consensus is requested before all three roles
    (executor/audit/stress) have recorded a verdict — a critical decision is
    never decided on a partial review."""


@dataclass(frozen=True, slots=True)
class RoleVerdict:
    """One seat's independent evaluation of a critical decision.

    ``evaluator`` is an optional free-form identifier (a model name, an agent
    id, a human's handle) for provenance; it plays no role in the consensus
    rule, which only ever looks at ``role`` and ``verdict``."""

    role: Role
    verdict: Verdict
    explanation: str
    evaluator: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; must be one of {ROLES}")
        if self.verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict {self.verdict!r}; must be one of {_VERDICTS}")
        if not self.explanation or not self.explanation.strip():
            raise MissingExplanationError(
                f"{self.role!r} verdict must carry a non-empty explanation"
            )


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """The outcome of applying the final consensus rule to one critical
    decision's three independent evaluations."""

    outcome: Outcome
    rationale: str
    verdicts: tuple[RoleVerdict, ...]

    @property
    def explanations(self) -> dict[str, str]:
        """The three recorded explanations, keyed by role — addressable by
        name for display or an audit trail."""
        return {v.role: v.explanation for v in self.verdicts}


def evaluate(verdicts: list[RoleVerdict] | tuple[RoleVerdict, ...]) -> ConsensusResult:
    """Apply the final consensus rule for a critical decision.

    Requires exactly one verdict per role in :data:`ROLES` — raises
    :class:`DuplicateRoleError` if a role appears twice, or
    :class:`IncompleteReviewError` if any role is missing. Consensus is
    deliberately conservative, favouring a human hard-stop over guessing:

    * any ``reject`` -> ``"rejected"`` — a single seat can veto a critical
      decision; consensus never averages away a veto;
    * all three ``approve`` -> ``"approved"`` — unanimous consent is required
      to auto-proceed;
    * anything else (no reject, but not unanimous approval — e.g. an
      ``approve``/``needs_changes`` split) -> ``"escalated"`` — disagreement
      without a veto is not resolved by majority vote, it is routed to a
      human for adjudication.
    """
    by_role: dict[Role, RoleVerdict] = {}
    for v in verdicts:
        if v.role in by_role:
            raise DuplicateRoleError(f"role {v.role!r} was evaluated more than once")
        by_role[v.role] = v
    missing = [r for r in ROLES if r not in by_role]
    if missing:
        raise IncompleteReviewError(f"missing verdict(s) from: {', '.join(missing)}")

    ordered = tuple(by_role[r] for r in ROLES)
    choices = {v.role: v.verdict for v in ordered}

    if any(choice == "reject" for choice in choices.values()):
        rejecters = [r for r, c in choices.items() if c == "reject"]
        outcome: Outcome = "rejected"
        rationale = (
            f"rejected: {', '.join(rejecters)} vetoed the decision "
            "(a single reject is a hard stop for critical decisions)"
        )
    elif all(choice == "approve" for choice in choices.values()):
        outcome = "approved"
        rationale = "approved: unanimous approval from executor, audit and stress review"
    else:
        undecided = [r for r, c in choices.items() if c != "approve"]
        outcome = "escalated"
        rationale = (
            f"escalated to human adjudication: no veto, but {', '.join(undecided)} "
            "did not approve — critical decisions require unanimity or a human call"
        )
    return ConsensusResult(outcome=outcome, rationale=rationale, verdicts=ordered)
