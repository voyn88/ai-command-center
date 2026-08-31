"""Streamlit panel for the persistent daily self-audit service."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import streamlit as st

from command_center.daily_audit import DailyAuditStore, parse_time, utc_now
from command_center.runtime import db as runtime_db

LAUNCH_AGENT_LABEL = "com.ai-command-center.daily-audit"

# States a single `launchctl print <domain>/<label>` probe can resolve to.
_RUNNING = "running"
_STOPPED = "stopped"
_ABSENT = "absent"
_UNKNOWN = "unknown"

# Substrings launchctl uses when a domain exists but this process lacks the
# privilege to inspect it (e.g. another account's GUI domain). These must be
# treated as "could not verify", never folded into "not installed".
_PERMISSION_DENIED_MARKERS = (
    "permission denied",
    "not privileged",
    "operation not permitted",
)

# macOS reserves UIDs below 500 for system/service accounts.
_MIN_HUMAN_UID = 500


def _local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return parse_time(value).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return value


def _probe_domain(launchctl: str, target: str) -> str:
    """Probe one launchd domain target (e.g. ``system/<label>`` or
    ``gui/<uid>/<label>``), distinguishing "not installed" from "could not
    verify" so a permission failure on another account's domain is never
    reported as a false "not installed"."""
    try:
        result = subprocess.run(
            [launchctl, "print", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _UNKNOWN
    if result.returncode == 0:
        return _RUNNING if "\n\tstate = running\n" in result.stdout else _STOPPED
    stderr = (result.stderr or "").lower()
    if any(marker in stderr for marker in _PERMISSION_DENIED_MARKERS):
        return _UNKNOWN
    return _ABSENT


def _other_account_uids(current_uid: int) -> list[int]:
    """Best-effort enumeration of other local accounts' UIDs, used to look
    for a legacy per-user LaunchAgent left behind on another account. Returns
    an empty list if accounts cannot be enumerated (missing `dscl`, etc.);
    that only narrows which accounts get probed, it never claims a clean
    result on their behalf."""
    dscl = shutil.which("dscl")
    if dscl is None:
        return []
    try:
        result = subprocess.run(
            [dscl, ".", "-list", "/Users", "UniqueID"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    uids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            uid = int(parts[1])
        except ValueError:
            continue
        if uid >= _MIN_HUMAN_UID and uid != current_uid:
            uids.add(uid)
    return sorted(uids)


@dataclass(frozen=True)
class DaemonStatus:
    """Coexistence-aware launchd status: the system LaunchDaemon plus every
    legacy per-user (`gui/<uid>`) LaunchAgent domain this process can see."""

    launchctl_available: bool
    system_state: str = _ABSENT
    own_legacy_state: str = _ABSENT
    other_legacy_states: dict = field(default_factory=dict)  # uid -> state

    @property
    def system_running(self) -> bool:
        return self.system_state == _RUNNING

    @property
    def legacy_active(self) -> bool:
        """A legacy GUI-domain agent is confirmed loaded somewhere this
        process could inspect (running or stopped-but-loaded)."""
        confirmed = (_RUNNING, _STOPPED)
        if self.own_legacy_state in confirmed:
            return True
        return any(state in confirmed for state in self.other_legacy_states.values())

    @property
    def legacy_unverified(self) -> bool:
        """True when some legacy domain could not be inspected -- typically
        another account's GUI domain, which requires elevated privilege."""
        if self.own_legacy_state == _UNKNOWN:
            return True
        return any(state == _UNKNOWN for state in self.other_legacy_states.values())

    def summary(self) -> tuple[bool, str]:
        """Overall (is_running, human label) used for the metric tile."""
        if not self.launchctl_available:
            return False, "launchd недоступен"
        if self.system_running:
            if self.legacy_active:
                return True, "работает (обнаружен устаревший gui-агент)"
            return True, "работает"
        if self.legacy_active:
            return False, "устаревший gui-агент активен, требуется миграция"
        if self.system_state == _STOPPED:
            return False, "установлен, но не запущен"
        return False, "не установлен"


def launch_agent_status(label: str = LAUNCH_AGENT_LABEL) -> DaemonStatus:
    """Read launchd state without changing or restarting the service.

    Both the system LaunchDaemon domain and every reachable legacy
    `gui/<uid>` domain are always probed -- a running system daemon does not
    short-circuit legacy detection, so an old per-user LaunchAgent left over
    from an upgrade (or still running alongside the daemon) is reported
    instead of silently hidden.
    """
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return DaemonStatus(launchctl_available=False)

    system_state = _probe_domain(launchctl, f"system/{label}")

    current_uid = os.getuid()
    own_legacy_state = _probe_domain(launchctl, f"gui/{current_uid}/{label}")

    other_legacy_states = {
        uid: _probe_domain(launchctl, f"gui/{uid}/{label}")
        for uid in _other_account_uids(current_uid)
    }

    return DaemonStatus(
        launchctl_available=True,
        system_state=system_state,
        own_legacy_state=own_legacy_state,
        other_legacy_states=other_legacy_states,
    )


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
    daemon = launch_agent_status()
    agent_running, agent_label = daemon.summary()

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

    if daemon.legacy_active:
        st.warning(
            "Обнаружен устаревший LaunchAgent в пользовательском домене "
            "(gui/<uid>) наряду с (или вместо) системного LaunchDaemon. "
            "Это может привести к двойному запуску кампаний. Выполните "
            "миграцию, см. раздел «Миграция» в docs/DAILY_SELF_AUDIT.md."
        )

    if daemon.legacy_unverified:
        st.caption(
            "Не удалось проверить устаревший LaunchAgent в других учётных "
            "записях этого Mac: недостаточно прав для чтения их "
            "gui-домена. Проверьте эти учётные записи вручную (например, "
            "из-под root) прежде чем считать миграцию завершённой."
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
