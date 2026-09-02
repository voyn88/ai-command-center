"""Agent leaderboard: per-agent tiers computed from run history, not a static
one-shot ranking.

Product requirement (VOYN-AGT-LEADERBOARD): the leaderboard must show a
*trend*, not only a snapshot rank — an agent whose success rate is falling
must be visibly distinguishable from one holding steady at the same rate.
This module computes both: a tier (`TOP`/`CHALLENGER`/`NEWCOMER`/`RISKY`) and
a trend (`UP`/`DOWN`/`FLAT`/`INSUFFICIENT_DATA`) derived by comparing an
agent's earlier scored runs against its more recent ones.

Pure and Streamlit-free (same convention as `read_model.py`/`recommend.py`):
takes the unified run list `command_center.runtime.runs_read.list_unified_runs`
already produces and returns an immutable snapshot. The caller decides where
that list comes from; this module never touches the database or filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Outcome vocabulary — which persisted `run.state` values count as a scored
# attempt, and which of those are a success. Mirrors `read_model.py`'s
# philosophy: an operator-cancelled run is a deliberate stop, not a defect,
# so (like `RUN_ATTENTION_STATES` excludes it) it is excluded from an agent's
# scored history entirely rather than counted against it. In-flight runs
# (`PREPARED`/`QUEUED`/`RUNNING`) have no outcome yet and are likewise
# excluded from scoring, though they still count toward `total_runs`.
# --------------------------------------------------------------------------
SUCCESS_STATES: frozenset[str] = frozenset({"COMPLETED"})
SCORED_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "UNKNOWN"})

# An agent identity `runs_read._agent_from_command` could not determine.
# Runs like this cannot be attributed to any agent, so they never enter the
# leaderboard (they would otherwise create a misleading "—" row).
UNKNOWN_AGENT: str = "—"

# --------------------------------------------------------------------------
# Tiers, in the display order the leaderboard renders them.
# --------------------------------------------------------------------------
TIER_TOP = "Top"
TIER_CHALLENGER = "Challenger"
TIER_NEWCOMER = "Newcomer"
TIER_RISKY = "Risky"

TIER_ORDER: tuple[str, ...] = (TIER_TOP, TIER_CHALLENGER, TIER_NEWCOMER, TIER_RISKY)
_TIER_RANK: dict[str, int] = {tier: index for index, tier in enumerate(TIER_ORDER)}

# Below this many scored runs, an agent's success rate is too noisy to rate —
# it stays `Newcomer` regardless of how those few runs went (one lucky or
# unlucky run must not swing an agent straight to `Top`/`Risky`).
MIN_RUNS_FOR_RATED = 5

# Below this many scored runs, a trend split would compare halves of 1-2 runs
# each — too noisy to call a direction, so the trend is `INSUFFICIENT_DATA`.
MIN_RUNS_FOR_TREND = 4

TOP_SUCCESS_RATE = 0.85
RISKY_SUCCESS_RATE = 0.5

# A drop of this size (or more) between an agent's earlier and more recent
# half moves it to `Risky` even when its overall rate still clears
# `RISKY_SUCCESS_RATE` — a sharp decline is itself the risk signal, not just
# the resulting average (the product ask this module exists to satisfy).
DECLINING_TREND_DELTA = -0.15

# --------------------------------------------------------------------------
# Trend directions.
# --------------------------------------------------------------------------
TREND_UP = "up"
TREND_DOWN = "down"
TREND_FLAT = "flat"
TREND_INSUFFICIENT_DATA = "insufficient_data"

# A delta smaller than this (either direction) reads as noise, not a trend.
_TREND_FLAT_EPSILON = 0.05


@dataclass(frozen=True)
class AgentLeaderboardEntry:
    """One agent's row: its tier, its overall rate, and the trend behind it."""

    agent: str
    tier: str
    total_runs: int
    scored_runs: int
    success_rate: float | None
    trend: str
    trend_delta: float | None
    earlier_success_rate: float | None
    recent_success_rate: float | None
    last_run_at: str | None


@dataclass(frozen=True)
class Leaderboard:
    """The full board: every agent with at least one attributable run,
    ordered `Top` -> `Challenger` -> `Newcomer` -> `Risky`, and by success
    rate (highest first) within a tier."""

    entries: list[AgentLeaderboardEntry]


