"""Renders the weekly bench leaderboard as a Markdown artifact.

Mirrors `runtime.reports`'s convention for a run report: a plain, immutable,
human-readable document rather than a UI-only view — this text is what
`db.bench_store` persists verbatim against the week it was produced, so a
past week's leaderboard is always exactly reproducible from the artifact
even without re-querying the store.
"""

from __future__ import annotations

from collections.abc import Sequence

from command_center.bench.cases import CASES
from command_center.bench.types import RankedAgent


def render_weekly_report(week_of: str, ranked: Sequence[RankedAgent]) -> str:
    """Render the ranked leaderboard for `week_of` (an ISO date, week start).

    `ranked` must already be in leaderboard order (see `scorer.rank_agents`,
    which sorts by stable score descending) — this function renders order,
    it does not decide it.
    """
    lines: list[str] = [
        f"# Agent hard-bench weekly report — {week_of}",
        "",
        f"{len(CASES)} cases across "
        f"{len({case.category for case in CASES})} categories "
        "(critical, incident, code, ux, security). Ranked by stable score — "
        "an EWMA of this week's raw score against prior weeks, so a single "
        "noisy run cannot flip the leaderboard on its own.",
        "",
        "| Rank | Agent | Stable score | Raw score this week | Status |",
        "| ---: | :--- | ---: | ---: | :--- |",
    ]

    for index, agent in enumerate(ranked, start=1):
        status = "provisional" if agent.provisional else "stable"
        lines.append(
            f"| {index} | {agent.agent_id} | {agent.stable_score:.1f} | "
            f"{agent.raw_score:.1f} | {status} |"
        )

    lines.append("")

    for index, agent in enumerate(ranked, start=1):
        lines.append(f"## {index}. {agent.agent_id}")
        lines.append("")
        lines.append(
            "| Category | Passed | Weighted rate | 95% lower bound |"
        )
        lines.append("| :--- | ---: | ---: | ---: |")
        for cat in agent.categories:
            lines.append(
                f"| {cat.category} | {cat.cases_passed}/{cat.cases_run} | "
                f"{cat.weighted_rate:.1f} | {cat.lower_bound:.1f} |"
            )
        lines.append("")
        for reason in agent.reasons:
            lines.append(f"- {reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
