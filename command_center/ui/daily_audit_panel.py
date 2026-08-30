"""Streamlit panel for the persistent daily self-audit service."""

from __future__ import annotations

import os
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


def _probe_launchctl(launchctl: str, target: str) -> tuple[bool, bool]:
    """Return (installed, running) for a `launchctl print` domain/service target."""
    try:
        result = subprocess.run(
            [launchctl, "print", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, False
    if result.returncode != 0:
        return False, False
    running = "\n\tstate = running\n" in result.stdout
    return True, running


def _legacy_gui_uids() -> list[int]:
    """UIDs to probe for a pre-migration per-user GUI agent.

    Enumerates every real user account via `dscl` (macOS reserves UIDs below
    500 for system/service accounts) so an agent left loaded under another
    logged-in account is not missed. Falls back to just the current process
    UID when account enumeration is unavailable.
    """
    uids = {os.getuid()}
    dscl = shutil.which("dscl")
    if dscl is None:
        return sorted(uids)
    try:
        result = subprocess.run(
            [dscl, ".", "-list", "/Users", "UniqueID"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return sorted(uids)
    if result.returncode != 0:
        return sorted(uids)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            uid = int(parts[1])
        except ValueError:
            continue
        if uid >= 500:
            uids.add(uid)
    return sorted(uids)


def launch_agent_status(label: str = LAUNCH_AGENT_LABEL) -> tuple[bool, str]:
    """Read launchd state without changing or restarting the service.

    Always probes the `system/` LaunchDaemon domain *and* every real user's
    legacy `gui/<uid>/` domain, so a still-loaded pre-migration agent is
    surfaced even when the system daemon is already healthy (and vice versa)
    instead of being hidden behind a short-circuited lookup.
    """
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return False, "launchd недоступен"

    system_installed, system_running = _probe_launchctl(launchctl, f"system/{label}")

    legacy_installed_uids = []
    legacy_running = False
    for uid in _legacy_gui_uids():
        installed, running = _probe_launchctl(launchctl, f"gui/{uid}/{label}")
        if installed:
            legacy_installed_uids.append(uid)
        if running:
            legacy_running = True

    if legacy_installed_uids:
        uid_list = ", ".join(str(uid) for uid in legacy_installed_uids)
        legacy_note = (
            f"обнаружен устаревший gui-агент (uid {uid_list}) — выполните миграцию"
        )
        if system_installed:
            base = "работает" if system_running else "установлен, но не запущен"
            return system_running, f"{base}; {legacy_note}"
        return legacy_running, f"не мигрирован; {legacy_note}"

    if not system_installed:
        return False, "не установлен"
    return system_running, "работает" if system_running else "установлен, но не запущен"


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
