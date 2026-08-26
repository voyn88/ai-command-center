"""Streamlit panel for the persistent daily self-audit service."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import streamlit as st

from command_center.daily_audit import DailyAuditStore, parse_time, utc_now
from command_center.runtime import db as runtime_db

LAUNCH_AGENT_LABEL = "com.ai-command-center.daily-audit"


def _local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return parse_time(value).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return value


def launch_agent_status(label: str = LAUNCH_AGENT_LABEL) -> tuple[bool, str]:
    """Read launchd state without changing or restarting the service.

    The service is bootstrapped into the ``system`` domain (a LaunchDaemon),
    not the per-user ``gui/<uid>`` domain: a GUI-domain agent only runs while
    that user has an active, logged-in Aqua session, which would make
    autonomous operation depend on the laptop being open and unlocked.
    """
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return False, "launchd недоступен"
    try:
        result = subprocess.run(
            [launchctl, "print", f"system/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "статус недоступен"
    if result.returncode != 0:
        return False, "не установлен"
    running = "\n\tstate = running\n" in result.stdout
    return running, "работает" if running else "установлен, но не запущен"


def _latest_daily_runs(db_path: Path) -> list[dict]:
    return [
        run
        for run in runtime_db.list_runs(db_path, limit=100)
        if run.get("launch_source") == "daily_self_audit"
    ][:10]


def render_daily_audit_page(db_path: Path) -> None:
    store = DailyAuditStore(db_path)
    status = store.status() or {}
    campaigns = store.list_campaigns(limit=10)
    runs = _latest_daily_runs(db_path)
    agent_running, agent_label = launch_agent_status()

    st.subheader("Ежедневный аудит")
    st.caption(
        "Постоянный продуктовый и инженерный контроль: пользовательский путь, UI/UX, "
        "очереди и зависимости, remediation, полный набор тестов, Review, CI и merge."
    )

    active = status.get("active_campaign")
    state_label = "Выполняется" if active else "Ожидает расписания"
    cols = st.columns(4)
    cols[0].metric("Сервис", agent_label)
    cols[1].metric("Кампания", state_label)
    cols[2].metric("Следующий запуск", _local_time(status.get("next_run_at")))
    cols[3].metric("Последний результат", campaigns[0]["status"] if campaigns else "—")

    if not agent_running:
        st.error(
            "Фоновый launchd-сервис не работает. Ручной запрос сохранится, "
            "но не будет обработан до запуска сервиса."
        )

    if active:
        st.info(
            f"Кампания `{active['id'][:8]}` выполняется с "
            f"{_local_time(active.get('started_at'))}."
        )
    else:
        if st.button(
            "Запустить аудит сейчас",
            type="primary",
            key="daily_audit_run_now",
        ):
            if store.request_run_now(now=utc_now()):
                st.success("Запуск поставлен в очередь фонового сервиса.")
                st.rerun()
            else:
                st.info("Кампания уже выполняется.")

    if runs:
        st.markdown("#### Последние запуски агентов")
        st.dataframe(
            [
                {
                    "Run": run["id"][:8],
                    "Тип": run["task_type"],
                    "Состояние": run["state"],
                    "Создан": _local_time(run.get("created_at")),
                    "Завершён": _local_time(run.get("completed_at")),
                }
                for run in runs
            ],
            width="stretch",
            hide_index=True,
        )

    st.markdown("#### История кампаний")
    if not campaigns:
        st.caption("Кампаний пока нет.")
    else:
        st.dataframe(
            [
                {
                    "Кампания": item["id"][:8],
                    "Статус": item["status"],
                    "Начало": _local_time(item.get("started_at")),
                    "Завершение": _local_time(item.get("finished_at")),
                    "PR": item.get("pull_request_url") or "—",
                    "Target verified": bool(item.get("target_verified")),
                }
                for item in campaigns
            ],
            width="stretch",
            hide_index=True,
        )
