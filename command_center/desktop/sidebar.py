"""``Sidebar`` — primary navigation listing the nine sections.

Implements `DESIGN_SYSTEM.md` §7.2 / `INFORMATION_ARCHITECTURE.md` §2. Builds one
:class:`NavigationItem` per :data:`command_center.desktop.sections.SECTIONS`
entry, keeps single-selection via a ``QButtonGroup``, and emits
:attr:`section_selected` when an enabled item is activated. Disabled items are
inert and never emit.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QLabel, QVBoxLayout, QWidget

from command_center.platform import DensityMode

from . import i18n, tokens
from .navigation_item import NavigationItem
from .sections import SECTIONS, Section


class Sidebar(QWidget):
    section_selected = Signal(str)  # emits the selected section key

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(220)
        self.setAccessibleName(i18n.NAV_ACCESSIBLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            tokens.SPACE_SM, tokens.SPACE_MD, tokens.SPACE_SM, tokens.SPACE_MD
        )
        layout.setSpacing(tokens.SPACE_XS)

        title = QLabel(i18n.NAV_HEADER)
        title.setObjectName("SidebarTitle")
        layout.addWidget(title)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._items: dict[str, NavigationItem] = {}

        for section in SECTIONS:
            item = self._build_item(section)
            self._items[section.key] = item
            layout.addWidget(item)
            if section.enabled:
                self._group.addButton(item)

        layout.addStretch(1)

    def _build_item(self, section: Section) -> NavigationItem:
        item = NavigationItem(section.key, section.label, enabled=section.enabled)
        if section.enabled:
            item.clicked.connect(
                lambda _checked=False, key=section.key: self._on_clicked(key)
            )
        return item

    def _on_clicked(self, key: str) -> None:
        # A checkable button re-emits clicked even when already current; keep the
        # selection sticky and emit so the shell can (idempotently) switch pages.
        self._items[key].setChecked(True)
        self.section_selected.emit(key)

    def items(self) -> dict[str, NavigationItem]:
        return dict(self._items)

    def set_current(self, key: str) -> None:
        """Reflect the active section in the UI without re-emitting selection."""
        item = self._items.get(key)
        if item is not None and item.isEnabled():
            item.setChecked(True)

    def apply_density(self, density: DensityMode) -> None:
        height = (
            tokens.CONTROL_HEIGHT_MD
            if density is DensityMode.COMPACT
            else tokens.CONTROL_HEIGHT_LG
        )
        spacing = tokens.SPACE_MD if density is DensityMode.COMPACT else tokens.SPACE_LG
        self.layout().setSpacing(spacing)
        for item in self._items.values():
            item.setMinimumHeight(height)
            item.setMaximumHeight(height)

    @property
    def current_key(self) -> str | None:
        for key, item in self._items.items():
            if item.isChecked():
                return key
        return None
