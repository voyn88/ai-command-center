"""Feedback collector: surface recurring run failures as reliability feedback.

Signal source is the local task/run history (read through the db facade). It
groups terminal runs by project, counts the non-successful outcomes
(``FAILED``/``CANCELLED``/``INTERRUPTED``/``UNKNOWN``) and, when a project crosses
a failure threshold, raises one ``feedback`` candidate summarizing the most
common failure reason. Like every built-in collector it is local and free, and
returns **zero** candidates when nothing has been failing.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import ClassVar

from command_center.advisor.collectors.base import Collector
from command_center.advisor.types import Candidate, CollectorContext
from command_center.runtime import db

#: Terminal states that count as "not a clean success".
_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "INTERRUPTED", "UNKNOWN"})


class FeedbackCollector(Collector):
    """Raise a ``feedback`` proposal per project with a cluster of failed runs."""

    name: ClassVar[str] = "feedback"
    kind: ClassVar[str] = "feedback"

    def __init__(self, *, min_failures: int = 2) -> None:
        if min_failures < 1:
            raise ValueError("min_failures must be >= 1")
        self._min_failures = min_failures

    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        runs = db.list_runs(ctx.db_path, states=sorted(db.TERMINAL_STATES), limit=ctx.max_runs)
        totals: dict[str, int] = defaultdict(int)
        failures: dict[str, int] = defaultdict(int)
        reasons: dict[str, Counter] = defaultdict(Counter)
        for run in runs:
            project = run.get("project") or ""
            if not project:
                continue
            totals[project] += 1
            if run.get("state") in _FAILURE_STATES:
                failures[project] += 1
                reasons[project][run.get("failure_reason") or run.get("state") or "unknown"] += 1

        candidates: list[Candidate] = []
        for project, failed in sorted(failures.items()):
            if failed < self._min_failures:
                continue
            total = totals[project]
            top_reason, top_count = reasons[project].most_common(1)[0]
            rate = failed / total if total else 0.0
            candidates.append(
                Candidate(
                    kind=self.kind,
                    title=f"Investigate recurring failures in {project}",
                    project_ref=project,
                    body=(
                        f"{failed} of {total} recent runs in {project} did not "
                        f"complete cleanly; the most common reason was "
                        f"'{top_reason}' ({top_count}). Triage the pattern before "
                        "it erodes trust in the pipeline."
                    ),
                    signals={
                        # A high failure rate is high-impact feedback; frequency
                        # tracks the same rate, effort is unknown-ish, risk of
                        # acting on reliability work is low.
                        "impact": min(1.0, rate + 0.2),
                        "frequency": rate,
                        "effort": 0.5,
                        "risk": 0.15,
                    },
                    source=self.name,
                )
            )
        return candidates
