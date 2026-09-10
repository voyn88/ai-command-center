"""Ethics and anti-bias policy gate for agent decisions.

VOYN-AGT-ETHICS: agent output can carry a property its own success metric
never rates it on. Three are in scope here:

* **retraining** — the decision changes the model/policy an agent runs on. A
  decision has no standing to approve its own successor.
* **conflict_of_interest** — the party proposing the decision is also named
  among its declared stakeholders, i.e. it stands to gain or lose from the
  very choice it is making.
* **toxicity** — the decision's content scores at or above the toxicity
  threshold, or carries content nobody ever scored at all.

None of the three is read from what the decision *claims about itself*:
:func:`classify_risk` only reads structural facts a caller reports
independently of the agent's own verdict (``action``, ``stakeholders``,
``toxicity_score``), so an agent cannot clear the gate by simply asserting it
is low-risk. This is the "anti-vulnerable" half of the gate — classification
cannot be talked out of by the thing it classifies.

:func:`evaluate_ethics_gate` is the separate policy-gate the acceptance
requires: a decision with no detected risk category proceeds untouched. A
decision that carries any risk category is blocked unless it carries a
:class:`PolicyReview` that is bound to the exact decision id, was authored by
someone other than the decision's own proposer, was approved, and explicitly
covers every category found. A missing, mismatched, self-authored,
unapproved, or partially-covering review all block, each with its own typed
reason. Content nobody scored for toxicity blocks the same way unscored
budget blocks every dispatch in :mod:`command_center.dispatch.policy`: an
unknown measurement is not a clean one, and treating it as clean would let the
check be defeated by simply never running the classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RETRAINING = "retraining"
CONFLICT_OF_INTEREST = "conflict_of_interest"
TOXICITY = "toxicity"

#: Every category this gate knows how to detect and require review for.
RISK_CATEGORIES = frozenset({RETRAINING, CONFLICT_OF_INTEREST, TOXICITY})

#: Action names (case/format-insensitive) that mark a decision as one that
#: changes the model or policy an agent runs on. Callers with a broader
#: vocabulary pass their own set to :func:`classify_risk` /
#: :func:`evaluate_ethics_gate` rather than editing this default.
DEFAULT_RETRAINING_ACTIONS = frozenset(
    {
        "retrain_model",
        "retrain",
        "fine_tune",
        "fine_tune_model",
        "update_model_weights",
        "replace_policy",
        "deploy_retrained_model",
    }
)

#: A toxicity score at or above this fraction is high-risk by default.
DEFAULT_TOXICITY_THRESHOLD = 0.5


def _normalize_action(action: str) -> str:
    """Collapse case/hyphen/space variants so ``"Fine-Tune"`` and
    ``"fine_tune"`` are the same action name."""
    return action.strip().lower().replace("-", "_").replace(" ", "_")


@dataclass(frozen=True, slots=True)
class AgentDecision:
    """The structural facts about one agent output, as reported by the
    caller — never as self-declared by the agent's own verdict.

    * ``stakeholders`` — ids of parties with a declared material interest in
      the outcome. A conflict of interest is read from ``proposer_id``
      appearing in this set, not from a self-report.
    * ``toxicity_score`` — ``None`` when no content classifier has scored this
      decision yet; distinct from a real ``0.0``.
    * ``text`` — the decision's free-text content, if any. Only used to tell
      "no content to score" apart from "content nobody scored".
    """

    id: str
    proposer_id: str
    action: str
    stakeholders: frozenset[str] = field(default_factory=frozenset)
    toxicity_score: float | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyReview:
    """An explicit, independent sign-off bound to one decision.

    ``covers`` is the set of risk categories the reviewer attests they
    evaluated — a review of the conflict-of-interest angle alone does not
    also clear a toxicity finding on the same decision.
    """

    decision_id: str
    reviewer_id: str
    covers: frozenset[str]
    approved: bool = True


@dataclass(frozen=True, slots=True)
class GateVerdict:
    allowed: bool
    risk_categories: frozenset[str]
    reasons: tuple[str, ...]


def classify_risk(
    decision: AgentDecision,
    *,
    retraining_actions: frozenset[str] = DEFAULT_RETRAINING_ACTIONS,
    toxicity_threshold: float = DEFAULT_TOXICITY_THRESHOLD,
) -> frozenset[str]:
    """Return the risk categories `decision` structurally carries.

    Every check reads a fact about the decision, never a claim it makes about
    itself, so classification cannot be defeated by the agent asserting it is
    safe.
    """
    categories: set[str] = set()

    if _normalize_action(decision.action) in retraining_actions:
        categories.add(RETRAINING)

    if decision.proposer_id in decision.stakeholders:
        categories.add(CONFLICT_OF_INTEREST)

    if decision.toxicity_score is not None:
        if decision.toxicity_score >= toxicity_threshold:
            categories.add(TOXICITY)
    elif decision.text is not None and decision.text.strip():
        # Content nobody scored is the "unknown" case, not the "clean" one —
        # see the module docstring for why this fails closed rather than
        # passing silently.
        categories.add(TOXICITY)

    return frozenset(categories)


def evaluate_ethics_gate(
    decision: AgentDecision,
    review: PolicyReview | None = None,
    *,
    retraining_actions: frozenset[str] = DEFAULT_RETRAINING_ACTIONS,
    toxicity_threshold: float = DEFAULT_TOXICITY_THRESHOLD,
) -> GateVerdict:
    """Evaluate whether `decision` may proceed.

    A decision with no detected risk category is allowed with no review at
    all — this gate only ever adds friction to what it classifies as
    high-risk, exactly as the acceptance requires ("every high-risk output
    passes through a separate policy-gate", not every output). A decision
    that does carry a risk category needs an independent, approved,
    exactly-bound review that covers every category found; any gap blocks
    with its own typed reason so a caller (and its tests) can tell exactly
    what is missing.
    """
    risk = classify_risk(
        decision,
        retraining_actions=retraining_actions,
        toxicity_threshold=toxicity_threshold,
    )
    if not risk:
        return GateVerdict(allowed=True, risk_categories=risk, reasons=())

    if review is None:
        reasons = tuple(f"policy_review_required:{c}" for c in sorted(risk))
        return GateVerdict(allowed=False, risk_categories=risk, reasons=reasons)

    reasons_list: list[str] = []
    if review.decision_id != decision.id:
        reasons_list.append("policy_review_decision_mismatch")
    if review.reviewer_id == decision.proposer_id:
        reasons_list.append("policy_review_not_independent")
    if not review.approved:
        reasons_list.append("policy_review_rejected")
    reasons_list.extend(
        f"policy_review_missing_coverage:{c}" for c in sorted(risk - review.covers)
    )

    return GateVerdict(
        allowed=not reasons_list, risk_categories=risk, reasons=tuple(reasons_list)
    )
