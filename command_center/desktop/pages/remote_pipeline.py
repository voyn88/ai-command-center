"""«Конвейер» — read-only remote view of the autonomous pipeline.

Reads the native gateway's ``GET /v1/snapshot`` (its ``pipelineHealth`` block)
through `command_center.application.remote_gateway`. The fetch runs on a Qt
worker (`ARCHITECTURE.md` §9–§10) like every other page; the page only renders
the result. Auto-refresh every 60 s runs only while the page is visible.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from command_center.application.remote_gateway import (
    FetchOutcome,
    GatewayConfig,
    HealthLevel,
    RemoteSnapshot,
    fetch_with_config,
    health_level,
)

from .. import i18n
from ..workers import AdapterCallRunnable
from .base_page import BasePage

AUTO_REFRESH_MS = 60_000

ConfigProvider = Callable[[], "GatewayConfig | None"]
Fetcher = Callable[["GatewayConfig | None"], RemoteSnapshot]


def _table(object_name: str, headers: tuple[str, ...], accessible: str) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setObjectName(object_name)
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    table.setAccessibleName(accessible)
    return table


class RemotePipelinePage(BasePage):
    def __init__(
        self,
        config_provider: ConfigProvider,
        fetch: Fetcher = fetch_with_config,
        *,
        pool: QThreadPool | None = None,
        auto_refresh_ms: int = AUTO_REFRESH_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("pipeline", i18n.PIPELINE_TITLE, i18n.PIPELINE_SUBTITLE, parent)
        self._config_provider = config_provider
        self._fetch = fetch
        self._pool = pool
        self._request_id = 0
        self._runnables: dict[int, AdapterCallRunnable] = {}
        self._accept_signals = True
        self._cancel_event: threading.Event | None = None
        self.snapshot: RemoteSnapshot | None = None
        self.level: HealthLevel | None = None

        top = QWidget()
        top_row = QHBoxLayout(top)
        top_row.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel(i18n.PIPELINE_NOT_LOADED)
        self.status_label.setObjectName("PipelineStatus")
        self.status_label.setWordWrap(True)
        top_row.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton(i18n.PIPELINE_REFRESH)
        self.refresh_button.setObjectName("PipelineRefreshButton")
        self.refresh_button.setAccessibleName(i18n.PIPELINE_REFRESH)
        self.refresh_button.setAccessibleDescription(i18n.PIPELINE_REFRESH_DESCRIPTION)
        self.refresh_button.clicked.connect(lambda: self.refresh())
        top_row.addWidget(self.refresh_button)
        self.add_content(top)

        self.freshness_label = QLabel("")
        self.freshness_label.setObjectName("RowMeta")
        self.add_content(self.freshness_label)

        self.auth_warning = QLabel("")
        self.auth_warning.setObjectName("PipelineAuthWarning")
        self.auth_warning.setWordWrap(True)
        self.auth_warning.setVisible(False)
        self.add_content(self.auth_warning)

        self.monitor_label = QLabel("")
        self.monitor_label.setObjectName("PipelineMonitor")
        self.monitor_label.setWordWrap(True)
        self.add_content(self.monitor_label)

        metrics = QGroupBox(i18n.PIPELINE_METRICS_GROUP)
        grid = QGridLayout(metrics)
        self.metric_values: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(i18n.PIPELINE_METRIC_LABELS.items()):
            name = QLabel(label)
            value = QLabel("—")
            value.setObjectName(f"PipelineMetric_{key}")
            value.setAccessibleName(label)
            name.setBuddy(value)
            row, col = divmod(index, 3)
            grid.addWidget(name, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self.metric_values[key] = value
        self.metrics_group = metrics
        self.add_content(metrics)

        tables = QWidget()
        tables_row = QHBoxLayout(tables)
        tables_row.setContentsMargins(0, 0, 0, 0)
        backlog_box = QGroupBox(i18n.PIPELINE_BACKLOG_GROUP)
        QVBoxLayout(backlog_box)
        self.backlog_table = _table(
            "PipelineBacklogTable", i18n.PIPELINE_BACKLOG_COLUMNS, i18n.PIPELINE_BACKLOG_GROUP
        )
        backlog_box.layout().addWidget(self.backlog_table)
        tables_row.addWidget(backlog_box, 1)
        escalation_box = QGroupBox(i18n.PIPELINE_ESCALATIONS_GROUP)
        QVBoxLayout(escalation_box)
        self.escalations_empty = QLabel("")
        self.escalations_empty.setObjectName("RowMeta")
        escalation_box.layout().addWidget(self.escalations_empty)
        self.escalations_table = _table(
            "PipelineEscalationsTable",
            i18n.PIPELINE_ESCALATION_COLUMNS,
            i18n.PIPELINE_ESCALATIONS_GROUP,
        )
        escalation_box.layout().addWidget(self.escalations_table)
        tables_row.addWidget(escalation_box, 2)
        self.add_content(tables, stretch=1)

        self.timer = QTimer(self)
        self.timer.setObjectName("PipelineAutoRefresh")
        self.timer.setInterval(auto_refresh_ms)
        self.timer.timeout.connect(lambda: self.refresh())
        self._show_data(False)

    # --- rendering ----------------------------------------------------------
    def _show_data(self, visible: bool) -> None:
        self.metrics_group.setVisible(visible)
        self.backlog_table.parentWidget().setVisible(visible)
        self.escalations_table.parentWidget().setVisible(visible)
        if not visible:
            self.auth_warning.setVisible(False)
            self.auth_warning.setText("")
            self.monitor_label.setText("")

    def render(self, snapshot: RemoteSnapshot) -> None:
        self.snapshot = snapshot
        health = snapshot.health
        self.level = health_level(health) if health is not None else None

        if snapshot.freshness:
            freshness = i18n.PIPELINE_FRESHNESS_LABELS.get(
                snapshot.freshness, snapshot.freshness
            )
            self.freshness_label.setText(
                i18n.PIPELINE_FRESHNESS.format(
                    freshness=freshness, generated_at=snapshot.generated_at or "—"
                )
            )
        else:
            self.freshness_label.setText("")

        message_parts: list[str] = []
        if self.level is not None:
            message_parts.append(i18n.PIPELINE_LEVEL_LABELS[self.level.value])
        if snapshot.outcome is not FetchOutcome.OK:
            template = i18n.PIPELINE_OUTCOME_MESSAGES.get(
                snapshot.outcome.value, i18n.PIPELINE_OUTCOME_MESSAGES["invalid_response"]
            )
            message_parts.append(template.format(status=snapshot.http_status or "—"))
        self.status_label.setText(" · ".join(message_parts))
        self.status_label.setProperty(
            "level", self.level.value if self.level is not None else snapshot.outcome.value
        )

        if health is None:
            self._show_data(False)
            return
        self._show_data(True)
        values = {
            "ready": str(health.ready),
            "claimed": str(health.claimed),
            "dead": str(health.dead),
            "oldest_ready": i18n.pipeline_age(health.oldest_ready_age_seconds),
            "succeeded_last_hour": str(health.succeeded_last_hour),
            "active_lanes": ", ".join(
                f"{lane.role} ({lane.attempts})" for lane in health.lanes
            )
            or "0",
        }
        for key, text in values.items():
            self.metric_values[key].setText(text)

        if health.auth_failures_30m > 0:
            self.auth_warning.setText(
                f"{i18n.PIPELINE_AUTH_WARNING}\n"
                f"{i18n.PIPELINE_AUTH_COUNT.format(count=health.auth_failures_30m)}"
            )
            self.auth_warning.setVisible(True)
        else:
            self.auth_warning.setText("")
            self.auth_warning.setVisible(False)

        monitor = health.monitor
        if monitor is None:
            self.monitor_label.setText("")
        elif monitor.failing:
            self.monitor_label.setText(
                i18n.PIPELINE_MONITOR_FAILING.format(
                    failures=", ".join(monitor.failures) or monitor.verdict
                )
            )
        else:
            self.monitor_label.setText(i18n.PIPELINE_MONITOR_OK)

        backlog = sorted(health.backlog_by_status.items())
        self.backlog_table.setRowCount(len(backlog))
        for row, (status, count) in enumerate(backlog):
            label = i18n.PIPELINE_BACKLOG_STATUS_LABELS.get(status, status)
            self.backlog_table.setItem(row, 0, QTableWidgetItem(label))
            self.backlog_table.setItem(row, 1, QTableWidgetItem(str(count)))
        self.backlog_table.resizeColumnsToContents()

        escalations = health.escalations
        self.escalations_table.setRowCount(len(escalations))
        for row, item in enumerate(escalations):
            cells = (item.task_id, item.title, item.priority or "—", item.updated_at or "—")
            for col, text in enumerate(cells):
                self.escalations_table.setItem(row, col, QTableWidgetItem(text))
        self.escalations_table.resizeColumnsToContents()
        self.escalations_table.setVisible(bool(escalations))
        self.escalations_empty.setText("" if escalations else i18n.PIPELINE_ESCALATIONS_EMPTY)
        self.escalations_empty.setVisible(not escalations)

    # --- loading ------------------------------------------------------------
    def refresh(
        self, *, cancel_event: threading.Event | None = None
    ) -> AdapterCallRunnable | None:
        if not self._accept_signals:
            return None
        if cancel_event is not None:
            self._cancel_event = cancel_event
        self._request_id += 1
        request_id = self._request_id
        for old_id, old in tuple(self._runnables.items()):
            if old_id != request_id:
                old.cancel()
        self.status_label.setText(i18n.PIPELINE_LOADING)
        config = self._config_provider()  # read on the GUI thread
        runnable = AdapterCallRunnable(self._fetch, config, cancel_event=self._cancel_event)
        self._runnables[request_id] = runnable
        page_ref = weakref.ref(self)

        def current(page: RemotePipelinePage | None) -> bool:
            return page is not None and page._accept_signals and request_id == page._request_id

        def deliver(snapshot: RemoteSnapshot) -> None:
            page = page_ref()
            if current(page):
                page.render(snapshot)

        def fail(_exc: Exception) -> None:
            page = page_ref()
            if current(page):
                page.render(RemoteSnapshot(FetchOutcome.INVALID_RESPONSE))

        def release() -> None:
            page = page_ref()
            if page is not None:
                page._runnables.pop(request_id, None)

        runnable.signals.result.connect(deliver)
        runnable.signals.error.connect(fail)
        runnable.signals.finished.connect(release)
        (self._pool or QThreadPool.globalInstance()).start(runnable)
        return runnable

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._accept_signals:
            self.timer.start()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self.timer.stop()

    def shutdown_workers(self) -> None:
        self._accept_signals = False
        self.timer.stop()
        for runnable in tuple(self._runnables.values()):
            runnable.cancel()
        self._runnables.clear()
