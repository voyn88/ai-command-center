"""D3 Settings form for platform-native desktop preferences."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from command_center.platform import DataSourceMode, DensityMode

from .. import i18n, tokens
from ..theme import ThemeMode
from .base_page import BasePage

_MODE_LABELS: tuple[tuple[ThemeMode, str], ...] = (
    (ThemeMode.LIGHT, i18n.THEME_LIGHT),
    (ThemeMode.DARK, i18n.THEME_DARK),
    (ThemeMode.SYSTEM, i18n.THEME_SYSTEM),
)
_DENSITY_LABELS: tuple[tuple[DensityMode, str], ...] = (
    (DensityMode.COMFORTABLE, i18n.DENSITY_COMFORTABLE),
    (DensityMode.COMPACT, i18n.DENSITY_COMPACT),
)
_DATA_SOURCE_LABELS: tuple[tuple[DataSourceMode, str], ...] = (
    (DataSourceMode.LOCAL, i18n.DATA_SOURCE_LOCAL),
    (DataSourceMode.SERVER, i18n.DATA_SOURCE_SERVER),
)


class SettingsForm(QWidget):
    """Accessible preference form; persistence remains owned by the shell."""

    theme_mode_changed = Signal(object)
    density_mode_changed = Signal(object)
    window_geometry_reset_requested = Signal()
    workspace_save_requested = Signal(object)
    data_source_mode_changed = Signal(object)

    def __init__(
        self,
        current_mode: ThemeMode,
        current_density: DensityMode,
        selected_project: str | None,
        current_data_source_mode: DataSourceMode = DataSourceMode.LOCAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsForm")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(tokens.SPACE_LG)

        appearance = QGroupBox(i18n.SETTINGS_APPEARANCE_GROUP)
        appearance.setAccessibleName(i18n.SETTINGS_APPEARANCE_ACCESSIBLE)
        appearance_box = QVBoxLayout(appearance)
        appearance_box.setSpacing(tokens.SPACE_SM)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_buttons: dict[ThemeMode, QRadioButton] = {}
        for mode, label in _MODE_LABELS:
            radio = QRadioButton(label)
            radio.setAccessibleName(i18n.theme_accessible_name(label))
            radio.setChecked(mode is current_mode)
            radio.toggled.connect(
                lambda checked, m=mode: checked and self.theme_mode_changed.emit(m)
            )
            self._theme_group.addButton(radio)
            self._theme_buttons[mode] = radio
            appearance_box.addWidget(radio)
        root.addWidget(appearance)

        density = QGroupBox(i18n.SETTINGS_DENSITY_GROUP)
        density.setAccessibleName(i18n.SETTINGS_DENSITY_ACCESSIBLE)
        density_box = QVBoxLayout(density)
        density_box.setSpacing(tokens.SPACE_SM)
        self._density_group = QButtonGroup(self)
        self._density_group.setExclusive(True)
        self._density_buttons: dict[DensityMode, QRadioButton] = {}
        for mode, label in _DENSITY_LABELS:
            radio = QRadioButton(label)
            radio.setAccessibleName(i18n.density_accessible_name(label))
            radio.setChecked(mode is current_density)
            radio.toggled.connect(
                lambda checked, m=mode: checked and self.density_mode_changed.emit(m)
            )
            self._density_group.addButton(radio)
            self._density_buttons[mode] = radio
            density_box.addWidget(radio)
        root.addWidget(density)

        data_source = QGroupBox(i18n.SETTINGS_DATA_SOURCE_GROUP)
        data_source.setAccessibleName(i18n.SETTINGS_DATA_SOURCE_ACCESSIBLE)
        data_source_box = QVBoxLayout(data_source)
        data_source_box.setSpacing(tokens.SPACE_SM)
        self._data_source_group = QButtonGroup(self)
        self._data_source_group.setExclusive(True)
        self._data_source_buttons: dict[DataSourceMode, QRadioButton] = {}
        for mode, label in _DATA_SOURCE_LABELS:
            radio = QRadioButton(label)
            radio.setObjectName(f"DataSourceMode{mode.value.title()}")
            radio.setAccessibleName(label)
            radio.setChecked(mode is current_data_source_mode)
            radio.toggled.connect(
                lambda checked, m=mode: checked and self.data_source_mode_changed.emit(m)
            )
            self._data_source_group.addButton(radio)
            self._data_source_buttons[mode] = radio
            data_source_box.addWidget(radio)
        data_source_description = QLabel(i18n.SETTINGS_DATA_SOURCE_DESCRIPTION)
        data_source_description.setObjectName("DataSourceDescription")
        data_source_description.setWordWrap(True)
        data_source_box.addWidget(data_source_description)
        root.addWidget(data_source)

        window = QGroupBox(i18n.SETTINGS_WINDOW_GROUP)
        window_box = QVBoxLayout(window)
        self.reset_geometry_button = QPushButton(i18n.SETTINGS_RESET_GEOMETRY)
        self.reset_geometry_button.setObjectName("ResetWindowGeometryButton")
        self.reset_geometry_button.setAccessibleName(i18n.SETTINGS_RESET_GEOMETRY)
        self.reset_geometry_button.setAccessibleDescription(
            i18n.SETTINGS_RESET_GEOMETRY_DESCRIPTION
        )
        self.reset_geometry_button.clicked.connect(
            self.window_geometry_reset_requested.emit
        )
        window_box.addWidget(self.reset_geometry_button)
        root.addWidget(window)

        workspace = QGroupBox(i18n.SETTINGS_WORKSPACE_GROUP)
        workspace_box = QFormLayout(workspace)
        self.selected_project_edit = QLineEdit(selected_project or "")
        self.selected_project_edit.setObjectName("SelectedProjectPreference")
        self.selected_project_edit.setPlaceholderText(
            i18n.SETTINGS_SELECTED_PROJECT_PLACEHOLDER
        )
        self.selected_project_edit.setAccessibleName(
            i18n.SETTINGS_SELECTED_PROJECT_LABEL
        )
        self.selected_project_edit.setAccessibleDescription(
            i18n.SETTINGS_SELECTED_PROJECT_DESCRIPTION
        )
        selected_label = QLabel(i18n.SETTINGS_SELECTED_PROJECT_LABEL)
        selected_label.setBuddy(self.selected_project_edit)
        workspace_box.addRow(selected_label, self.selected_project_edit)
        self.save_workspace_button = QPushButton(i18n.SETTINGS_SAVE_WORKSPACE)
        self.save_workspace_button.setObjectName("SaveWorkspacePreferencesButton")
        self.save_workspace_button.clicked.connect(self._save_workspace)
        workspace_box.addRow(self.save_workspace_button)
        self.saved_label = QLabel("")
        self.saved_label.setObjectName("SettingsSavedStatus")
        self.saved_label.setAccessibleName(i18n.SETTINGS_SAVED)
        workspace_box.addRow(self.saved_label)
        root.addWidget(workspace)
        root.addStretch(1)
        self.apply_density(current_density)

    def _save_workspace(self) -> None:
        value = self.selected_project_edit.text().strip() or None
        self.workspace_save_requested.emit(value)
        self.saved_label.setText(i18n.SETTINGS_SAVED)

    def set_theme_mode(self, mode: ThemeMode) -> None:
        button = self._theme_buttons.get(mode)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)

    def set_density_mode(self, mode: DensityMode) -> None:
        button = self._density_buttons.get(mode)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)

    def apply_density(self, mode: DensityMode) -> None:
        spacing = tokens.SPACE_MD if mode is DensityMode.COMPACT else tokens.SPACE_LG
        self.layout().setSpacing(spacing)

    def theme_buttons(self) -> dict[ThemeMode, QRadioButton]:
        return dict(self._theme_buttons)

    def density_buttons(self) -> dict[DensityMode, QRadioButton]:
        return dict(self._density_buttons)

    def data_source_buttons(self) -> dict[DataSourceMode, QRadioButton]:
        return dict(self._data_source_buttons)


class SettingsPage(BasePage):
    theme_mode_changed = Signal(object)
    density_mode_changed = Signal(object)
    window_geometry_reset_requested = Signal()
    workspace_save_requested = Signal(object)
    data_source_mode_changed = Signal(object)

    def __init__(
        self,
        current_mode: ThemeMode,
        current_density: DensityMode = DensityMode.COMFORTABLE,
        selected_project: str | None = None,
        current_data_source_mode: DataSourceMode = DataSourceMode.LOCAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "settings",
            i18n.SETTINGS_TITLE,
            i18n.SETTINGS_SUBTITLE,
            parent,
        )
        self.form = SettingsForm(
            current_mode,
            current_density,
            selected_project,
            current_data_source_mode,
        )
        self.form.theme_mode_changed.connect(self.theme_mode_changed.emit)
        self.form.density_mode_changed.connect(self.density_mode_changed.emit)
        self.form.window_geometry_reset_requested.connect(
            self.window_geometry_reset_requested.emit
        )
        self.form.workspace_save_requested.connect(self.workspace_save_requested.emit)
        self.form.data_source_mode_changed.connect(self.data_source_mode_changed.emit)
        self.add_content(self.form, stretch=1)

    def set_mode(self, mode: ThemeMode) -> None:
        self.form.set_theme_mode(mode)

    def buttons(self) -> dict[ThemeMode, QRadioButton]:
        """D1-compatible accessor retained for existing callers and tests."""
        return self.form.theme_buttons()

    def density_buttons(self) -> dict[DensityMode, QRadioButton]:
        return self.form.density_buttons()

    def data_source_buttons(self) -> dict[DataSourceMode, QRadioButton]:
        return self.form.data_source_buttons()

    def apply_density(self, mode: DensityMode) -> None:
        self.form.apply_density(mode)
