"""Two-axis triage scoring for advisor candidates.

The Советник inbox sorts on *value* vs *effort* (with a *risk* discount). The
scorer turns a :class:`~command_center.advisor.types.Candidate`'s raw ``signals``
into a normalized :class:`Score` and a single ``priority`` the service uses to
rank candidates and to evaluate auto-promote rules. The two text buckets
(``value_bucket``/``effort_bucket``) are what land in the persisted proposal's
``expected_gain``/``effort`` columns, so the inbox can render them without the
numeric model leaking into the contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.advisor.types import Candidate


def _clamp(value: float) -> float:
    """Clamp to the ``0.0..1.0`` band every signal and score live in."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def _bucket(value: float) -> str:
    """Map a ``0.0..1.0`` score onto the coarse text label the inbox shows."""
    if value < 1 / 3:
        return "low"
    if value < 2 / 3:
        return "medium"
    return "high"


@dataclass(frozen=True, slots=True)
class Score:
    """A candidate's normalized value/effort/risk and the composite priority.

    ``priority`` rewards high value, discounts effort and penalizes risk, so a
    cheap high-value low-risk suggestion outranks an expensive risky one of the
    same nominal value. All four fields are in ``0.0..1.0``."""

    value: float
    effort: float
    risk: float
    priority: float

    @property
    def value_bucket(self) -> str:
        return _bucket(self.value)

    @property
    def effort_bucket(self) -> str:
        return _bucket(self.effort)

    @property
    def risk_bucket(self) -> str:
        return _bucket(self.risk)


class ProposalScorer:
    """Compute a :class:`Score` from a candidate's ``signals``.

    Weights are constructor-injected so a caller can retune triage without a
    code change. Defaults: value is mostly impact with a frequency component;
    effort and risk pass through their signals; priority multiplies value by an
    effort discount and a risk penalty. Missing signals fall back to neutral
    midpoints rather than raising, so a collector that can only measure one axis
    still yields a usable score."""

    def __init__(
        self,
        *,
        impact_weight: float = 0.6,
        frequency_weight: float = 0.4,
        effort_discount: float = 0.5,
        risk_penalty: float = 0.5,
    ) -> None:
        total = impact_weight + frequency_weight
        if total <= 0:
            raise ValueError("impact_weight + frequency_weight must be > 0")
        # Normalize the value weights so they always sum to 1 regardless of the
        # raw numbers a caller passes.
        self._impact_weight = impact_weight / total
        self._frequency_weight = frequency_weight / total
        self._effort_discount = _clamp(effort_discount)
        self._risk_penalty = _clamp(risk_penalty)

    def score(self, candidate: Candidate) -> Score:
        signals = candidate.signals
        impact = _clamp(float(signals.get("impact", 0.5)))
        frequency = _clamp(float(signals.get("frequency", 0.5)))
        effort = _clamp(float(signals.get("effort", 0.5)))
        risk = _clamp(float(signals.get("risk", 0.3)))
        value = _clamp(self._impact_weight * impact + self._frequency_weight * frequency)
        priority = _clamp(
            value * (1.0 - self._effort_discount * effort) * (1.0 - self._risk_penalty * risk)
        )
        return Score(value=value, effort=effort, risk=risk, priority=priority)
