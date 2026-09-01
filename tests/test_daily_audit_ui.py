import re
import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import daily_audit_panel

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "DAILY_SELF_AUDIT.md"


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
    assert daily_audit_panel.launch_agent_status() == (False, "launchd недоступен")


def _fake_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    return run


def test_probe_domain_reports_installed_and_running(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(0, stdout="state = running\n\tstate = running\n"),
    )
    probe = daily_audit_panel._probe_domain("system", "com.example.svc")
    assert probe.state == daily_audit_panel.STATE_INSTALLED
    assert probe.running is True


def test_probe_domain_recognizes_not_found_as_absent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(3, stderr="Could not find service \"x\" in domain for system"),
    )
    probe = daily_audit_panel._probe_domain("system", "com.example.svc")
    assert probe.state == daily_audit_panel.STATE_ABSENT


def test_probe_domain_treats_permission_denied_as_unknown_not_absent(monkeypatch):
    """A cross-account probe that is denied must not be read as 'not installed'."""
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(1, stderr="Operation not permitted"),
    )
    probe = daily_audit_panel._probe_domain("gui/501", "com.example.svc")
    assert probe.state == daily_audit_panel.STATE_UNKNOWN
    assert probe.state != daily_audit_panel.STATE_ABSENT


def test_probe_domain_treats_unexpected_failure_as_unknown_not_absent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(70, stderr="launchctl: something unexpected exploded"),
    )
    probe = daily_audit_panel._probe_domain("system", "com.example.svc")
    assert probe.state == daily_audit_panel.STATE_UNKNOWN


def test_probe_domain_timeout_is_unknown_not_absent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="launchctl", timeout=5)

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", raise_timeout)
    probe = daily_audit_panel._probe_domain("system", "com.example.svc")
    assert probe.state == daily_audit_panel.STATE_UNKNOWN


def test_other_account_uids_enumeration_failure_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/dscl")
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(1, stderr="eDSPermissionError"),
    )
    uids, ok, detail = daily_audit_panel._other_account_uids()
    assert uids == ()
    assert ok is False
    assert detail


def test_other_account_uids_missing_dscl_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: None)
    uids, ok, detail = daily_audit_panel._other_account_uids()
    assert uids == ()
    assert ok is False
    assert detail


def test_other_account_uids_parses_local_users(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/dscl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(0, stdout="_spotlight 89\nroot 0\nalice 501\nbob 502\n"),
    )
    uids, ok, detail = daily_audit_panel._other_account_uids()
    assert uids == (502,)
    assert ok is True
    assert detail == ""


def test_daemon_status_reports_legacy_coexistence_even_when_system_is_installed(monkeypatch):
    """A successful system-domain lookup must not short-circuit the legacy probe."""
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda name: "/bin/launchctl" if name == "launchctl" else None)
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)

    calls = []

    def run(cmd, **_kwargs):
        target = cmd[2]
        calls.append(target)
        if target.startswith("system/"):
            return subprocess.CompletedProcess(cmd, 0, stdout="\n\tstate = running\n", stderr="")
        if target.startswith("gui/501/"):
            return subprocess.CompletedProcess(cmd, 0, stdout="\n\tstate = running\n", stderr="")
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="Could not find service")

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    status = daily_audit_panel.daemon_status()

    assert len(calls) >= 2
    assert status.system.state == daily_audit_panel.STATE_INSTALLED
    assert status.system.running is True
    assert status.own_legacy.state == daily_audit_panel.STATE_INSTALLED
    assert status.own_legacy.running is True
    assert 501 in status.legacy_running_uids


def test_daemon_status_legacy_unverified_includes_enumeration_failure(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda name: "/bin/launchctl" if name == "launchctl" else None)
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        daily_audit_panel.subprocess,
        "run",
        _fake_run(3, stderr="Could not find service"),
    )

    status = daily_audit_panel.daemon_status()

    assert status.enumeration_ok is False
    assert any("перечисление" in entry for entry in status.legacy_unverified)


def test_daemon_status_legacy_unverified_includes_permission_denied_other_account(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        daily_audit_panel,
        "_other_account_uids",
        lambda **_kwargs: ((777,), True, ""),
    )

    def run(cmd, **_kwargs):
        target = cmd[2]
        if target == "gui/777/com.ai-command-center.daily-audit":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Operation not permitted")
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="Could not find service")

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    status = daily_audit_panel.daemon_status()

    assert 777 not in status.legacy_running_uids
    assert 777 not in status.legacy_installed_uids
    assert any("gui/777" in entry for entry in status.legacy_unverified)


def test_docs_bootout_command_uses_the_service_target_form():
    """The runnable `bootout` command must join domain and label with '/'; a
    space removes a plist by name instead of a loaded service, and leaves the
    legacy agent running. (The wrong form is still discussed in prose, as a
    warning, so this only checks the fenced shell command.)"""
    text = DOCS_PATH.read_text(encoding="utf-8")
    command_blocks = re.findall(r"```text\n(.*?)\n```", text, re.DOTALL)
    bootout_commands = [
        line
        for block in command_blocks
        for line in block.splitlines()
        if line.startswith("launchctl bootout ")
    ]
    assert bootout_commands, "expected a runnable `launchctl bootout` command in the docs"
    assert all(
        line == "launchctl bootout gui/$(id -u)/com.ai-command-center.daily-audit"
        for line in bootout_commands
    )
