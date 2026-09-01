"""Streamlit panel for the persistent daily self-audit service."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from command_center.daily_audit import DailyAuditStore, parse_time, utc_now
from command_center.runtime import db as runtime_db

LAUNCH_AGENT_LABEL = "com.ai-command-center.daily-audit"

# launchd's own vocabulary for "no such service is loaded in this domain".
# Only these mean the label is genuinely absent; every other non-zero result
# (permission, timeout, unexpected launchctl error) must be treated as
# unverified -- reporting it as "absent" would hide a legacy agent this
# process simply could not see.
_NOT_FOUND_MARKERS = (
    "could not find service",
    "no such process",
    "3: no such process",
)
_PERMISSION_MARKERS = (
    "permission denied",
    "operation not permitted",
    "not privileged",
    "eperm",
)

STATE_INSTALLED = "installed"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

_MIN_ACCOUNT_UID = 500


@dataclass(frozen=True)
class DomainProbe:
    """Result of asking launchd about one label in one domain."""

    state: str  # STATE_INSTALLED, STATE_ABSENT or STATE_UNKNOWN
    running: bool
    detail: str


@dataclass(frozen=True)
class DaemonStatus:
    """Aggregated view of the system daemon and every legacy GUI agent.

    Coverage of other accounts' GUI domains is best-effort: a normal, non-root
    user cannot inspect another user's launchd session, so those probes -- and
    a failed account enumeration itself -- surface as unverified rather than
    silently counting as "clear".
    """

    system: DomainProbe
    own_legacy: DomainProbe
    other_legacy: dict[int, DomainProbe]
    enumeration_ok: bool
    enumeration_detail: str

    @property
    def legacy_running_uids(self) -> tuple[int, ...]:
        uids = [uid for uid, probe in self.other_legacy.items() if probe.state == STATE_INSTALLED and probe.running]
        if self.own_legacy.state == STATE_INSTALLED and self.own_legacy.running:
            uids.append(os.getuid())
        return tuple(sorted(uids))

    @property
    def legacy_installed_uids(self) -> tuple[int, ...]:
        uids = [uid for uid, probe in self.other_legacy.items() if probe.state == STATE_INSTALLED]
        if self.own_legacy.state == STATE_INSTALLED:
            uids.append(os.getuid())
        return tuple(sorted(uids))

    @property
    def legacy_unverified(self) -> tuple[str, ...]:
        """Scopes this process could not conclusively clear of a legacy agent."""
        unverified: list[str] = []
        if self.own_legacy.state == STATE_UNKNOWN:
            unverified.append(f"gui/{os.getuid()}: {self.own_legacy.detail}")
        if not self.enumeration_ok:
            unverified.append(f"перечисление учётных записей: {self.enumeration_detail}")
        for uid, probe in sorted(self.other_legacy.items()):
            if probe.state == STATE_UNKNOWN:
                unverified.append(f"gui/{uid}: {probe.detail}")
        return tuple(unverified)


def _probe_domain(domain: str, label: str, *, timeout: float = 5.0) -> DomainProbe:
    """Read launchd state for `label` in `domain` without mutating it."""
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return DomainProbe(STATE_UNKNOWN, False, "launchd недоступен")
    try:
        result = subprocess.run(
            [launchctl, "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DomainProbe(STATE_UNKNOWN, False, "статус недоступен: таймаут")
    except OSError as exc:
        return DomainProbe(STATE_UNKNOWN, False, f"статус недоступен: {exc}")
    if result.returncode == 0:
        running = "\n\tstate = running\n" in result.stdout
        return DomainProbe(
            STATE_INSTALLED, running, "работает" if running else "установлен, но не запущен"
        )
    combined = f"{result.stderr}\n{result.stdout}".lower()
    if any(marker in combined for marker in _NOT_FOUND_MARKERS):
        return DomainProbe(STATE_ABSENT, False, "не установлен")
    if any(marker in combined for marker in _PERMISSION_MARKERS):
        return DomainProbe(STATE_UNKNOWN, False, "нет прав для проверки этой области")
    detail = (result.stderr or result.stdout).strip() or f"launchctl exit={result.returncode}"
    return DomainProbe(STATE_UNKNOWN, False, f"статус не определён: {detail[:200]}")


def _other_account_uids(*, timeout: float = 5.0) -> tuple[tuple[int, ...], bool, str]:
    """List local account UIDs other than the caller's, best-effort.

    Returns `(uids, enumeration_ok, detail)`. `enumeration_ok` is False when
    the directory service could not be queried at all -- callers must not
    read that as "no other accounts exist", only as "unknown".
    """
    dscl = shutil.which("dscl")
    if dscl is None:
        return (), False, "dscl недоступен"
    try:
        result = subprocess.run(
            [dscl, ".", "-list", "/Users", "UniqueID"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (), False, "перечисление учётных записей превысило таймаут"
    except OSError as exc:
        return (), False, f"перечисление учётных записей не удалось: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit={result.returncode}"
        return (), False, f"перечисление учётных записей не удалось: {detail}"
    own_uid = os.getuid()
    uids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            uid = int(parts[1])
        except ValueError:
            continue
        if uid >= _MIN_ACCOUNT_UID and uid != own_uid:
            uids.add(uid)
    return tuple(sorted(uids)), True, ""


def daemon_status(label: str = LAUNCH_AGENT_LABEL) -> DaemonStatus:
    """Probe the system daemon and every reachable legacy GUI-domain agent.

    Both domains are always probed -- an installed, or even running, system
    daemon does not make it safe to stop looking: the legacy per-user agent
    may still be loaded and dispatching alongside it.
    """
    system = _probe_domain("system", label)
    own_legacy = _probe_domain(f"gui/{os.getuid()}", label)
    other_uids, enumeration_ok, enumeration_detail = _other_account_uids()
    other_legacy = {uid: _probe_domain(f"gui/{uid}", label) for uid in other_uids}
    return DaemonStatus(
        system=system,
        own_legacy=own_legacy,
        other_legacy=other_legacy,
        enumeration_ok=enumeration_ok,
        enumeration_detail=enumeration_detail,
    )


def launch_agent_status(label: str = LAUNCH_AGENT_LABEL) -> tuple[bool, str]:
    """Read system-daemon launchd state without changing or restarting it."""
    probe = _probe_domain("system", label)
    if probe.state == STATE_INSTALLED:
        return probe.running, probe.detail
    return False, probe.detail


def _local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return parse_time(value).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return value


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
    daemon = daemon_status()
    agent_running, agent_label = daemon.system.running, daemon.system.detail

    st.subheader("Ежедневный аудит")
    st.caption(
        "Постоянный продуктовый и инженерный контроль: пользовательский путь, UI/UX, "
        "очереди и зависимости, remediation, полный набор тестов, Review, CI и merge."
    )

    active = status.get("active_campaign")
    state_label = "Выполняется" if active else "Ожидает расписания"
    cols = st.columns(4)
    cols[0].metric("Сервис (system)", agent_label)
    cols[1].metric("Кампания", state_label)
    cols[2].metric("Следующий запуск", _local_time(status.get("next_run_at")))
    cols[3].metric("Последний результат", campaigns[0]["status"] if campaigns else "—")

    if not agent_running:
        st.error(
            "Системный launchd-демон не работает. Ручной запрос сохранится, "
            "но не будет обработан до запуска сервиса."
        )

    if daemon.legacy_running_uids:
        st.error(
            "Обнаружен устаревший GUI-агент launchd, работающий одновременно с "
            "системным демоном для UID "
            f"{', '.join(str(uid) for uid in daemon.legacy_running_uids)}. Это может "
            "привести к дублирующему запуску кампаний. Выгрузите старый агент: "
            f"`launchctl bootout gui/<uid>/{LAUNCH_AGENT_LABEL}`, затем удалите его "
            "plist из `~/Library/LaunchAgents/`. См. раздел «Миграция» в "
            "docs/DAILY_SELF_AUDIT.md."
        )
    elif daemon.legacy_installed_uids:
        st.warning(
            "Обнаружен устаревший GUI-агент launchd (загружен, но не запущен) для UID "
            f"{', '.join(str(uid) for uid in daemon.legacy_installed_uids)}. Удалите его, "
            "чтобы не оставлять два независимых определения сервиса: "
            f"`launchctl bootout gui/<uid>/{LAUNCH_AGENT_LABEL}`. См. раздел «Миграция» "
            "в docs/DAILY_SELF_AUDIT.md."
        )

    if daemon.legacy_unverified:
        st.warning(
            "Наличие устаревшего GUI-агента не удалось проверить для следующих "
            "областей (обычно из-за прав доступа к чужим сессиям launchd) -- это "
            "не означает, что они свободны от устаревшего агента: "
            + "; ".join(daemon.legacy_unverified)
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
