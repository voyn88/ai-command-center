import os
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import daily_audit_panel

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


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


def _fake_result(returncode, stdout=""):
    class Result:
        pass

    result = Result()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_launch_agent_status_queries_system_domain_not_gui_domain(monkeypatch):
    """The daemon must run without a logged-in GUI session (headless host),
    so status must be read from the `system` domain, never `gui/<uid>`."""
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/launchctl")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_result(0, "\n\tstate = running\n")

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", fake_run)
    running, label = daily_audit_panel.launch_agent_status()
    assert running is True
    assert label == "работает"
    assert len(calls) == 1
    assert calls[0][-1] == f"system/{daily_audit_panel.LAUNCH_AGENT_LABEL}"
    assert not any(arg.startswith("gui/") for arg in calls[0])


def test_launch_agent_status_falls_back_to_legacy_gui_domain_when_running(monkeypatch):
    """A pre-migration host still has a `gui/<uid>` LaunchAgent instead of the
    `system` LaunchDaemon. It must read as "legacy agent active", not "not
    installed", so an in-place upgrade doesn't hide that the old copy is
    still running."""
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/launchctl")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-1].startswith("system/"):
            return _fake_result(1)
        return _fake_result(0, "\n\tstate = running\n")

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", fake_run)
    running, label = daily_audit_panel.launch_agent_status()
    assert running is True
    assert "требуется миграция" in label
    assert len(calls) == 2
    assert calls[0][-1] == f"system/{daily_audit_panel.LAUNCH_AGENT_LABEL}"
    assert calls[1][-1] == f"gui/{os.getuid()}/{daily_audit_panel.LAUNCH_AGENT_LABEL}"


def test_launch_agent_status_falls_back_to_legacy_gui_domain_when_stopped(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/launchctl")

    def fake_run(cmd, **kwargs):
        if cmd[-1].startswith("system/"):
            return _fake_result(1)
        return _fake_result(0, "\n\tstate = not running\n")

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", fake_run)
    running, label = daily_audit_panel.launch_agent_status()
    assert running is False
    assert "требуется миграция" in label


def test_launch_agent_status_reports_not_installed_when_neither_domain_has_it(monkeypatch):
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/launchctl")
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", lambda cmd, **kwargs: _fake_result(1))
    assert daily_audit_panel.launch_agent_status() == (False, "не установлен")


def test_daily_audit_page_warns_about_legacy_agent_instead_of_generic_error(monkeypatch):
    monkeypatch.setattr(
        daily_audit_panel,
        "launch_agent_status",
        lambda: (True, "работает через устаревший gui-агент, требуется миграция"),
    )
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "daily_audit"
    at.run()

    assert not at.exception
    assert any("миграци" in warning.value for warning in at.warning)
    assert not any("не работает" in error.value for error in at.error)
