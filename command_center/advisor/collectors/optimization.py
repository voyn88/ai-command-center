"""Optimization collector: spot slow, inefficient runs from local run history.

Signal source is the runtime ``run`` table (read through the db facade, never raw
SQL). For every project it measures the wall-clock duration of completed runs
(``completed_at - started_at``) and, when a project accumulates enough runs above
a "slow" threshold, raises one ``optimization`` candidate suggesting the owner
look at what makes those runs slow. This is entirely local and free — no external
profiler, no paid APIs — and it yields **zero** candidates cleanly when there is
no run history yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import ClassVar

from command_center.advisor.collectors.base import Collector
from command_center.advisor.types import Candidate, CollectorContext
from command_center.runtime import db


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _duration_seconds(run: dict) -> float | None:
    """Wall-clock seconds for a run, or ``None`` if either endpoint is missing
    or the timestamps are out of order (a clock skew we refuse to score)."""
    started = _parse(run.get("started_at"))
    completed = _parse(run.get("completed_at"))
    if started is None or completed is None:
        return None
    seconds = (completed - started).total_seconds()
    return seconds if seconds >= 0 else None


class OptimizationCollector(Collector):
    """Raise an ``optimization`` proposal per project with a cluster of slow runs."""

    name: ClassVar[str] = "optimization"
    kind: ClassVar[str] = "optimization"

    def __init__(
        self,
        *,
        slow_threshold_seconds: float = 300.0,
        min_slow_runs: int = 2,
    ) -> None:
        if slow_threshold_seconds <= 0:
            raise ValueError("slow_threshold_seconds must be positive")
        if min_slow_runs < 1:
            raise ValueError("min_slow_runs must be >= 1")
        self._threshold = slow_threshold_seconds
        self._min_slow = min_slow_runs

    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        runs = db.list_runs(ctx.db_path, states=sorted(db.TERMINAL_STATES), limit=ctx.max_runs)
        # project -> (durations, slow_durations)
        by_project: dict[str, list[float]] = defaultdict(list)
        slow_by_project: dict[str, list[float]] = defaultdict(list)
        for run in runs:
            seconds = _duration_seconds(run)
            if seconds is None:
                continue
            project = run.get("project") or ""
            if not project:
                continue
            by_project[project].append(seconds)
            if seconds >= self._threshold:
                slow_by_project[project].append(seconds)

        candidates: list[Candidate] = []
        for project, slow in sorted(slow_by_project.items()):
            if len(slow) < self._min_slow:
                continue
            total = len(by_project[project])
            avg_slow = sum(slow) / len(slow)
            frequency = len(slow) / total if total else 0.0
            # impact grows with how far the slow runs overshoot the threshold,
            # saturating at 4x the threshold so a single pathological run cannot
            # dominate the score.
            impact = min(1.0, (avg_slow / self._threshold - 1.0) / 3.0)
            candidates.append(
                Candidate(
                    kind=self.kind,
                    title=f"Optimize slow runs in {project}",
                    project_ref=project,
                    body=(
                        f"{len(slow)} of {total} completed runs in {project} took "
                        f"at least {int(self._threshold)}s (avg {int(avg_slow)}s). "
                        "Profile the slow path and cut avoidable work."
                    ),
                    signals={
                        "impact": impact,
                        "frequency": frequency,
                        "effort": 0.5,
                        "risk": 0.2,
                    },
                    source=self.name,
                )
            )
        return candidates
