"""``TopBar`` — project switcher, global refresh, and the reserved status area.

Implements `DESIGN_SYSTEM.md` §7.4 and the two forward-compatible affordances of
`DESIGN_DIRECTIONS.md` §5: a reserved status area (present but empty in D1 — no
polling/live content) and a fixed slot for the project switcher.

In Desktop Increment 1 no page is data-scoped, so the project switcher renders in
its documented placeholder/disabled state (§7.5) — space reserved, wired to real
project scope in D3. The refresh action is present with its platform shortcut and
emits :attr:`refresh_requested`; the shell decides what (if anything) to refresh.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QToolButton, QWidget

from . import i18n, tokens


class TopBar(QWidget):
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(tokens.CONTROL_HEIGHT_TOPBAR)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            tokens.SPACE_LG, tokens.SPACE_XS, tokens.SPACE_LG, tokens.SPACE_XS
        )
        layout.setSpacing(tokens.SPACE_MD)

        # ProjectSwitcher (§7.5) — placeholder/disabled in D1.
        self.project_switcher = QComboBox()
        self.project_switcher.setObjectName("ProjectSwitcher")
        self.project_switcher.addItem(i18n.PROJECT_SWITCHER_PLACEHOLDER)
        self.project_switcher.setEnabled(False)
        self.project_switcher.setAccessibleName(i18n.PROJECT_SWITCHER_ACCESSIBLE)
        self.project_switcher.setAccessibleDescription(
            i18n.PROJECT_SWITCHER_DESCRIPTION
        )
        self.project_switcher.setMinimumWidth(200)
        layout.addWidget(self.project_switcher)

        layout.addStretch(1)

        # Reserved ambient status area (§5) — empty in D1, never live-updating.
        self.status_area = QLabel("")
        self.status_area.setObjectName("StatusArea")
        self.status_area.setAccessibleName(i18n.STATUS_AREA_ACCESSIBLE)
        self.status_area.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.status_area)

        # Global refresh action (§7.4 / IA §6).
        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("RefreshButton")
        self.refresh_button.setText(i18n.REFRESH_TEXT)
        self.refresh_button.setAccessibleName(i18n.REFRESH_TEXT)
        self.refresh_button.setToolTip(i18n.REFRESH_TOOLTIP)
        # Platform-appropriate accelerator: Qt maps "Ctrl" to the Command key on
        # macOS, so this is Cmd+R on macOS and Ctrl+R on Windows (IA §10) with no
        # OS branching of our own.
        self.refresh_button.setShortcut(QKeySequence("Ctrl+R"))
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self.refresh_button)

    def set_status_text(self, text: str) -> None:
        """Set a static, manually-refreshed status string (never a live poll)."""
        self.status_area.setText(text)
