"""Agent Metrics panel: one dashboard with the unified, normalized per-agent
metrics schema — quality, speed, cost, rollback rate, manual rework rate.

Read-only, like `portfolio_overview_panel`: it loads the same runtime data
every other Runs-adjacent page already reads (`runs_read.list_unified_runs`,
`completion`, `provider_attempt`) and hands it to the pure
`command_center.agent_metrics` projection, so the scoring rules live in one
Streamlit-free module and stay unit-testable there.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import agent_metrics
from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db
from command_center.runtime import runs_read

# quality/speed/cost: higher is better. rollback/manual rework: lower is
# better — so their color thresholds read the raw value directly instead of
# through `_goodness`.
_HIGHER_IS_BETTER = "higher"
_LOWER_IS_BETTER = "lower"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _goodness(value: float | None, *, direction: str) -> float | None:
    if value is None:
        return None
    return value if direction == _HIGHER_IS_BETTER else 1.0 - value


def _badge_color(value: float | None, *, direction: str) -> str:
    goodness = _goodness(value, direction=direction)
    if goodness is None:
        return "gray"
    if goodness >= 0.75:
        return "green"
    if goodness >= 0.4:
        return "orange"
    return "red"


_METRIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("quality", "Качество", _HIGHER_IS_BETTER),
    ("speed", "Скорость", _HIGHER_IS_BETTER),
    ("cost", "Стоимость (эффективность)", _HIGHER_IS_BETTER),
    ("rollback_rate", "Откаты", _LOWER_IS_BETTER),
    ("manual_rework_rate", "Ручные доработки", _LOWER_IS_BETTER),
)


def _render_agent_row(metrics: agent_metrics.AgentMetrics) -> None:
    with st.container(border=True):
        header = st.columns([2, 1])
        header[0].markdown(f"**{metrics.agent}**")
        header[1].caption(f"{metrics.sample_size} запусков")

        cols = st.columns(len(_METRIC_COLUMNS))
        for col, (field, label, direction) in zip(cols, _METRIC_COLUMNS):
            value = getattr(metrics, field)
            with col:
                st.caption(label)
                st.badge(_fmt_pct(value), color=_badge_color(value, direction=direction))

        raw = metrics.raw
        with st.expander("Исходные данные"):
            st.caption(
                f"COMPLETED: {raw.runs_completed} · FAILED: {raw.runs_failed} · "
                f"отменено: {raw.runs_cancelled} · записей completion: {raw.completions_total} · "
                f"требовали человека: {raw.requires_human_count} · с восстановлением: {raw.recovered_count}"
            )
            duration = (
                f"{raw.median_duration_seconds:.0f} с" if raw.median_duration_seconds is not None else "—"
            )
            attempts = (
                f"{raw.avg_attempts_per_run:.1f}" if raw.avg_attempts_per_run is not None else "—"
            )
            st.caption(f"медианная длительность: {duration} · попыток провайдера в среднем: {attempts}")


def render_agent_metrics_panel(api: runtime_api.ExecutionCenterAPI, *, root: Path) -> None:
    st.markdown("#### Метрики агентов")
    st.caption(
        "Единая нормализованная схема по каждому агенту: качество выполнения, скорость, "
        "стоимость, частота откатов и доля ручных доработок. Качество/скорость/стоимость — "
        "чем выше, тем лучше; откаты/ручные доработки — чем ниже, тем лучше. "
        "«—» означает отсутствие данных, а не ноль. Стоимость — прокси по числу попыток "
        "провайдера на запуск (`provider_attempt`), так как денежная стоимость пока не "
        "учитывается ни в одной таблице."
    )

    runs = runs_read.list_unified_runs(api.db_path, root=root)
    run_ids = [run["id"] for run in runs if run.get("id")]
    completions_by_run = runtime_db.get_completions_for_runs(api.db_path, run_ids)
    attempts_by_run = runtime_db.get_provider_attempts_for_runs(api.db_path, run_ids)

    metrics = agent_metrics.compute_agent_metrics(runs, completions_by_run, attempts_by_run)

    if not metrics:
        st.info("Нет запусков для расчёта метрик агентов.")
        return

    for row in metrics:
        _render_agent_row(row)
