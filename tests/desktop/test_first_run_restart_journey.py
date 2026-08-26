"""First-run and restart journeys for the native shell (issue #196 AC).

First run: a fresh profile boots to Home with documented defaults and every
registered section activates without error, including the real-data Workspace
Home load through the production adapter wiring (`app.run`'s exact composition).
Restart: a second launch over the same backing file restores theme, density,
workspace preference and window geometry, and every section still activates.

The real-data load must end in a *handled* terminal state — populated content,
the explicit empty state, or the explicit Russian error label — never a hang or
an unhandled exception (`WORKSPACE_HOME_SPEC.md` §15).
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from command_center.desktop import i18n
from command_center.desktop.app import build_shell
from command_center.desktop.settings import SettingsStore
from command_center.desktop.theme import ThemeMode
from command_center.platform import DensityMode

LOAD_TIMEOUT_MS = 30_000


def _relaunch_store(settings_file) -> SettingsStore:
    return SettingsStore(QSettings(str(settings_file), QSettings.IniFormat))


def _walk_every_section(shell, qapp) -> None:
    for key in list(shell._pages):
        shell.navigate_to(key)
        qapp.processEvents()
        assert shell.current_section_key == key


def _home_reached_handled_terminal_state(home) -> bool:
    """Populated content, explicit empty state, or the explicit error label."""
    if home.is_loading():
        return False
    if home.project_cards() or home.metric_cards():
        return True  # populated real data
    from PySide6.QtWidgets import QLabel

    from command_center.desktop.components.empty_state import EmptyState
    from command_center.desktop.components.error_state import ErrorState

    if home.findChildren(EmptyState) or home.findChildren(ErrorState):
        return True
    return i18n.HOME_LOAD_ERROR in [w.text() for w in home.findChildren(QLabel)]


def test_first_run_journey_defaults_all_sections_and_real_data_load(
    qtbot, qapp, settings_store, settings_file
):
    # First run == pristine backing file: documented defaults, no stale state.
    assert settings_store.geometry() is None
    assert settings_store.selected_project() is None
    assert settings_store.density_mode() is DensityMode.COMFORTABLE

    shell, _theme = build_shell(qapp, settings_store)
    qtbot.addWidget(shell)
    assert shell.current_section_key == "home"

    _walk_every_section(shell, qapp)

    # Production adapter wiring, exactly as `app.run` composes it (D2 real data).
    from command_center.application.operations_adapter import OperationsAdapter
    from command_center.application.workspace_home_adapter import WorkspaceHomeAdapter

    home_adapter = WorkspaceHomeAdapter()
    shell.navigate_to("home")
    shell.load_workspace_home(
        home_adapter, OperationsAdapter(workspace_home_adapter=home_adapter)
    )
    home = shell._home
    qtbot.waitUntil(lambda: not home.is_loading(), timeout=LOAD_TIMEOUT_MS)
    assert _home_reached_handled_terminal_state(home)

    assert shell.shutdown()


def test_restart_journey_restores_preferences_and_sections(
    qtbot, qapp, settings_store, settings_file
):
    first, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(first)
    first.show()
    qapp.processEvents()
    first.resize(1000, 720)
    qapp.processEvents()
    saved_height = first.size().height()

    first.navigate_to("settings")
    page = first._settings_page
    page.buttons()[ThemeMode.DARK].click()
    page.density_buttons()[DensityMode.COMPACT].click()
    page.form.selected_project_edit.setText("AICC")
    page.form.save_workspace_button.click()
    assert first.shutdown()

    second, theme = build_shell(qapp, _relaunch_store(settings_file))
    qtbot.addWidget(second)
    second.show()
    qapp.processEvents()

    assert theme.mode is ThemeMode.DARK
    assert theme.density is DensityMode.COMPACT
    assert second._settings_page.form.selected_project_edit.text() == "AICC"
    assert second.size().height() == saved_height
    assert second.current_section_key == "home"

    _walk_every_section(second, qapp)
    assert second.shutdown()


def test_home_load_error_journey_shows_russian_error_state(
    qtbot, qapp, shell
):
    # A first run whose adapter fails must land on the explicit Russian error
    # label — the journey's guaranteed worst-case terminal state.
    class ExplodingAdapter:
        def snapshot(self):  # the one port `HomePage.load` requires
            raise RuntimeError("boom")

    shell.navigate_to("home")
    shell.load_workspace_home(ExplodingAdapter())
    home = shell._home
    qtbot.waitUntil(lambda: not home.is_loading(), timeout=LOAD_TIMEOUT_MS)

    from PySide6.QtWidgets import QLabel

    texts = [label.text() for label in home.findChildren(QLabel)]
    assert i18n.HOME_LOAD_ERROR in texts


def test_first_run_wizard_journey_configures_workspace_then_lands_home(
    qtbot, qapp, settings_store, tmp_path, monkeypatch
):
    """D-1 wizard path: unconfigured machine → wizard → configured → Home.

    POSIX-gated like the other filesystem-sensitive journeys.
    """
    import os

    if os.name != "posix":
        import pytest

        pytest.skip("first-run wizard journey is POSIX-gated (D-1)")

    from command_center.application.first_run import (
        HealthCheckItem,
        HealthStatus,
        initialize_workspace,
    )
    from command_center.desktop.pages.first_run_wizard import FirstRunWizard

    # Unconfigured: no persisted root, no wizard has run yet.
    assert settings_store.workspace_root() is None

    env: dict[str, str] = {}
    wizard = FirstRunWizard(
        settings_store,
        health_checks=lambda: (
            HealthCheckItem("git", "Git", HealthStatus.OK, "/usr/bin/git"),
        ),
        initialize=lambda raw: initialize_workspace(raw, environ=env),
    )
    qtbot.addWidget(wizard)
    wizard.workspace_edit.setText(str(tmp_path / "workspace"))
    wizard.continue_button.click()
    assert wizard.result() == int(wizard.DialogCode.Accepted)

    # The wizard wrote the config the app expects: persisted root + layout.
    root = settings_store.workspace_root()
    assert root is not None
    for child in ("data", "generated", "reports"):
        assert (tmp_path / "workspace" / child).is_dir()
    assert env["AICC_WORKSPACE_ROOT"] == root

    # Next launch over the same settings skips the wizard and lands on Home.
    from command_center.application.first_run import workspace_is_configured

    assert workspace_is_configured(env)
    shell, _theme = build_shell(qapp, settings_store)
    qtbot.addWidget(shell)
    assert shell.current_section_key == "home"
    _walk_every_section(shell, qapp)
    assert shell.shutdown()
