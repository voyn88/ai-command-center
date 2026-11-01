"""Tests for command_center.bench.report."""

from __future__ import annotations

from command_center.bench.report import render_weekly_report
from command_center.bench.types import CategoryScore, RankedAgent


def _agent(agent_id: str, stable: float, raw: float, *, provisional: bool = False) -> RankedAgent:
    return RankedAgent(
        agent_id=agent_id,
        stable_score=stable,
        raw_score=raw,
        provisional=provisional,
        categories=(
            CategoryScore(
                category="critical",
                weighted_rate=raw,
                cases_run=3,
                cases_passed=2,
                lower_bound=raw / 2,
            ),
        ),
        reasons=(f"raw score this week: {raw:.1f}/100",),
    )


def test_render_weekly_report_lists_agents_in_ranked_order():
    ranked = [_agent("first", 90.0, 85.0), _agent("second", 40.0, 40.0)]
    markdown = render_weekly_report("2026-09-01", ranked)
    assert markdown.index("first") < markdown.index("second")


def test_render_weekly_report_includes_week_and_scores():
    ranked = [_agent("solo", 77.5, 80.0)]
    markdown = render_weekly_report("2026-09-01", ranked)
    assert "2026-09-01" in markdown
    assert "77.5" in markdown
    assert "80.0" in markdown


def test_render_weekly_report_marks_provisional_status():
    ranked = [_agent("newcomer", 50.0, 50.0, provisional=True)]
    markdown = render_weekly_report("2026-09-01", ranked)
    assert "provisional" in markdown


def test_render_weekly_report_includes_reasons():
    ranked = [_agent("solo", 77.5, 80.0)]
    markdown = render_weekly_report("2026-09-01", ranked)
    assert "raw score this week: 80.0/100" in markdown


def test_render_weekly_report_empty_leaderboard_still_renders():
    markdown = render_weekly_report("2026-09-01", [])
    assert "2026-09-01" in markdown
    assert markdown.endswith("\n")
