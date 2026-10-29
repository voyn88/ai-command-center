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
    QFrame,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from command_center.platform import DensityMode

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


class SettingsForm(QWidget):
    """Accessible preference form; persistence remains owned by the shell."""

    theme_mode_changed = Signal(object)
    density_mode_changed = Signal(object)
    window_geometry_reset_requested = Signal()
    workspace_save_requested = Signal(object)
    remote_save_requested = Signal(object)  # (url, token, pin) — never logged

    def __init__(
        self,
        current_mode: ThemeMode,
        current_density: DensityMode,
        selected_project: str | None,
        parent: QWidget | None = None,
        remote_gateway: tuple[str | None, str | None, str | None] = (None, None, None),
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
        root.addWidget(self._build_remote_group(remote_gateway))
        root.addStretch(1)
        self.apply_density(current_density)

    def _build_remote_group(
        self, remote_gateway: tuple[str | None, str | None, str | None]
    ) -> QGroupBox:
        url, token, pin = remote_gateway
        remote = QGroupBox(i18n.SETTINGS_REMOTE_GROUP)
        box = QFormLayout(remote)
        self.remote_url_edit = self._remote_field(
            "RemoteGatewayUrl",
            url,
            i18n.SETTINGS_REMOTE_URL_LABEL,
            i18n.SETTINGS_REMOTE_URL_DESCRIPTION,
            i18n.SETTINGS_REMOTE_URL_PLACEHOLDER,
            box,
        )
        self.remote_token_edit = self._remote_field(
            "RemoteGatewayToken",
            token,
            i18n.SETTINGS_REMOTE_TOKEN_LABEL,
            i18n.SETTINGS_REMOTE_TOKEN_DESCRIPTION,
            "",
            box,
        )
        self.remote_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remote_pin_edit = self._remote_field(
            "RemoteGatewayPin",
            pin,
            i18n.SETTINGS_REMOTE_PIN_LABEL,
            i18n.SETTINGS_REMOTE_PIN_DESCRIPTION,
            i18n.SETTINGS_REMOTE_PIN_PLACEHOLDER,
            box,
        )
        self.save_remote_button = QPushButton(i18n.SETTINGS_SAVE_REMOTE)
        self.save_remote_button.setObjectName("SaveRemoteGatewayButton")
        self.save_remote_button.clicked.connect(self._save_remote)
        box.addRow(self.save_remote_button)
        self.remote_saved_label = QLabel("")
        self.remote_saved_label.setObjectName("RemoteSettingsSavedStatus")
        self.remote_saved_label.setAccessibleName(i18n.SETTINGS_SAVED)
        box.addRow(self.remote_saved_label)
        return remote

    @staticmethod
    def _remote_field(
        object_name: str,
        value: str | None,
        label_text: str,
        description: str,
        placeholder: str,
        box: QFormLayout,
    ) -> QLineEdit:
        edit = QLineEdit(value or "")
        edit.setObjectName(object_name)
        edit.setAccessibleName(label_text)
        edit.setAccessibleDescription(description)
        if placeholder:
            edit.setPlaceholderText(placeholder)
        label = QLabel(label_text)
        label.setBuddy(edit)
        box.addRow(label, edit)
        return edit

    def _save_remote(self) -> None:
        values = tuple(
            edit.text().strip() or None
            for edit in (self.remote_url_edit, self.remote_token_edit, self.remote_pin_edit)
        )
        self.remote_save_requested.emit(values)
        self.remote_saved_label.setText(i18n.SETTINGS_SAVED)

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


class SettingsPage(BasePage):
    theme_mode_changed = Signal(object)
    density_mode_changed = Signal(object)
    window_geometry_reset_requested = Signal()
    workspace_save_requested = Signal(object)
    remote_save_requested = Signal(object)

    def __init__(
        self,
        current_mode: ThemeMode,
        current_density: DensityMode = DensityMode.COMFORTABLE,
        selected_project: str | None = None,
        parent: QWidget | None = None,
        remote_gateway: tuple[str | None, str | None, str | None] = (None, None, None),
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
            remote_gateway=remote_gateway,
        )
        self.form.theme_mode_changed.connect(self.theme_mode_changed.emit)
        self.form.density_mode_changed.connect(self.density_mode_changed.emit)
        self.form.window_geometry_reset_requested.connect(
            self.window_geometry_reset_requested.emit
        )
        self.form.workspace_save_requested.connect(self.workspace_save_requested.emit)
        self.form.remote_save_requested.connect(self.remote_save_requested.emit)
        # The form outgrows a 600 px window once the remote group is present:
        # scroll instead of squeezing the groups.
        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.form)
        self.add_content(scroll, stretch=1)

    def set_mode(self, mode: ThemeMode) -> None:
        self.form.set_theme_mode(mode)

    def buttons(self) -> dict[ThemeMode, QRadioButton]:
        """D1-compatible accessor retained for existing callers and tests."""
        return self.form.theme_buttons()

    def density_buttons(self) -> dict[DensityMode, QRadioButton]:
        return self.form.density_buttons()

    def apply_density(self, mode: DensityMode) -> None:
        self.form.apply_density(mode)
