"""Board/Investor view — one weekly, one-page, jargon-free summary.

`VOYN-MIN-BOARD-LAUNCH`: a board member or investor never opens Kanban, the
Execution Center or a run log, and none of this app's other screens are
written for that reader — every one of them assumes "operator" vocabulary
(runs, launches, Blocker/High findings, worktrees). This module builds and
renders a single page a non-technical executive can read in under a minute:
overall health, what shipped this week, the risks that need their attention,
and per-project status, entirely in plain language.

Split into a pure builder (`build_weekly_summary`, no Streamlit import, unit
testable) and a renderer (`render_board_view`) — the same separation as
`command_center.ui.live_board`, so "what counts as a risk this week" can be
pinned down in tests without a Streamlit runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import streamlit as st

from command_center import models, project_config

# Plain-language health bands — deliberately not the operator vocabulary
# (Good/Attention/At Risk in `portfolio_intelligence`, or the five Kanban
# lanes): a board reader needs one word and one reason, not a status enum.
HEALTH_ON_TRACK = "На курсе"
HEALTH_WATCH = "Требует внимания"
HEALTH_AT_RISK = "Есть риск"

_HEALTH_ACCENT = {
    HEALTH_ON_TRACK: "#1a7f37",
    HEALTH_WATCH: "#9a6700",
    HEALTH_AT_RISK: "#cf222e",
}

# Priorities serious enough that a single blocked/attention task on them pushes
# the whole board to "Есть риск" rather than the milder "Требует внимания".
_HIGH_SEVERITY_PRIORITIES = frozenset({"High", "Critical"})

_MAX_RISKS_SHOWN = 8
_MAX_WINS_SHOWN = 8
_MAX_DECISIONS_SHOWN = 5


@dataclass(frozen=True)
class RiskItem:
    project: str
    title: str
    reason: str
    severity: str  # "Высокий" | "Средний"


@dataclass(frozen=True)
class ProjectRow:
    project_id: str
    display_name: str
    status_label: str
    health: str
    active: int
    done: int
    blocked: int


@dataclass(frozen=True)
class WeeklySummary:
    generated_at: datetime
    week_label: str
    overall_health: str
    overall_reason: str
    completed_titles: tuple[str, ...]
    completed_total: int
    risks: tuple[RiskItem, ...]
    risks_total: int
    decisions_needed: tuple[str, ...]
    projects: tuple[ProjectRow, ...]


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _week_label(now: datetime) -> str:
    start = now - timedelta(days=now.weekday())
    end = start + timedelta(days=6)
    return f"неделя {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}"


def _completed_this_week(tasks: list[dict], *, now: datetime, days: int = 7) -> list[dict]:
    cutoff = now - timedelta(days=days)
    completed = []
    for task in tasks:
        if task.get("status") != "Done":
            continue
        ts = _parse_ts(task.get("updated_at")) or _parse_ts(task.get("created_at"))
        if ts is not None and ts >= cutoff:
            completed.append(task)
    completed.sort(key=lambda t: t.get("updated_at") or t.get("created_at") or "", reverse=True)
    return completed


def _needs_attention(task: dict) -> bool:
    """Mirrors `read_model.task_snapshot`'s per-task attention rule (a resolved
    task needs a real regression flag; an open one needs an explicit launch
    status) so this page's risk count never drifts from the canonical one."""
    status = task.get("status")
    if status in ("Done", "Closed"):
        return bool(task.get("regressed_after_done"))
    return task.get("launch_status") == "Requires Attention"


def _severity_for(task: dict) -> str:
    return "Высокий" if task.get("priority") in _HIGH_SEVERITY_PRIORITIES else "Средний"


def _blocked_reason(task: dict, tasks_by_id: dict[str, dict]) -> str:
    unmet = models.unmet_dependencies(task, tasks_by_id)
    if not unmet:
        return "Заблокирована без указанной причины"
    names = ", ".join(
        (tasks_by_id[dep_id].get("title") or "(без названия)")[:60]
        if dep_id in tasks_by_id
        else "(связанная задача удалена)"
        for dep_id in unmet
    )
    return f"Ждёт завершения: {names}"


def _collect_risks(tasks: list[dict], tasks_by_id: dict[str, dict]) -> list[RiskItem]:
    risks: list[RiskItem] = []
    for task in tasks:
        if task.get("status") == "Blocked":
            risks.append(
                RiskItem(
                    project=task.get("project") or "—",
                    title=(task.get("title") or "Без названия")[:80],
                    reason=_blocked_reason(task, tasks_by_id),
                    severity=_severity_for(task),
                )
            )
        elif _needs_attention(task):
            risks.append(
                RiskItem(
                    project=task.get("project") or "—",
                    title=(task.get("title") or "Без названия")[:80],
                    reason="Нужно решение человека, чтобы двигаться дальше",
                    severity=_severity_for(task),
                )
            )
    # Highest severity first, so a one-page cut never hides a Critical/High
    # item behind a screenful of Medium ones.
    risks.sort(key=lambda r: 0 if r.severity == "Высокий" else 1)
    return risks


def _overall_health(risks: list[RiskItem]) -> tuple[str, str]:
    high = sum(1 for r in risks if r.severity == "Высокий")
    if high:
        return HEALTH_AT_RISK, f"Высокоприоритетных рисков: {high}"
    if risks:
        return HEALTH_WATCH, f"Требуют внимания задач: {len(risks)}"
    return HEALTH_ON_TRACK, "Заблокированных задач и открытых рисков нет"


