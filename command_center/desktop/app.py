"""Application assembly and entry point wiring for the desktop shell.

Constructs the one ``QApplication`` (`ARCHITECTURE.md` §3), the settings store,
the theme controller (applying the persisted mode), and the :class:`AppShell`
main window — in the documented startup order. Kept import-light: importing this
module constructs nothing (no ``QApplication`` at import time), satisfying the D1A
smoke-test contract.

D-1 additions: :func:`run` gates on first-run configuration (unconfigured
machine → setup wizard before the shell) and wraps the whole startup
composition in a top-level error boundary — known failures become actionable
Russian messages with the full traceback behind the dialog's details expander,
never a console traceback.
"""

from __future__ import annotations

import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from command_center.application.first_run import (
    StartupFailure,
    humanize_startup_error,
    workspace_is_configured,
)
from command_center.platform.paths import configure_runtime_environment

from .main_window import AppShell
from .settings import APPLICATION, ORGANIZATION, SettingsStore
from .theme import ThemeController


def build_shell(
    app: QApplication, settings: SettingsStore | None = None
) -> tuple[AppShell, ThemeController]:
    """Wire settings → theme → main window against an existing ``QApplication``.

    Split out from :func:`run` so tests can build the shell under pytest-qt's
    offscreen ``QApplication`` and inject an isolated :class:`SettingsStore`.
    """
    store = settings or SettingsStore()
    theme = ThemeController(
        app,
        mode=store.theme_mode(),
        density=store.density_mode(),
    )
    theme.apply()
    shell = AppShell(store, theme)
    return shell, theme


def _get_or_create_app(argv: list[str]) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    app = QApplication(argv)
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    return app


def _ensure_workspace_configured(store: SettingsStore) -> bool:
    """Point the runtime at a workspace; run the first-run wizard if none.

    Order: an already-exported environment or conventional directory wins; then
    the root persisted by a previous wizard run; otherwise the modal wizard.
    Returns False only when the user quit the wizard without configuring.
    """
    persisted = store.workspace_root()
    if persisted:
        configure_runtime_environment(persisted)
    if workspace_is_configured():
        configure_runtime_environment()
        return True

    from .pages.first_run_wizard import FirstRunWizard

    wizard = FirstRunWizard(store)
    accepted = wizard.exec() == int(wizard.DialogCode.Accepted)
    return accepted and wizard.workspace_root is not None


def _show_startup_error(error: BaseException) -> None:
    """Top-level boundary presentation: humanized message, trace in expander."""
    failure: StartupFailure = humanize_startup_error(error)
    from .i18n import STARTUP_ERROR_DETAILS_HINT, STARTUP_ERROR_WINDOW_TITLE

    box = QMessageBox()
    box.setObjectName("StartupErrorBox")
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(STARTUP_ERROR_WINDOW_TITLE)
    box.setText(failure.title)
    box.setInformativeText(
        f"{failure.message}\n\n{failure.hint}\n\n{STARTUP_ERROR_DETAILS_HINT}"
    )
    box.setDetailedText(
        "".join(traceback.format_exception(type(error), error, error.__traceback__))
    )
    box.exec()


def run(argv: list[str] | None = None) -> int:
    """Launch the desktop application and run the Qt event loop to completion."""
    argv = list(sys.argv if argv is None else argv)
    app = _get_or_create_app(argv)
    store = SettingsStore()

    try:
        if not _ensure_workspace_configured(store):
            return 0  # user quit the first-run wizard — a clean, chosen exit

        shell, _theme = build_shell(app, store)
        # Wire the Workspace Home data adapter and kick off the first async load
        # (D2). Import here so importing this module constructs no
        # ExecutionCenterAPI or runtime database at import time (keeps the D1A
        # smoke-test contract).
        from command_center.application.operations_adapter import OperationsAdapter
        from command_center.application.workspace_home_adapter import (
            WorkspaceHomeAdapter,
        )

        home_adapter = WorkspaceHomeAdapter()
        shell.load_workspace_home(
            home_adapter,
            OperationsAdapter(workspace_home_adapter=home_adapter),
        )
        shell.show()
    except Exception as error:  # top-level render boundary (D-1 AC 3)
        _show_startup_error(error)
        return 1
    return app.exec()
