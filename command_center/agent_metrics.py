"""Unified per-agent metrics schema — one normalized view across all agents.

This is a pure projection, not a new data source: every input already exists
in the runtime read layer.

* run outcome / duration come from :func:`command_center.runtime.runs_read.list_unified_runs`
  (``state``, ``duration_seconds``, ``agent``);
* "a human had to step in" and "a run needed a recovery pass" come from the
  ``completion`` table's ``requires_human`` / ``recovery_count`` columns — the
  only real signals in the schema for manual rework and rollback, keyed by
  run via :func:`command_center.runtime.db.get_completions_for_runs`;
* repeat-provider-call counts come from the ``provider_attempt`` table via
  :func:`command_center.runtime.db.get_provider_attempts_for_runs` — the only
  real per-run repeat-cost signal, since token/dollar cost is not tracked
  anywhere yet. ``cost`` is therefore a proxy (fewer attempts ⇒ cheaper), not
  a currency figure.

Every dimension is normalized to ``[0, 1]``. ``quality``, ``speed`` and
``cost`` follow "higher is better" (fastest/cheapest/most-successful agent in
the cohort scores 1.0). ``rollback_rate`` and ``manual_rework_rate`` are the
literal rate they name, so "lower is better" for those two. A dimension is
``None`` when the inputs it needs are entirely absent for that agent — never
a guessed 0, matching the "unknown stays unknown" rule the rest of this
codebase's evidence-facing modules follow (see ``dashboard_truth.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

_COMPLETED_STATE = "COMPLETED"


@dataclass(frozen=True)
class AgentRawMetrics:
    agent: str
    runs_total: int
    runs_completed: int
    runs_failed: int
    runs_cancelled: int
    quality_success_count: int
    completions_total: int
    requires_human_count: int
    recovered_count: int
    avg_attempts_per_run: float | None
    median_duration_seconds: float | None


@dataclass(frozen=True)
class AgentMetrics:
    agent: str
    sample_size: int
    quality: float | None
    speed: float | None
    cost: float | None
    rollback_rate: float | None
    manual_rework_rate: float | None
    raw: AgentRawMetrics


def _quality(raw: AgentRawMetrics) -> float | None:
    if raw.runs_total == 0:
        return None
    return raw.quality_success_count / raw.runs_total


def _manual_rework_rate(raw: AgentRawMetrics) -> float | None:
    if raw.completions_total == 0:
        return None
    return raw.requires_human_count / raw.completions_total


def _rollback_rate(raw: AgentRawMetrics) -> float | None:
    if raw.completions_total == 0:
        return None
    return raw.recovered_count / raw.completions_total


def _min_max_normalize(
    raws: list[AgentRawMetrics], *, value: dict[str, float], invert: bool
) -> dict[str, float | None]:
    """Scale ``value`` (present only for agents with real data) to [0, 1]
    across the cohort. ``invert=True`` maps the smallest raw value to 1.0
    (used for durations/attempts, where smaller is better); a cohort with a
    single distinct value maps everyone with data to 1.0 rather than dividing
    by zero."""
    if not value:
        return {raw.agent: None for raw in raws}
    lo, hi = min(value.values()), max(value.values())
    result: dict[str, float | None] = {}
    for raw in raws:
        v = value.get(raw.agent)
        if v is None:
            result[raw.agent] = None
        elif hi == lo:
            result[raw.agent] = 1.0
        else:
            result[raw.agent] = (hi - v) / (hi - lo) if invert else (v - lo) / (hi - lo)
    return result


def raw_metrics_by_agent(
    runs: list[dict],
    completions_by_run: dict[str, dict],
    attempts_by_run: dict[str, list[dict]],
) -> list[AgentRawMetrics]:
    """Group normalized run rows (``runs_read.list_unified_runs`` shape) by
    agent and reduce each group to the plain counts every metric derives
    from. Runs without an ``agent`` land under ``"—"`` (the same placeholder
    the Runs page filter already uses for unattributed runs)."""
    by_agent: dict[str, list[dict]] = {}
    for run in runs:
        by_agent.setdefault(run.get("agent") or "—", []).append(run)

    raws: list[AgentRawMetrics] = []
    for agent, agent_runs in sorted(by_agent.items()):
        completions = [
            completions_by_run[run["id"]]
            for run in agent_runs
            if run.get("id") in completions_by_run
        ]
        quality_success_count = 0
        for run in agent_runs:
            if run.get("state") != _COMPLETED_STATE:
                continue
            completion = completions_by_run.get(run.get("id"))
            if completion and completion.get("review_verdict") == "REJECT":
                continue
            quality_success_count += 1

        durations = [
            run["duration_seconds"] for run in agent_runs if run.get("duration_seconds") is not None
        ]
        attempt_counts = [
            len(attempts_by_run[run["id"]])
            for run in agent_runs
            if attempts_by_run.get(run.get("id"))
        ]

        raws.append(
            AgentRawMetrics(
                agent=agent,
                runs_total=len(agent_runs),
                runs_completed=sum(1 for r in agent_runs if r.get("state") == _COMPLETED_STATE),
                runs_failed=sum(1 for r in agent_runs if r.get("state") == "FAILED"),
                runs_cancelled=sum(
                    1 for r in agent_runs if r.get("state") in ("CANCELLED", "INTERRUPTED")
                ),
                quality_success_count=quality_success_count,
                completions_total=len(completions),
                requires_human_count=sum(1 for c in completions if c.get("requires_human")),
                recovered_count=sum(1 for c in completions if (c.get("recovery_count") or 0) > 0),
                avg_attempts_per_run=(
                    sum(attempt_counts) / len(attempt_counts) if attempt_counts else None
                ),
                median_duration_seconds=median(durations) if durations else None,
            )
        )
    return raws


def compute_agent_metrics(
    runs: list[dict],
    completions_by_run: dict[str, dict],
    attempts_by_run: dict[str, list[dict]],
) -> list[AgentMetrics]:
    """The one public entry point: unified, normalized metrics for every
    agent present in ``runs``, sorted by agent name."""
    raws = raw_metrics_by_agent(runs, completions_by_run, attempts_by_run)
    speed_by_agent = _min_max_normalize(
        raws,
        value={r.agent: r.median_duration_seconds for r in raws if r.median_duration_seconds is not None},
        invert=True,
    )
    cost_by_agent = _min_max_normalize(
        raws,
        value={r.agent: r.avg_attempts_per_run for r in raws if r.avg_attempts_per_run is not None},
        invert=True,
    )
    return [
        AgentMetrics(
            agent=raw.agent,
            sample_size=raw.runs_total,
            quality=_quality(raw),
            speed=speed_by_agent[raw.agent],
            cost=cost_by_agent[raw.agent],
            rollback_rate=_rollback_rate(raw),
            manual_rework_rate=_manual_rework_rate(raw),
            raw=raw,
        )
        for raw in raws
    ]
