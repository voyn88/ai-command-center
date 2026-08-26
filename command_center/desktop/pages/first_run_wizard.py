"""First-run setup wizard (D-1 "workable out of the box").

A modal dialog shown before the shell when no workspace is configured yet:
a dependency health checklist (per-item status + fix hint) and the workspace
root form. On accept it initializes the workspace layout, exports the runtime
environment, and persists the root in the settings store, so the next launch
skips the wizard and every later launch lands directly on Home.

Presentation only: the checks and validation live in the Qt-free application
layer (`command_center.application.first_run`).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from command_center.application.first_run import (
    HealthCheckItem,
    HealthStatus,
    blocking_errors,
    default_workspace_root,
    initialize_workspace,
    run_health_checks,
    validate_workspace_root,
)
from command_center.platform import SettingsStore

from .. import i18n, tokens

_STATUS_COLOR = {
    HealthStatus.OK: "#2e7d32",
    HealthStatus.WARNING: "#b26a00",
    HealthStatus.ERROR: "#c62828",
}


class HealthCheckRow(QWidget):
    """One dependency row: name, status label, detail, optional fix hint."""

    def __init__(self, item: HealthCheckItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"HealthCheckRow_{item.check_id}")
        self.item = item

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, tokens.SPACE_XS)
        layout.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(tokens.SPACE_SM)
        name = QLabel(item.name)
        name.setObjectName("HealthCheckName")
        head.addWidget(name)

        status_text = i18n.FIRST_RUN_STATUS_LABELS[item.status.value]
        self.status_label = QLabel(status_text)
        self.status_label.setObjectName("HealthCheckStatus")
        self.status_label.setStyleSheet(
            f"color: {_STATUS_COLOR[item.status]}; font-weight: 600;"
        )
        self.status_label.setAccessibleName(f"{item.name}: {status_text}")
        head.addWidget(self.status_label)
        head.addStretch(1)
        layout.addLayout(head)

        detail = QLabel(item.detail)
        detail.setObjectName("HealthCheckDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        if item.fix_hint:
            hint = QLabel(item.fix_hint)
            hint.setObjectName("HealthCheckHint")
            hint.setWordWrap(True)
            hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(hint)


class FirstRunWizard(QDialog):
    """Modal first-run setup: environment checklist + workspace root form."""

    def __init__(
        self,
        settings: SettingsStore,
        *,
        health_checks: Callable[[], tuple[HealthCheckItem, ...]] = run_health_checks,
        initialize: Callable[[str], object] = initialize_workspace,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._health_checks = health_checks
        self._initialize = initialize
        self.workspace_root: str | None = None

        self.setObjectName("FirstRunWizard")
        self.setWindowTitle(i18n.FIRST_RUN_TITLE)
        self.setModal(True)
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(
            tokens.SPACE_XL, tokens.SPACE_XL, tokens.SPACE_XL, tokens.SPACE_XL
        )
        root.setSpacing(tokens.SPACE_LG)

        title = QLabel(i18n.FIRST_RUN_TITLE)
        title.setObjectName("FirstRunTitle")
        root.addWidget(title)
        subtitle = QLabel(i18n.FIRST_RUN_SUBTITLE)
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self._checks_group = QGroupBox(i18n.FIRST_RUN_CHECKS_GROUP)
        self._checks_group.setAccessibleName(i18n.FIRST_RUN_CHECKS_ACCESSIBLE)
        self._checks_layout = QVBoxLayout(self._checks_group)
        self._checks_layout.setSpacing(tokens.SPACE_SM)
        self._errors_note = QLabel(i18n.FIRST_RUN_ERRORS_NOTE)
        self._errors_note.setObjectName("FirstRunErrorsNote")
        self._errors_note.setWordWrap(True)
        root.addWidget(self._checks_group)
        root.addWidget(self._errors_note)

        self.recheck_button = QPushButton(i18n.FIRST_RUN_RECHECK)
        self.recheck_button.setObjectName("FirstRunRecheck")
        self.recheck_button.clicked.connect(self.refresh_checks)
        root.addWidget(self.recheck_button, alignment=Qt.AlignLeft)

        workspace_group = QGroupBox(i18n.FIRST_RUN_WORKSPACE_GROUP)
        form = QFormLayout(workspace_group)
        path_row = QHBoxLayout()
        self.workspace_edit = QLineEdit(str(default_workspace_root()))
        self.workspace_edit.setObjectName("FirstRunWorkspaceEdit")
        self.workspace_edit.setAccessibleName(i18n.FIRST_RUN_WORKSPACE_LABEL)
        path_row.addWidget(self.workspace_edit, 1)
        self.browse_button = QPushButton(i18n.FIRST_RUN_BROWSE)
        self.browse_button.setObjectName("FirstRunBrowse")
        self.browse_button.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_button)
        form.addRow(QLabel(i18n.FIRST_RUN_WORKSPACE_LABEL), path_row)
        description = QLabel(i18n.FIRST_RUN_WORKSPACE_DESCRIPTION)
        description.setWordWrap(True)
        form.addRow(description)
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("FirstRunValidationError")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet(
            f"color: {_STATUS_COLOR[HealthStatus.ERROR]};"
        )
        self.validation_label.hide()
        form.addRow(self.validation_label)
        root.addWidget(workspace_group)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.quit_button = QPushButton(i18n.FIRST_RUN_QUIT)
        self.quit_button.setObjectName("FirstRunQuit")
        self.quit_button.clicked.connect(self.reject)
        buttons.addWidget(self.quit_button)
        self.continue_button = QPushButton(i18n.FIRST_RUN_CONTINUE)
        self.continue_button.setObjectName("FirstRunContinue")
        self.continue_button.setDefault(True)
        self.continue_button.clicked.connect(self._on_continue)
        buttons.addWidget(self.continue_button)
        root.addLayout(buttons)

        self.check_rows: list[HealthCheckRow] = []
        self.refresh_checks()

    # --- health checks -----------------------------------------------------
    def refresh_checks(self) -> None:
        for row in self.check_rows:
            self._checks_layout.removeWidget(row)
            row.deleteLater()
        self.check_rows = []
        items = self._health_checks()
        for item in items:
            row = HealthCheckRow(item, self._checks_group)
            self._checks_layout.addWidget(row)
            self.check_rows.append(row)
        self._errors_note.setVisible(bool(blocking_errors(items)))

    # --- workspace form ----------------------------------------------------
    def _on_browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, i18n.FIRST_RUN_WORKSPACE_LABEL, self.workspace_edit.text()
        )
        if selected:
            self.workspace_edit.setText(selected)

    def _on_continue(self) -> None:
        raw = self.workspace_edit.text()
        error = validate_workspace_root(raw)
        if error is None:
            try:
                resolved = self._initialize(raw)
            except (ValueError, OSError) as exc:
                error = str(exc)
            else:
                self.workspace_root = str(resolved)
                self._settings.set_workspace_root(self.workspace_root)
                self._settings.sync()
                self.validation_label.hide()
                self.accept()
                return
        self.validation_label.setText(error)
        self.validation_label.show()