def _run_time_key(run: dict) -> str:
    # `started_at` is when the agent actually began work; `created_at` is the
    # fallback for a run that was queued but never started (still orderable,
    # just less precise). Empty string sorts first so undated runs — which
    # should not occur for scored/terminal runs in practice — never crash the
    # sort or silently land "most recent".
    return run.get("started_at") or run.get("created_at") or ""


def _trend(scored_runs_sorted: list[dict]) -> tuple[str, float | None, float | None, float | None]:
    """Split an agent's scored runs (oldest -> newest) into an earlier and a
    more recent half and compare their success rates.

    Returns `(trend, delta, earlier_rate, recent_rate)`. `delta` is
    `recent_rate - earlier_rate`; positive means improving. The split point is
    `n // 2` so an odd run gets counted in the *recent* half — weighting the
    trend toward what the agent is doing now, not its distant history."""
    n = len(scored_runs_sorted)
    if n < MIN_RUNS_FOR_TREND:
        return TREND_INSUFFICIENT_DATA, None, None, None

    mid = n // 2
    earlier = scored_runs_sorted[:mid]
    recent = scored_runs_sorted[mid:]
    earlier_rate = sum(1 for r in earlier if r.get("state") in SUCCESS_STATES) / len(earlier)
    recent_rate = sum(1 for r in recent if r.get("state") in SUCCESS_STATES) / len(recent)
    delta = recent_rate - earlier_rate

    if delta >= _TREND_FLAT_EPSILON:
        trend = TREND_UP
    elif delta <= -_TREND_FLAT_EPSILON:
        trend = TREND_DOWN
    else:
        trend = TREND_FLAT
    return trend, delta, earlier_rate, recent_rate


def _tier(*, scored_runs: int, success_rate: float | None, trend_delta: float | None) -> str:
    if scored_runs < MIN_RUNS_FOR_RATED:
        return TIER_NEWCOMER
    assert success_rate is not None  # scored_runs >= MIN_RUNS_FOR_RATED > 0 guarantees a rate

    declining = trend_delta is not None and trend_delta <= DECLINING_TREND_DELTA
    if success_rate < RISKY_SUCCESS_RATE or declining:
        return TIER_RISKY
    if success_rate >= TOP_SUCCESS_RATE:
        return TIER_TOP
    return TIER_CHALLENGER


def _entry_sort_key(entry: AgentLeaderboardEntry) -> tuple:
    # Within a tier, best-first: highest success rate, most scored runs to
    # break ties (more evidence behind the same rate outranks less), then
    # agent name so the order is fully deterministic.
    rate_rank = -(entry.success_rate if entry.success_rate is not None else -1.0)
    return (_TIER_RANK[entry.tier], rate_rank, -entry.scored_runs, entry.agent)


def compute_leaderboard(runs: list[dict]) -> Leaderboard:
    """Reduce a unified run list (see `runtime.runs_read.list_unified_runs`)
    to one leaderboard entry per attributable agent.

    Runs whose agent could not be determined (`UNKNOWN_AGENT`) are excluded
    entirely — they cannot be scored against any agent's record. Every other
    agent that has at least one run gets exactly one entry, so the board
    never silently drops an agent that has run at all."""
    by_agent: dict[str, list[dict]] = {}
    for run in runs:
        agent = run.get("agent") or UNKNOWN_AGENT
        if agent == UNKNOWN_AGENT:
            continue
        by_agent.setdefault(agent, []).append(run)

    entries: list[AgentLeaderboardEntry] = []
    for agent, agent_runs in by_agent.items():
        scored = [r for r in agent_runs if r.get("state") in SCORED_STATES]
        scored_sorted = sorted(scored, key=_run_time_key)
        scored_runs = len(scored_sorted)
        success_rate = (
            sum(1 for r in scored_sorted if r.get("state") in SUCCESS_STATES) / scored_runs
            if scored_runs
            else None
        )
        trend, trend_delta, earlier_rate, recent_rate = _trend(scored_sorted)
        tier = _tier(scored_runs=scored_runs, success_rate=success_rate, trend_delta=trend_delta)
        last_run_at = max((_run_time_key(r) for r in agent_runs), default="") or None

        entries.append(
            AgentLeaderboardEntry(
                agent=agent,
                tier=tier,
                total_runs=len(agent_runs),
                scored_runs=scored_runs,
                success_rate=success_rate,
                trend=trend,
                trend_delta=trend_delta,
                earlier_success_rate=earlier_rate,
                recent_success_rate=recent_rate,
                last_run_at=last_run_at,
            )
        )

    entries.sort(key=_entry_sort_key)
    return Leaderboard(entries=entries)
