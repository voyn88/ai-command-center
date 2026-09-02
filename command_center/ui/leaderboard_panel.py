"""Agent Leaderboard panel: renders `command_center.leaderboard`'s tiers.

Read-only, like `ui.portfolio_overview_panel`: this file only presents the
plain-data `Leaderboard` the domain module computes from
`runtime.runs_read.list_unified_runs()`, so the tier/trend rules stay
independent of Streamlit and unit-testable on their own (`tests/test_leaderboard.py`).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import leaderboard
from command_center.runtime import runs_read

_TIER_COLOR: dict[str, str] = {
    leaderboard.TIER_TOP: "green",
    leaderboard.TIER_CHALLENGER: "blue",
    leaderboard.TIER_NEWCOMER: "gray",
    leaderboard.TIER_RISKY: "red",
}

_TIER_LABEL: dict[str, str] = {
    leaderboard.TIER_TOP: "Top",
    leaderboard.TIER_CHALLENGER: "Challenger",
    leaderboard.TIER_NEWCOMER: "Newcomer",
    leaderboard.TIER_RISKY: "Risky",
}

_TREND_DISPLAY: dict[str, tuple[str, str]] = {
    leaderboard.TREND_UP: ("↑ растёт", "green"),
    leaderboard.TREND_DOWN: ("↓ падает", "red"),
    leaderboard.TREND_FLAT: ("→ стабильно", "gray"),
    leaderboard.TREND_INSUFFICIENT_DATA: ("— недостаточно данных", "gray"),
}


def _format_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def _render_entry(entry: leaderboard.AgentLeaderboardEntry) -> None:
    with st.container(border=True):
        header = st.columns([2, 1, 2])
        header[0].markdown(f"**{entry.agent}**")
        header[1].badge(_TIER_LABEL[entry.tier], color=_TIER_COLOR[entry.tier])
        trend_text, trend_color = _TREND_DISPLAY[entry.trend]
        header[2].badge(trend_text, color=trend_color)

        cols = st.columns(4)
        cols[0].metric("Успешность", _format_rate(entry.success_rate))
        cols[1].metric("Оценено запусков", entry.scored_runs)
        cols[2].metric("Всего запусков", entry.total_runs)
        delta_text = "—" if entry.trend_delta is None else f"{entry.trend_delta:+.0%}"
        cols[3].metric("Δ тренда", delta_text)

        if entry.trend != leaderboard.TREND_INSUFFICIENT_DATA:
            st.caption(
                f"Ранее: {_format_rate(entry.earlier_success_rate)} · "
                f"сейчас: {_format_rate(entry.recent_success_rate)} · "
                f"последний запуск: {entry.last_run_at or '—'}"
            )
        else:
            st.caption(f"последний запуск: {entry.last_run_at or '—'}")


def render_leaderboard_panel(*, db_path: Path, root: Path) -> None:
    st.markdown("#### Лидерборд агентов")
    st.caption(
        "Тир каждого агента (Top / Challenger / Newcomer / Risky) считается по всей "
        "истории запусков, а тренд — по сравнению более ранней и более свежей "
        "половины его оценённых запусков. Только чтение — источник тот же "
        "объединённый журнал запусков, что и страница «Журнал запусков»."
    )

    runs = runs_read.list_unified_runs(db_path, root=root)
    board = leaderboard.compute_leaderboard(runs)

    if not board.entries:
        st.info("Пока нет запусков с определённым агентом.")
        return

    for tier in leaderboard.TIER_ORDER:
        tier_entries = [e for e in board.entries if e.tier == tier]
        if not tier_entries:
            continue
        st.markdown(f"##### {_TIER_LABEL[tier]} ({len(tier_entries)})")
        for entry in tier_entries:
            _render_entry(entry)
