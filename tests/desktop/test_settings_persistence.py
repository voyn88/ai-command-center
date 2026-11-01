"""Window-geometry and theme persistence round-trips through QSettings.

Covers `DESKTOP_INCREMENT_1.md` §2's "window geometry persists across a restart"
criterion and `ARCHITECTURE.md` §14.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from command_center.desktop.app import build_shell
from command_center.desktop.settings import SettingsStore
from command_center.platform import DensityMode
from command_center.desktop.theme import ThemeMode


def _reopen_store(settings_file) -> SettingsStore:
    # A fresh store over the same backing file == a fresh application launch.
    return SettingsStore(QSettings(str(settings_file), QSettings.IniFormat))


def test_geometry_blob_round_trips_through_the_store(settings_store, settings_file):
    # The persistence wiring the shell owns: a saved geometry blob survives a
    # relaunch (a fresh store over the same backing file) unchanged.
    assert settings_store.geometry() is None
    from PySide6.QtCore import QByteArray

    blob = QByteArray(b"geometry-blob-bytes")
    settings_store.set_geometry(blob)
    settings_store.sync()
    assert _reopen_store(settings_file).geometry() == blob


def test_shell_persists_geometry_on_shutdown_and_restores_it(
    qtbot, qapp, settings_store, settings_file
):
    first, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(first)
    first.show()
    qapp.processEvents()
    first.resize(1000, 720)  # a distinctive, non-default size within min bounds
    qapp.processEvents()
    saved_height = first.size().height()
    first.shutdown()  # persists geometry + syncs

    # A non-empty geometry blob was written by shutdown.
    assert _reopen_store(settings_file).geometry() is not None

    # A fresh shell over the same file == relaunch: it restores the saved
    # geometry rather than falling back to the built-in default size. (Height
    # round-trips faithfully under the offscreen QPA; exact width fidelity is a
    # real-display concern verified at the D1 manual cross-platform gate.)
    second, _ = build_shell(qapp, _reopen_store(settings_file))
    qtbot.addWidget(second)
    second.show()
    qapp.processEvents()
    assert second.size().height() == saved_height

    from command_center.desktop.main_window import DEFAULT_HEIGHT

    assert second.size().height() != DEFAULT_HEIGHT


def test_theme_persists_across_restart(qtbot, qapp, settings_store, settings_file):
    first, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(first)
    first.navigate_to("settings")
    first._settings_page.buttons()[ThemeMode.DARK].click()
    first.shutdown()

    reopened = _reopen_store(settings_file)
    assert reopened.theme_mode() is ThemeMode.DARK

    second, theme = build_shell(qapp, reopened)
    qtbot.addWidget(second)
    assert theme.mode is ThemeMode.DARK
    assert theme.palette.name == "dark"


def test_selected_project_preference_roundtrips(settings_store, settings_file):
    assert settings_store.selected_project() is None
    settings_store.set_selected_project("BANK")
    settings_store.sync()
    assert _reopen_store(settings_file).selected_project() == "BANK"
    settings_store.set_selected_project(None)
    settings_store.sync()
    assert _reopen_store(settings_file).selected_project() is None


def test_density_preference_roundtrips(settings_store, settings_file):
    assert settings_store.density_mode() is DensityMode.COMFORTABLE
    settings_store.set_density_mode(DensityMode.COMPACT)
    settings_store.sync()
    assert _reopen_store(settings_file).density_mode() is DensityMode.COMPACT


def test_data_source_mode_defaults_local_and_roundtrips(settings_store, settings_file):
    from command_center.platform import DataSourceMode

    assert settings_store.data_source_mode() is DataSourceMode.LOCAL
    settings_store.set_data_source_mode(DataSourceMode.SERVER)
    settings_store.sync()
    assert _reopen_store(settings_file).data_source_mode() is DataSourceMode.SERVER


def test_data_source_mode_toggle_persists_across_restart(qtbot, qapp, settings_store, settings_file):
    from command_center.platform import DataSourceMode

    first, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(first)
    first.navigate_to("settings")
    first._settings_page.data_source_buttons()[DataSourceMode.SERVER].click()
    first.shutdown()

    reopened = _reopen_store(settings_file)
    assert reopened.data_source_mode() is DataSourceMode.SERVER

    second, _ = build_shell(qapp, reopened)
    qtbot.addWidget(second)
    assert second._settings.data_source_mode() is DataSourceMode.SERVER


def test_reset_window_geometry_removes_geometry_and_state(
    settings_store, settings_file
):
    from PySide6.QtCore import QByteArray

    settings_store.set_geometry(QByteArray(b"geometry"))
    settings_store.set_window_state(QByteArray(b"state"))
    settings_store.reset_window_geometry()
    settings_store.sync()

    reopened = _reopen_store(settings_file)
    assert reopened.geometry() is None
    assert reopened.window_state() is None


def test_settings_form_persists_density_and_workspace_across_restart(
    qtbot, qapp, settings_store, settings_file
):
    first, theme = build_shell(qapp, settings_store)
    qtbot.addWidget(first)
    first.navigate_to("settings")
    page = first._settings_page

    page.density_buttons()[DensityMode.COMPACT].click()
    assert first.sidebar.items()["home"].height() == 32
    page.form.selected_project_edit.setText("  AICC  ")
    page.form.save_workspace_button.click()
    first.shutdown()

    reopened = _reopen_store(settings_file)
    assert reopened.density_mode() is DensityMode.COMPACT
    assert reopened.selected_project() == "AICC"
    assert theme.density is DensityMode.COMPACT

    second, second_theme = build_shell(qapp, reopened)
    qtbot.addWidget(second)
    assert second_theme.density is DensityMode.COMPACT
    assert second._settings_page.form.selected_project_edit.text() == "AICC"
    assert second._settings_page.density_buttons()[DensityMode.COMPACT].isChecked()


def test_settings_form_reset_restores_default_window_size(
    qtbot, qapp, settings_store
):
    shell, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(shell)
    shell.resize(1000, 700)
    settings_store.set_geometry(shell.saveGeometry())
    shell.navigate_to("settings")

    shell._settings_page.form.reset_geometry_button.click()

    from command_center.desktop.main_window import DEFAULT_HEIGHT, DEFAULT_WIDTH

    assert shell.width() == DEFAULT_WIDTH
    assert shell.height() == DEFAULT_HEIGHT
    assert settings_store.geometry() is None


def test_settings_fields_have_accessible_labels(shell):
    shell.navigate_to("settings")
    form = shell._settings_page.form
    assert form.selected_project_edit.accessibleName()
    assert form.selected_project_edit.accessibleDescription()
    assert form.reset_geometry_button.accessibleName()
    assert form.reset_geometry_button.accessibleDescription()
