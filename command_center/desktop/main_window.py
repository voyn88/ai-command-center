"""``AppShell`` — the single top-level window composing the shell.

Implements `DESIGN_SYSTEM.md` §7.1 and `ARCHITECTURE.md` §3/§13/§14. Owns the
sidebar, top bar, and the page stack; restores/persists window geometry through
:class:`SettingsStore`; and performs a bounded clean shutdown (§13) — signalling
any in-flight ``QThreadPool`` workers to stop cooperatively and waiting up to a
fixed timeout before the window closes. D1 has no data workers yet, so this is the
seam D2 extends rather than a redesign point.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from command_center.platform import DataSourceMode, DensityMode

from . import i18n
from .pages.home import HomePage
from .pages.operational import OperationalPage
from .pages.projects import ProjectsPage
from .pages.settings_page import SettingsPage
from .settings import SettingsStore
from .sections import ACTIVE_SECTION_KEYS, DEFAULT_SECTION_KEY
from .sidebar import Sidebar
from .theme import ThemeController, ThemeMode
from .top_bar import TopBar

WINDOW_TITLE = "AI Command Center"
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 760
MIN_WIDTH = 900
MIN_HEIGHT = 600
SHUTDOWN_TIMEOUT_MS = 5000


class AppShell(QWidget):
    """Top-level shell window. A ``QWidget`` (not ``QMainWindow``) is sufficient
    here: the shell composes its own sidebar/top bar rather than using Qt's
    dock/toolbar/menu chrome, keeping the layout fully token-driven."""

    refresh_requested = Signal()

    def __init__(
        self,
        settings: SettingsStore,
        theme: ThemeController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._theme = theme
        # Cooperative-cancellation flag shared with background workers
        # (`ARCHITECTURE.md` §11). Set on shutdown; workers poll it at checkpoints.
        self._cancel_event = threading.Event()
        # Workspace Home data adapter, wired by :meth:`load_workspace_home`.
        self._adapter: object | None = None
        self._operations_adapter: object | None = None

        self.setObjectName("AppShell")
        self.setWindowTitle(WINDOW_TITLE)
        self.setAccessibleName(WINDOW_TITLE)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self._build_ui()
        self._apply_density(self._theme.density)
        self._restore_geometry()
        # Select the default section without emitting through the sidebar signal.
        self._activate_section(DEFAULT_SECTION_KEY)
        self.sidebar.set_current(DEFAULT_SECTION_KEY)

    # --- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.section_selected.connect(self._on_section_selected)
        root.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.top_bar = TopBar()
        self.top_bar.refresh_requested.connect(self._on_refresh)
        right_layout.addWidget(self.top_bar)

        self.stack = QStackedWidget()
        self._pages: dict[str, QWidget] = {}

        home = HomePage()
        home.navigate_requested.connect(self.navigate_to)
        self._home = home
        # Keep the Home page's dynamic badges in step with the theme, and colour
        # them for the palette already applied before this shell was constructed.
        self._theme.palette_changed.connect(home.apply_palette)
        if self._theme.palette is not None:
            home.apply_palette(self._theme.palette)
        self._add_page(home)

        self._add_page(ProjectsPage())

        operational_columns = {
            "sessions": (("project", "Проект"), ("state", "Состояние"), ("id", "ID"), ("updated_at", "Обновлено")),
            "execution": (("project", "Проект"), ("state", "Состояние"), ("task_type", "Тип задачи"), ("run_id", "ID запуска"), ("created_at", "Создан")),
            "git": (("project", "Проект"), ("state", "Состояние"), ("branch", "Ветка"), ("worktrees", "Worktree"), ("path", "Путь")),
            "artifacts": (("project", "Проект"), ("task_type", "Тип задачи"), ("created_at", "Создан"), ("path", "Путь")),
            "reports": (("project", "Проект"), ("verdict", "Заключение"), ("created_at", "Создан"), ("path", "Путь")),
            "agents": (("display_name", "Агент"), ("readiness", "Состояние"), ("running", "Активные запуски"), ("detail", "Подробности")),
        }
        self._operational_pages: dict[str, OperationalPage] = {}
        for key, columns in operational_columns.items():
            title, description = i18n.OPERATIONAL_PAGE_TEXT[key]
            page = OperationalPage(key, title, description, columns=columns)
            self._operational_pages[key] = page
            self._add_page(page)

        settings_page = SettingsPage(
            self._theme.mode,
            self._settings.density_mode(),
            self._settings.selected_project(),
            self._settings.data_source_mode(),
        )
        settings_page.theme_mode_changed.connect(self._on_theme_mode_changed)
        settings_page.density_mode_changed.connect(self._on_density_mode_changed)
        settings_page.window_geometry_reset_requested.connect(
            self._on_window_geometry_reset_requested
        )
        settings_page.workspace_save_requested.connect(
            self._on_workspace_save_requested
        )
        settings_page.data_source_mode_changed.connect(
            self._on_data_source_mode_changed
        )
        self._settings_page = settings_page
        self._add_page(settings_page)

        right_layout.addWidget(self.stack, 1)
        root.addWidget(right, 1)

    def _add_page(self, page: QWidget) -> None:
        key = page.section_key  # type: ignore[attr-defined]
        self._pages[key] = page
        self.stack.addWidget(page)

    # --- navigation --------------------------------------------------------
    def _on_section_selected(self, key: str) -> None:
        self._activate_section(key)

    def navigate_to(self, key: str) -> None:
        """Programmatic navigation (e.g. an EmptyState next-action). Reflects the
        selection in the sidebar too, keeping the two in sync."""
        if key not in ACTIVE_SECTION_KEYS:
            return
        self.sidebar.set_current(key)
        self._activate_section(key)

    def _activate_section(self, key: str) -> None:
        page = self._pages.get(key)
        if page is not None:
            self.stack.setCurrentWidget(page)
            self._load_operational_page(key)

    @property
    def current_section_key(self) -> str | None:
        current = self.stack.currentWidget()
        for key, page in self._pages.items():
            if page is current:
                return key
        return None

    # --- theme -------------------------------------------------------------
    def _on_theme_mode_changed(self, mode: ThemeMode) -> None:
        self._theme.set_mode(mode)
        self._settings.set_theme_mode(mode)

    def _on_density_mode_changed(self, mode: DensityMode) -> None:
        self._theme.set_density(mode)
        self._apply_density(mode)
        self._settings.set_density_mode(mode)

    def _apply_density(self, mode: DensityMode) -> None:
        self.sidebar.apply_density(mode)
        self._settings_page.apply_density(mode)

    def _on_window_geometry_reset_requested(self) -> None:
        self._settings.reset_window_geometry()
        self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

    def _on_workspace_save_requested(self, project_id: str | None) -> None:
        self._settings.set_selected_project(project_id)
        self._settings.sync()

    def _on_data_source_mode_changed(self, mode: DataSourceMode) -> None:
        self._settings.set_data_source_mode(mode)
        self._settings.sync()
        # The toggle is meant to take effect immediately, not on the next
        # navigation/refresh (VOYN-W0-APP-CONTROL-S2): re-load "Выполнение"
        # right away if it is the page currently on screen.
        if self.current_section_key == "execution":
            self._load_operational_page("execution")

    # --- data / refresh ----------------------------------------------------
    def load_workspace_home(self, adapter: object, operations_adapter: object | None = None) -> None:
        """Wire the Workspace Home data adapter and start the first async load."""
        self._adapter = adapter
        self._operations_adapter = operations_adapter
        self._home.load(adapter, cancel_event=self._cancel_event)

    def _load_operational_page(self, key: str) -> None:
        page = self._operational_pages.get(key)
        adapter = self._operations_adapter
        fetch = getattr(adapter, key, None) if adapter is not None else None
        if page is not None and callable(fetch):
            page.load(fetch, cancel_event=self._cancel_event)

    def _on_refresh(self) -> None:
        # Re-run the active page's data load when an adapter is wired; always
        # re-emit so callers/tests can observe the user's refresh intent.
        current = self.current_section_key
        if current == "home" and self._adapter is not None:
            self._home.load(self._adapter, cancel_event=self._cancel_event)
        elif current is not None:
            self._load_operational_page(current)
        self.refresh_requested.emit()

    # --- geometry / lifecycle ---------------------------------------------
    def _restore_geometry(self) -> None:
        geometry = self._settings.geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

    def _persist_state(self) -> None:
        self._settings.set_geometry(self.saveGeometry())
        self._settings.set_theme_mode(self._theme.mode)
        self._settings.set_density_mode(self._theme.density)
        self._settings.sync()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def shutdown(self, timeout_ms: int = SHUTDOWN_TIMEOUT_MS) -> bool:
        """Bounded clean shutdown (`ARCHITECTURE.md` §13). Signals cooperative
        cancellation, waits up to ``timeout_ms`` for the global thread pool to
        drain, then persists state. Returns whether the pool drained in time."""
        self._cancel_event.set()
        self._home.shutdown_workers()
        for page in self._operational_pages.values():
            page.shutdown_workers()
        drained = QThreadPool.globalInstance().waitForDone(timeout_ms)
        self._persist_state()
        return drained

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self.shutdown()
        event.accept()
