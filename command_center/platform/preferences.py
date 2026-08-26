"""Platform-native desktop preferences backed by :class:`QSettings`."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QByteArray, QSettings

ORGANIZATION = "AI Command Center"
APPLICATION = "AI Command Center Desktop"

_KEY_GEOMETRY = "window/geometry"
_KEY_WINDOW_STATE = "window/state"
_KEY_THEME = "appearance/theme"
_KEY_DENSITY = "appearance/density"
_KEY_SELECTED_PROJECT = "workspace/selected_project"
_KEY_WORKSPACE_ROOT = "workspace/root"


class ThemeMode(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"

    @classmethod
    def from_value(cls, value: object, default: "ThemeMode") -> "ThemeMode":
        try:
            return cls(str(value))
        except ValueError:
            return default


class DensityMode(str, Enum):
    COMFORTABLE = "comfortable"
    COMPACT = "compact"

    @classmethod
    def from_value(cls, value: object, default: "DensityMode") -> "DensityMode":
        try:
            return cls(str(value))
        except ValueError:
            return default


class SettingsStore:
    """Typed preference access over one injectable platform-native handle."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or settings_handle()

    @property
    def qsettings(self) -> QSettings:
        return self._settings

    def theme_mode(self, default: ThemeMode = ThemeMode.SYSTEM) -> ThemeMode:
        return ThemeMode.from_value(self._settings.value(_KEY_THEME), default)

    def set_theme_mode(self, mode: ThemeMode) -> None:
        self._settings.setValue(_KEY_THEME, mode.value)

    def density_mode(
        self, default: DensityMode = DensityMode.COMFORTABLE
    ) -> DensityMode:
        return DensityMode.from_value(self._settings.value(_KEY_DENSITY), default)

    def set_density_mode(self, mode: DensityMode) -> None:
        self._settings.setValue(_KEY_DENSITY, mode.value)

    def geometry(self) -> QByteArray | None:
        value = self._settings.value(_KEY_GEOMETRY)
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def set_geometry(self, geometry: QByteArray) -> None:
        self._settings.setValue(_KEY_GEOMETRY, geometry)

    def window_state(self) -> QByteArray | None:
        value = self._settings.value(_KEY_WINDOW_STATE)
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def set_window_state(self, state: QByteArray) -> None:
        self._settings.setValue(_KEY_WINDOW_STATE, state)

    def reset_window_geometry(self) -> None:
        self._settings.remove(_KEY_GEOMETRY)
        self._settings.remove(_KEY_WINDOW_STATE)

    def selected_project(self) -> str | None:
        value = self._settings.value(_KEY_SELECTED_PROJECT)
        return str(value) if value not in (None, "") else None

    def set_selected_project(self, project_id: str | None) -> None:
        normalized = project_id.strip() if project_id else ""
        if normalized:
            self._settings.setValue(_KEY_SELECTED_PROJECT, normalized)
        else:
            self._settings.remove(_KEY_SELECTED_PROJECT)

    def workspace_root(self) -> str | None:
        """Workspace root chosen in the first-run wizard (D-1), if any."""
        value = self._settings.value(_KEY_WORKSPACE_ROOT)
        return str(value) if value not in (None, "") else None

    def set_workspace_root(self, path: str | None) -> None:
        normalized = path.strip() if path else ""
        if normalized:
            self._settings.setValue(_KEY_WORKSPACE_ROOT, normalized)
        else:
            self._settings.remove(_KEY_WORKSPACE_ROOT)

    def sync(self) -> None:
        self._settings.sync()


def settings_handle() -> QSettings:
    """Return the native NSUserDefaults/registry-backed settings handle."""
    return QSettings(ORGANIZATION, APPLICATION)
