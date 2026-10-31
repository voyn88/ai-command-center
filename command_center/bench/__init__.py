"""Agent hard-bench-set (VOYN-AGT-HARD-BENCH).

Runs a fixed set of hard cases (critical operational judgment, incident
response, code, UX, security) against a set of agents and produces a weekly
leaderboard ranked on a stable (EWMA-smoothed, confidence-aware) scale, so a
single noisy week cannot reorder it.

Public surface::

    BenchCase, CaseResult, CategoryScore, RankedAgent  -- value objects
    CASES, CASES_BY_ID                                  -- the fixed case set
    Grader, run_suite                                   -- execution seam
    rank_agents                                          -- scoring/ranking
    render_weekly_report                                 -- the artifact
    BenchService, WeeklyBenchReport                      -- the orchestrator

Persistence lives in `command_center.bench.history`, not here, matching the
split between domain logic and the store elsewhere in this codebase.
"""

from __future__ import annotations

from command_center.bench.cases import CASES, CASES_BY_ID
from command_center.bench.grading import Grader, GraderMismatchError, run_suite
from command_center.bench.report import render_weekly_report
from command_center.bench.scorer import rank_agents, wilson_lower_bound
from command_center.bench.service import BenchService, WeeklyBenchReport
from command_center.bench.types import BenchCase, CaseResult, CategoryScore, RankedAgent

__all__ = [
    "BenchCase",
    "CaseResult",
    "CategoryScore",
    "RankedAgent",
    "CASES",
    "CASES_BY_ID",
    "Grader",
    "GraderMismatchError",
    "run_suite",
    "rank_agents",
    "wilson_lower_bound",
    "render_weekly_report",
    "BenchService",
    "WeeklyBenchReport",
]
