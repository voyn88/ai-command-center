import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import daily_audit_panel

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
LABEL = daily_audit_panel.LAUNCH_AGENT_LABEL


def _fake_run(responses):
    calls = []

    def run(cmd, **kwargs):
        target = cmd[2]
        calls.append(target)
        stdout, returncode = responses.get(target, ("", 1))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return run, calls


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


def test_launch_agent_status_always_probes_both_domains_when_system_is_running(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    run, calls = _fake_run(
        {
            f"system/{LABEL}": ("\n\tstate = running\n", 0),
            f"gui/501/{LABEL}": ("", 1),
        }
    )
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    result = daily_audit_panel.launch_agent_status()

    assert calls == [f"system/{LABEL}", f"gui/501/{LABEL}"]
    assert result == (True, "работает")


def test_launch_agent_status_reports_coexistence_of_daemon_and_legacy_agent(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    run, calls = _fake_run(
        {
            f"system/{LABEL}": ("\n\tstate = running\n", 0),
            f"gui/501/{LABEL}": ("\n\tstate = running\n", 0),
        }
    )
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    running, message = daily_audit_panel.launch_agent_status()

    assert calls == [f"system/{LABEL}", f"gui/501/{LABEL}"]
    assert running is True
    assert "миграц" in message


def test_launch_agent_status_flags_legacy_agent_when_daemon_is_stopped(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    run, calls = _fake_run(
        {
            f"system/{LABEL}": ("\n\tstate = not running\n", 0),
            f"gui/501/{LABEL}": ("\n\tstate = running\n", 0),
        }
    )
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    running, message = daily_audit_panel.launch_agent_status()

    assert calls == [f"system/{LABEL}", f"gui/501/{LABEL}"]
    assert running is False
    assert "миграц" in message


def test_launch_agent_status_not_installed_anywhere(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.os, "getuid", lambda: 501)
    run, calls = _fake_run({})
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", run)

    result = daily_audit_panel.launch_agent_status()

    assert calls == [f"system/{LABEL}", f"gui/501/{LABEL}"]
    assert result == (False, "не установлен")
