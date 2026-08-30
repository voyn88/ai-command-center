import os
import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import daily_audit_panel

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

LABEL = daily_audit_panel.LAUNCH_AGENT_LABEL


def _fake_which(available):
    return lambda name: available.get(name)


def _fake_run(mapping):
    def _run(cmd, **kwargs):
        key = tuple(cmd)
        assert key in mapping, f"unexpected launchctl invocation: {cmd}"
        returncode, stdout = mapping[key]
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return _run


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


def test_launch_agent_status_reports_legacy_agent_even_when_system_running(monkeypatch):
    """A healthy system daemon must not short-circuit the legacy-domain probe."""
    uid = os.getuid()
    monkeypatch.setattr(
        daily_audit_panel.shutil, "which", _fake_which({"launchctl": "/usr/bin/launchctl"})
    )
    mapping = {
        ("/usr/bin/launchctl", "print", f"system/{LABEL}"): (
            0,
            "foo = {\n\tstate = running\n}\n",
        ),
        ("/usr/bin/launchctl", "print", f"gui/{uid}/{LABEL}"): (
            0,
            "foo = {\n\tstate = not running\n}\n",
        ),
    }
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", _fake_run(mapping))

    running, message = daily_audit_panel.launch_agent_status()

    assert running is True
    assert "устаревший" in message
    assert str(uid) in message


def test_launch_agent_status_probes_every_real_account_for_legacy_agent(monkeypatch):
    """A legacy agent loaded under another logged-in account must not be invisible."""
    uid = os.getuid()
    monkeypatch.setattr(
        daily_audit_panel.shutil,
        "which",
        _fake_which({"launchctl": "/usr/bin/launchctl", "dscl": "/usr/bin/dscl"}),
    )
    mapping = {
        ("/usr/bin/launchctl", "print", f"system/{LABEL}"): (1, ""),
        ("/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"): (
            0,
            "alice 501\nbob 502\ndaemonuser 92\n",
        ),
        ("/usr/bin/launchctl", "print", f"gui/{uid}/{LABEL}"): (1, ""),
        ("/usr/bin/launchctl", "print", f"gui/501/{LABEL}"): (1, ""),
        ("/usr/bin/launchctl", "print", f"gui/502/{LABEL}"): (0, "foo = {\n\tstate = running\n}\n"),
    }
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", _fake_run(mapping))

    running, message = daily_audit_panel.launch_agent_status()

    assert running is True
    assert "502" in message
    assert "не мигрирован" in message


def test_launch_agent_status_clean_system_install_has_no_legacy_note(monkeypatch):
    uid = os.getuid()
    monkeypatch.setattr(
        daily_audit_panel.shutil, "which", _fake_which({"launchctl": "/usr/bin/launchctl"})
    )
    mapping = {
        ("/usr/bin/launchctl", "print", f"system/{LABEL}"): (0, "foo = {\n\tstate = running\n}\n"),
        ("/usr/bin/launchctl", "print", f"gui/{uid}/{LABEL}"): (1, ""),
    }
    monkeypatch.setattr(daily_audit_panel.subprocess, "run", _fake_run(mapping))

    assert daily_audit_panel.launch_agent_status() == (True, "работает")