def _project_rows(
    tasks: list[dict],
    project_statuses: dict[str, str],
) -> list[ProjectRow]:
    rows: list[ProjectRow] = []
    for project_id in models.PROJECT_IDS:
        project_tasks = [
            t for t in tasks if project_config.project_matches(t.get("project"), project_id)
        ]
        if not project_tasks:
            continue
        active = sum(1 for t in project_tasks if t.get("status") in ("Backlog", "Next", "In Progress", "Review"))
        done = sum(1 for t in project_tasks if t.get("status") == "Done")
        blocked = sum(1 for t in project_tasks if t.get("status") == "Blocked")
        high_attention = any(
            (t.get("status") == "Blocked" or _needs_attention(t)) and t.get("priority") in _HIGH_SEVERITY_PRIORITIES
            for t in project_tasks
        )
        if high_attention:
            health = HEALTH_AT_RISK
        elif blocked:
            health = HEALTH_WATCH
        else:
            health = HEALTH_ON_TRACK
        rows.append(
            ProjectRow(
                project_id=project_id,
                display_name=project_config.DISPLAY_NAMES.get(project_id, project_id),
                status_label=project_statuses.get(project_id) or "Нет данных",
                health=health,
                active=active,
                done=done,
                blocked=blocked,
            )
        )
    return rows


def build_weekly_summary(
    tasks: list[dict],
    project_statuses: dict[str, str],
    *,
    now: datetime,
) -> WeeklySummary:
    """Pure assembly of the board page's data from the same task list every
    other screen reads — no separate "board truth", so this page can never
    show a different reality than Kanban does."""
    tasks_by_id = {t["id"]: t for t in tasks if t.get("id")}
    completed = _completed_this_week(tasks, now=now)
    risks = _collect_risks(tasks, tasks_by_id)
    overall_health, overall_reason = _overall_health(risks)
    decisions = [
        f"{r.project}: {r.title}" for r in risks if r.severity == "Высокий"
    ][:_MAX_DECISIONS_SHOWN]
    return WeeklySummary(
        generated_at=now,
        week_label=_week_label(now),
        overall_health=overall_health,
        overall_reason=overall_reason,
        completed_titles=tuple((t.get("title") or "Без названия") for t in completed[:_MAX_WINS_SHOWN]),
        completed_total=len(completed),
        risks=tuple(risks[:_MAX_RISKS_SHOWN]),
        risks_total=len(risks),
        decisions_needed=tuple(decisions),
        projects=tuple(_project_rows(tasks, project_statuses)),
    )


# --------------------------------------------------------------------------
# Rendering — plain language, one scroll, printable (browser Ctrl+P → PDF).
# --------------------------------------------------------------------------


def _health_badge(health: str) -> str:
    accent = _HEALTH_ACCENT.get(health, "#57606a")
    return (
        f"<span style='display:inline-block; padding:4px 14px; border-radius:999px; "
        f"font-weight:700; font-size:1.05rem; color:#fff; background:{accent}'>{health}</span>"
    )


def render_board_view(summary: WeeklySummary) -> None:
    st.markdown(
        f"### Сводка для совета директоров и инвесторов — {summary.week_label}",
        unsafe_allow_html=True,
    )
    st.caption(
        "Одна страница: что сделано, что требует решения, статус по направлениям. "
        "Совет: Ctrl+P (или ⌘+P) сохраняет эту страницу как PDF."
    )

    st.markdown(
        f"{_health_badge(summary.overall_health)}&nbsp;&nbsp;{summary.overall_reason}",
        unsafe_allow_html=True,
    )

    st.divider()

    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("#### Что сделано за неделю")
        if summary.completed_titles:
            for title in summary.completed_titles:
                st.markdown(f"- ✅ {title}")
            if summary.completed_total > len(summary.completed_titles):
                st.caption(f"…и ещё {summary.completed_total - len(summary.completed_titles)}")
        else:
            st.caption("На этой неделе завершённых задач нет.")

    with right:
        st.markdown("#### Риски, требующие внимания")
        if summary.risks:
            for risk in summary.risks:
                icon = "🔴" if risk.severity == "Высокий" else "🟡"
                st.markdown(f"{icon} **{risk.project}** — {risk.title}")
                st.caption(risk.reason)
            if summary.risks_total > len(summary.risks):
                st.caption(f"…и ещё {summary.risks_total - len(summary.risks)}")
        else:
            st.success("Открытых рисков нет.")

    if summary.decisions_needed:
        st.divider()
        st.markdown("#### Нужно решение руководства")
        for item in summary.decisions_needed:
            st.warning(item)

    st.divider()
    st.markdown("#### Статус по направлениям")
    if not summary.projects:
        st.caption("Нет задач ни по одному направлению.")
    else:
        for row in summary.projects:
            cols = st.columns([3, 2, 1, 1, 1])
            cols[0].markdown(f"**{row.display_name}**")
            cols[1].caption(row.status_label)
            cols[2].metric("В работе", row.active)
            cols[3].metric("Готово", row.done)
            cols[4].markdown(_health_badge(row.health), unsafe_allow_html=True)

    st.divider()
    st.caption(
        f"Сформировано автоматически из текущих задач: {summary.generated_at.strftime('%d.%m.%Y %H:%M')}."
    )
