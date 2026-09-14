"""Trust Lattice — per-agent trust scoring with explainable causal factors.

The task router (:mod:`command_center.orchestrator.routing`) already chooses
*which* executor a task goes to; this module answers a narrower question the
router does not: *how much should we trust that executor here* — for this
project, in this domain, at this risk level. A single global "claude succeeds
92% of the time" number hides the fact that an agent can be excellent on
low-risk documentation tasks in one project and shaky on high-risk migrations
in another. The Trust Lattice keeps those contexts separate.

"Lattice" is literal, not decorative: a trust query is a point
``(project, domain, risk_level)`` and the score for it is built by climbing a
chain of increasingly specific evidence sets —

    cold start (no evidence anywhere)
      -> this agent, globally
        -> this agent, at this risk level
          -> this agent, in this domain, at this risk level
            -> this agent, in this project+domain, at this risk level

— each step blending its own empirical success rate into the more general
prior below it, weighted by how much evidence that step actually has
(``sample_size / (sample_size + SMOOTHING)``). A level with zero runs
contributes nothing and the prior passes through unchanged, so an unfamiliar
project never resets trust to zero — it just falls back to what's known about
the agent one level up.

The acceptance bar this exists to satisfy is that a trust score is never a
bare float: :attr:`TrustScore.factors` is the ordered chain of
:class:`TrustFactor` records that produced it, so "why is this score 0.71"
always has an answer — which levels had evidence, how much, and how heavily
each one was weighted into the blend.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How many runs it takes for a lattice level's own empirical rate to be
#: weighted evenly against the more general prior beneath it
#: (``n / (n + SMOOTHING)``: at n=SMOOTHING the two are weighted 50/50). Low
#: enough that a handful of runs already move the needle, high enough that a
#: single failed run doesn't swing a level's score to 0.0.
DEFAULT_SMOOTHING = 5.0

#: Assumed success rate for an agent with no recorded history anywhere in the
#: lattice. Neutral (neither trusted nor distrusted) so a brand-new agent
#: starts even rather than blocked or auto-trusted before doing any work.
COLD_START_PRIOR = 0.5

#: Synthetic level name for the bottom of the lattice (no evidence at all).
COLD_START_LEVEL = "cold_start"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One historical agent run — the raw evidence the lattice scores from."""

    agent: str
    project: str
    domain: str
    risk_level: str
    success: bool


@dataclass(frozen=True, slots=True)
class TrustFactor:
    """One lattice level's contribution to a blended :class:`TrustScore`.

    ``level`` names which evidence set this is (e.g. ``"agent"``,
    ``"agent+domain+risk_level"``); ``sample_size``/``empirical_rate`` are that
    level's raw evidence (``empirical_rate`` is ``None`` when ``sample_size``
    is 0 — there is nothing to report, not a rate of zero); ``confidence`` is
    the weight (``0.0..1.0``) this level's own rate carried against the prior
    beneath it when it was blended in; ``prior_before``/``score_after`` are the
    running blend immediately before and after this level was applied, so the
    causal chain from cold start to final score can be replayed step by step."""

    level: str
    sample_size: int
    empirical_rate: float | None
    confidence: float
    prior_before: float
    score_after: float


@dataclass(frozen=True, slots=True)
class TrustScore:
    """A trust score for one ``(agent, project, domain, risk_level)`` query.

    ``score`` is the final blended value in ``0.0..1.0``; ``factors`` is the
    ordered causal chain (bottom of the lattice first) that produced it."""

    agent: str
    project: str
    domain: str
    risk_level: str
    score: float
    factors: tuple[TrustFactor, ...]


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


class TrustLattice:
    """Scores agents against a lattice of ``(project, domain, risk_level)``
    contexts built from their historical run outcomes.

    ``history`` is a snapshot, not a live store — construct a fresh lattice
    (or call :meth:`with_history`) when new outcomes should be reflected."""

    def __init__(
        self,
        history: list[RunOutcome] | None = None,
        *,
        smoothing: float = DEFAULT_SMOOTHING,
        cold_start_prior: float = COLD_START_PRIOR,
    ) -> None:
        if smoothing <= 0:
            raise ValueError("smoothing must be > 0")
        self._history = list(history) if history else []
        self._smoothing = float(smoothing)
        self._cold_start_prior = _clamp(cold_start_prior)

    def with_history(self, history: list[RunOutcome]) -> "TrustLattice":
        """A new lattice over ``history``, keeping this one's tuning."""
        return TrustLattice(
            history, smoothing=self._smoothing, cold_start_prior=self._cold_start_prior
        )

    def score(self, agent: str, project: str, domain: str, risk_level: str) -> TrustScore:
        """Blend evidence from the bottom of the lattice (no evidence) up to
        the fully specific ``(agent, project, domain, risk_level)`` context."""
        agent_history = [run for run in self._history if run.agent == agent]

        # Ascending specificity: each step's constraints are a superset of the
        # one before it, so evidence sets only ever shrink (or stay the same)
        # as we climb — the defining shape of a lattice's join chain.
        levels: list[tuple[str, dict[str, str]]] = [
            ("agent", {}),
            ("agent+risk_level", {"risk_level": risk_level}),
            ("agent+domain+risk_level", {"domain": domain, "risk_level": risk_level}),
            (
                "agent+project+domain+risk_level",
                {"project": project, "domain": domain, "risk_level": risk_level},
            ),
        ]

        prior = self._cold_start_prior
        factors = [
            TrustFactor(
                level=COLD_START_LEVEL,
                sample_size=0,
                empirical_rate=None,
                confidence=0.0,
                prior_before=prior,
                score_after=prior,
            )
        ]
        for level, constraints in levels:
            matches = [
                run
                for run in agent_history
                if all(getattr(run, field) == value for field, value in constraints.items())
            ]
            n = len(matches)
            before = prior
            if n:
                rate = sum(1 for run in matches if run.success) / n
                confidence = n / (n + self._smoothing)
                prior = _clamp(confidence * rate + (1.0 - confidence) * before)
            else:
                rate = None
                confidence = 0.0
                # prior unchanged: no evidence at this level, pass the more
                # general prior through untouched.
            factors.append(
                TrustFactor(
                    level=level,
                    sample_size=n,
                    empirical_rate=rate,
                    confidence=confidence,
                    prior_before=before,
                    score_after=prior,
                )
            )

        return TrustScore(
            agent=agent,
            project=project,
            domain=domain,
            risk_level=risk_level,
            score=prior,
            factors=tuple(factors),
        )
