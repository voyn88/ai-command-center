"""Orchestrates one weekly hard-bench pass (VOYN-AGT-HARD-BENCH).

Ties `grading.run_suite` (execute), `scorer.rank_agents` (score against
history for a stable scale), `history` (persist raw evidence and the
rendered artifact) and `report.render_weekly_report` (render) into the single
operation a scheduler tick calls. Shaped after `AdvisorService`/
`DailyAuditService`: a thin orchestrator over already-tested pure pieces,
not a place new logic should be added.

Wiring this to a real cadence (a systemd timer calling into a CLI, or a
lease-guarded scheduler like `daily_audit.DailyAuditService`) and to a real
`Grader` that invokes actual agents (see `runtime.providers`) are the two
follow-on integration points; both are deliberately out of this module so
that scoring/ranking correctness can be verified without either.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from command_center.bench import history
from command_center.bench.cases import CASES
from command_center.bench.grading import Grader, run_suite
from command_center.bench.report import render_weekly_report
from command_center.bench.scorer import rank_agents
from command_center.bench.types import BenchCase, RankedAgent


@dataclass(frozen=True)
class WeeklyBenchReport:
    week_of: str
    ranked: tuple[RankedAgent, ...]
    markdown: str


class BenchService:
    def __init__(self, bench_dir: Path):
        self._bench_dir = bench_dir

    def run_weekly_pass(
        self,
        week_of: str,
        agent_ids: Sequence[str],
        grader: Grader,
        *,
        cases: Sequence[BenchCase] = CASES,
    ) -> WeeklyBenchReport:
        """Run, score, persist, and render one weekly pass.

        Raises `history.WeekAlreadyRecorded` if `week_of` was already run —
        a weekly report is produced at most once per week, same as
        `daily_audit`'s at-most-once-per-interval guarantee. The suite runs
        before anything is persisted, so a grader failure mid-suite leaves no
        partial run behind for `week_of` to retry against.
        """
        results_by_agent = run_suite(agent_ids, grader, cases=cases)

        run_id = history.record_run(self._bench_dir, week_of)

        cases_by_id = {case.id: case for case in cases}
        for agent_id, results in results_by_agent.items():
            for result in results:
                category = cases_by_id[result.case_id].category
                history.record_case_result(self._bench_dir, run_id, category, result)

        previous_stable_scores = history.latest_stable_scores(self._bench_dir)
        previous_run_counts = history.run_counts(self._bench_dir)
        ranked = rank_agents(
            results_by_agent,
            previous_stable_scores=previous_stable_scores,
            previous_run_counts=previous_run_counts,
            cases_by_id=cases_by_id,
        )

        for agent in ranked:
            history.record_agent_score(
                self._bench_dir,
                run_id,
                agent.agent_id,
                raw_score=agent.raw_score,
                stable_score=agent.stable_score,
                provisional=agent.provisional,
            )

        markdown = render_weekly_report(week_of, ranked)
        history.record_report(self._bench_dir, run_id, week_of, markdown)

        return WeeklyBenchReport(week_of=week_of, ranked=tuple(ranked), markdown=markdown)
