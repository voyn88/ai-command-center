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


def test_launch_agent_status_queries_system_domain_not_gui_domain(monkeypatch):
    """The daemon must run without a logged-in GUI session (headless host),
    so status must be read from the `system` domain, never `gui/<uid>`."""
    monkeypatch.setattr(daily_audit_panel.shutil, "which", lambda _: "/usr/bin/launchctl")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            returncode = 0
            stdout = "\n\tstate = running\n"

        return Result()

    monkeypatch.setattr(daily_audit_panel.subprocess, "run", fake_run)
    running, label = daily_audit_panel.launch_agent_status()
    assert running is True
    assert label == "работает"
    assert captured["cmd"][-1] == f"system/{daily_audit_panel.LAUNCH_AGENT_LABEL}"
    assert not any(arg.startswith("gui/") for arg in captured["cmd"])
