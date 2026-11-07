"""Fleet page — read-only Streamlit view of the autonomous PostgreSQL contour.

Presentation only. Every query lives in :mod:`command_center.db.fleet_read`;
this module opens the pool when ``AICC_PG_*`` is set, asks the store for each
section, and paints the result. There is no write widget and no import of any
mutation store.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from command_center.db.fleet_read import (
    FleetReadStore,
    ensure_pool,
    postgres_configured,
)
from command_center.ui import content_area


def render(store: FleetReadStore | None = None) -> None:
    """Render the Fleet page, or explain why it cannot run without Postgres."""
    content_area.page_header(
        "Fleet",
        "Автономный контур из PostgreSQL: backlog, очередь, попытки исполнителей, "
        "monitor findings и смерженные PR. Только чтение — без записи.",
    )

    if not postgres_configured():
        st.info(
            "Fleet скрыт, пока не заданы `AICC_PG_*` (нужен как минимум "
            "`AICC_PG_HOST`). Страница читает канонический Postgres store, а не "
            "локальный `data/`."
        )
        return

    try:
        ensure_pool()
    except Exception as exc:  # noqa: BLE001 — surface config/connectivity to the operator
        st.error(f"Не удалось открыть пул PostgreSQL: {exc}")
        return

    reader = store if store is not None else FleetReadStore()

    try:
        tasks = reader.tasks_by_status_wave()
        queue = reader.queue_by_state()
        attempts = reader.recent_attempts_per_executor()
        findings = reader.open_monitor_findings()
        merges = reader.merged_prs_per_day()
    except Exception as exc:  # noqa: BLE001 — read failures must not crash the shell
        st.error(f"Ошибка чтения Fleet из PostgreSQL: {exc}")
        return

    _tasks_section(tasks)
    _queue_section(queue)
    _attempts_section(attempts)
    _findings_section(findings)
    _merges_section(merges)


def _tasks_section(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### Задачи по статусу и волне")
    if not rows:
        st.caption("В `backlog_task` пока нет строк.")
        return
    st.dataframe(rows, width="stretch", hide_index=True)


def _queue_section(payload: dict[str, Any]) -> None:
    st.markdown("#### Очередь по состоянию")
    counts = payload.get("counts") or []
    if counts:
        cols = st.columns(max(len(counts), 1))
        for col, row in zip(cols, counts):
            col.metric(str(row["state"]), int(row["count"]))
    else:
        st.caption("Очередь `work_item` пуста.")

    dead = payload.get("dead_reasons") or []
    attempt_reasons = payload.get("attempt_reasons") or []
    left, right = st.columns(2)
    with left:
        st.caption("Последние dead_reason")
        if dead:
            st.dataframe(dead, width="stretch", hide_index=True)
        else:
            st.caption("Нет.")
    with right:
        st.caption("Последние outcome_reason попыток")
        if attempt_reasons:
            st.dataframe(attempt_reasons, width="stretch", hide_index=True)
        else:
            st.caption("Нет.")


def _attempts_section(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### Недавние попытки по исполнителю")
    if not rows:
        st.caption("В `work_attempt_public` пока нет попыток.")
        return
    st.dataframe(rows, width="stretch", hide_index=True)


def _findings_section(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### Открытые monitor findings")
    if not rows:
        st.caption("Открытых findings нет.")
        return
    st.dataframe(rows, width="stretch", hide_index=True)


def _merges_section(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### Смерженные PR по дням")
    st.caption("Считается по `backlog_evidence` с `kind = 'sha'` (merge receipt).")
    if not rows:
        st.caption("За выбранный период merge receipts нет.")
        return
    st.dataframe(rows, width="stretch", hide_index=True)
