"""Quality-drift detection and agent quarantine (VOYN-AGT-DRIFT, QA/data).

An agent's quality metric (whatever the caller feeds `record` -- a review
acceptance rate, a task success rate, ...) is batched into fixed-size
windows. The FIRST closed window establishes that agent's own baseline: this
is drift detection against what the agent has already shown it can do, not a
check against a fixed SLA target, so two agents with different baselines are
each judged against themselves.

Every window after the baseline is compared against it. A window that lands
more than `deviation_threshold` below baseline is a breach. One breach is
noise -- a good window immediately resets the streak to zero -- but two
BREACHES IN A ROW (`consecutive_breaches_to_quarantine`, default 2) moves the
agent into quarantine.

Quarantine is a freeze, not a cooldown: it does not clear itself when quality
recovers. `release` is the only way out, and it deliberately drops the old
baseline too -- resuming would otherwise immediately re-arm the same
comparison against pre-quarantine history instead of letting the agent
re-establish what "normal" looks like now.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_CONSECUTIVE_BREACHES_TO_QUARANTINE",
    "DEFAULT_DEVIATION_THRESHOLD",
    "DEFAULT_WINDOW_SIZE",
    "QualityDriftMonitor",
    "WindowResult",
]

DEFAULT_WINDOW_SIZE = 20
#: The acceptance threshold: a window averaging more than 10% below baseline
#: is a breach.
DEFAULT_DEVIATION_THRESHOLD = 0.10
#: The acceptance threshold: two breaches IN A ROW quarantines the agent.
DEFAULT_CONSECUTIVE_BREACHES_TO_QUARANTINE = 2


@dataclass(frozen=True, slots=True)
class WindowResult:
    """One closed window's outcome for one agent."""

    window_index: int
    mean: float
    baseline: float
    #: Signed relative drop from baseline: positive means quality fell.
    #: (baseline - mean) / baseline.
    deviation: float
    breached: bool


@dataclass(slots=True)
class _AgentState:
    baseline: float | None = None
    buffer: list[float] = field(default_factory=list)
    consecutive_breaches: int = 0
    windows_closed: int = 0
    quarantined: bool = False
    quarantine_reason: str | None = None
    history: list[WindowResult] = field(default_factory=list)


class QualityDriftMonitor:
    """Tracks per-agent quality windows and quarantines on sustained drift."""

    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        deviation_threshold: float = DEFAULT_DEVIATION_THRESHOLD,
        consecutive_breaches_to_quarantine: int = DEFAULT_CONSECUTIVE_BREACHES_TO_QUARANTINE,
    ) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if not (0.0 < deviation_threshold < 1.0):
            raise ValueError("deviation_threshold must be in (0.0, 1.0)")
        if consecutive_breaches_to_quarantine < 1:
            raise ValueError("consecutive_breaches_to_quarantine must be >= 1")
        self._window_size = window_size
        self._deviation_threshold = deviation_threshold
        self._consecutive_breaches_to_quarantine = consecutive_breaches_to_quarantine
        self._agents: dict[str, _AgentState] = {}

    def record(self, agent_id: str, value: float) -> WindowResult | None:
        """Record one quality sample for `agent_id`.

        Returns the closed `WindowResult` when this sample completes a
        window, else `None`. Samples keep accumulating even while the agent
        is quarantined, so its history stays continuous -- but no amount of
        recovered quality lifts the quarantine on its own; only `release`
        does.
        """
        state = self._agents.setdefault(agent_id, _AgentState())
        state.buffer.append(value)
        if len(state.buffer) < self._window_size:
            return None

        window_mean = sum(state.buffer) / len(state.buffer)
        state.buffer = []
        state.windows_closed += 1

        if state.baseline is None:
            state.baseline = window_mean
            result = WindowResult(state.windows_closed, window_mean, state.baseline, 0.0, False)
            state.history.append(result)
            return result

        deviation = (state.baseline - window_mean) / state.baseline
        breached = deviation > self._deviation_threshold
        state.consecutive_breaches = state.consecutive_breaches + 1 if breached else 0

        result = WindowResult(state.windows_closed, window_mean, state.baseline, deviation, breached)
        state.history.append(result)

        if (
            not state.quarantined
            and state.consecutive_breaches >= self._consecutive_breaches_to_quarantine
        ):
            state.quarantined = True
            state.quarantine_reason = (
                f"quality dropped {deviation:.1%} below baseline "
                f"({window_mean:.4g} vs {state.baseline:.4g}) for "
                f"{state.consecutive_breaches} consecutive windows"
            )

        return result

    def is_quarantined(self, agent_id: str) -> bool:
        state = self._agents.get(agent_id)
        return bool(state and state.quarantined)

    def quarantine_reason(self, agent_id: str) -> str | None:
        state = self._agents.get(agent_id)
        return state.quarantine_reason if state else None

    def release(self, agent_id: str) -> None:
        """Manually lift quarantine, dropping the streak and the baseline so
        the next closed window re-establishes what "normal" means instead of
        re-arming against stale pre-quarantine history."""
        state = self._agents.get(agent_id)
        if state is None:
            return
        state.quarantined = False
        state.quarantine_reason = None
        state.consecutive_breaches = 0
        state.baseline = None

    def history(self, agent_id: str) -> list[WindowResult]:
        state = self._agents.get(agent_id)
        return list(state.history) if state else []
