import re
import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import daily_audit_panel

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "DAILY_SELF_AUDIT.md"
LABEL = daily_audit_panel.LAUNCH_AGENT_LABEL


def test_daily_audit_deep_link_renders_but_is_not_duplicated_in_navigation():
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "daily_audit"
    at.run()

    assert not at.exception
    assert at.subheader[0].value == "Ежедневный аудит"
    assert not any(button.key == "nav_btn_daily_audit" for button in at.sidebar.button)
    assert any("Запустить аудит сейчас" in button.label for button in at.button)


def test_launch_agent_status_is_portable_when_launchctl_is_absent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: None)
    status = daily_audit_panel.launch_agent_status()
    assert status.summary() == (False, "launchd недоступен")


def _stub_launchctl_and_dscl(monkeypatch, dscl_stdout, launchctl_responses):
    """launchctl_responses maps a print target (e.g. "system/<label>") to a
    subprocess.CompletedProcess. dscl always lists no other accounts unless
    dscl_stdout is given."""

    monkeypatch.setattr(
        daily_audit_panel.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("launchctl", "dscl") else None,
    )

    def fake_run(cmd, **kwargs):
        binary = cmd[0]
        if binary.endswith("dscl"):
            return subprocess.CompletedProcess(cmd, 0, dscl_stdout, "")
        assert binary.endswith("launchctl") and cmd[1] == "print"
        target = cmd[2]
        return launchctl_responses[target]

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", fake_run)


def test_launch_agent_status_always_probes_legacy_domain_even_when_daemon_runs(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    _stub_launchctl_and_dscl(
        monkeypatch,
        dscl_stdout="alice 501\n",
        launchctl_responses={
            f"system/{LABEL}": subprocess.CompletedProcess(
                [], 0, "\n\tstate = running\n", ""
            ),
            f"gui/501/{LABEL}": subprocess.CompletedProcess(
                [], 0, "\n\tstate = running\n", ""
            ),
        },
    )
    status = daily_audit_panel.launch_agent_status()
    assert status.system_running
    assert status.legacy_active
    running, label = status.summary()
    assert running is True
    assert "устаревш" in label.lower()


def test_launch_agent_status_reports_legacy_when_daemon_stopped(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    not_found = subprocess.CompletedProcess(
        [], 3, "", 'Could not find service "x" in domain'
    )
    _stub_launchctl_and_dscl(
        monkeypatch,
        dscl_stdout="alice 501\n",
        launchctl_responses={
            f"system/{LABEL}": not_found,
            f"gui/501/{LABEL}": subprocess.CompletedProcess(
                [], 0, "\n\tstate = running\n", ""
            ),
        },
    )
    status = daily_audit_panel.launch_agent_status()
    assert not status.system_running
    assert status.legacy_active
    running, label = status.summary()
    assert running is False
    assert "миграц" in label.lower()


def test_launch_agent_status_reports_other_accounts_not_just_current_uid(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    not_found = subprocess.CompletedProcess(
        [], 3, "", 'Could not find service "x" in domain'
    )
    _stub_launchctl_and_dscl(
        monkeypatch,
        dscl_stdout="alice 501\nbob 502\n",
        launchctl_responses={
            f"system/{LABEL}": not_found,
            f"gui/501/{LABEL}": not_found,
            f"gui/502/{LABEL}": subprocess.CompletedProcess(
                [], 0, "\n\tstate = running\n", ""
            ),
        },
    )
    status = daily_audit_panel.launch_agent_status()
    assert status.other_legacy_states == {502: "running"}
    assert status.legacy_active
    assert not status.legacy_unverified


def test_launch_agent_status_treats_permission_denied_as_unverified_not_absent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    not_found = subprocess.CompletedProcess(
        [], 3, "", 'Could not find service "x" in domain'
    )
    permission_denied = subprocess.CompletedProcess(
        [], 3, "", "launchctl print: Permission denied"
    )
    _stub_launchctl_and_dscl(
        monkeypatch,
        dscl_stdout="alice 501\nbob 502\n",
        launchctl_responses={
            f"system/{LABEL}": not_found,
            f"gui/501/{LABEL}": not_found,
            f"gui/502/{LABEL}": permission_denied,
        },
    )
    status = daily_audit_panel.launch_agent_status()
    assert status.other_legacy_states == {502: "unknown"}
    assert status.legacy_unverified
    assert not status.legacy_active
    running, label = status.summary()
    assert running is False
    assert label == "не установлен"


def test_launch_agent_status_reports_clean_install_with_no_legacy(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    not_found = subprocess.CompletedProcess(
        [], 3, "", 'Could not find service "x" in domain'
    )
    _stub_launchctl_and_dscl(
        monkeypatch,
        dscl_stdout="alice 501\n",
        launchctl_responses={
            f"system/{LABEL}": subprocess.CompletedProcess(
                [], 0, "\n\tstate = running\n", ""
            ),
            f"gui/501/{LABEL}": not_found,
        },
    )
    status = daily_audit_panel.launch_agent_status()
    assert status.summary() == (True, "работает")
    assert not status.legacy_active
    assert not status.legacy_unverified


def test_migration_docs_use_the_correct_bootout_service_target_syntax():
    text = DOCS_PATH.read_text(encoding="utf-8")
    assert f"launchctl bootout gui/$(id -u)/{LABEL}" in text
    # The buggy form treats the label as a *second* bootout argument instead
    # of part of the gui/<uid>/<label> service-target path.
    assert re.search(rf"bootout gui/\$\(id -u\)\s+{re.escape(LABEL)}", text) is None
