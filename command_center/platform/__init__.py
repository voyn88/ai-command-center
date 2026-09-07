"""The sole public OS-abstraction boundary for the native desktop client."""

from .file_manager import reveal_in_file_manager
from .paths import (
    cache_dir,
    configure_runtime_environment,
    crash_dir,
    log_dir,
    platform_name,
)
from .preferences import (
    APPLICATION,
    ORGANIZATION,
    DataSourceMode,
    DensityMode,
    SettingsStore,
    ThemeMode,
    settings_handle,
)
from .resources import resource, resource_path
from .theme import SystemThemeMonitor, system_theme

__all__ = [
    "APPLICATION",
    "ORGANIZATION",
    "DataSourceMode",
    "DensityMode",
    "SettingsStore",
    "SystemThemeMonitor",
    "ThemeMode",
    "cache_dir",
    "configure_runtime_environment",
    "crash_dir",
    "log_dir",
    "platform_name",
    "reveal_in_file_manager",
    "resource",
    "resource_path",
    "settings_handle",
    "system_theme",
]
