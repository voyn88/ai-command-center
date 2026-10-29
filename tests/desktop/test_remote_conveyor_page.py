"""«Конвейер» page: state rendering, background refresh, shell + settings wiring."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from command_center.application.remote_gateway import (
    Escalation,
    FetchOutcome,
    GatewayConfig,
    HealthLevel,
    Lane,
    MonitorVerdict,
    PipelineHealth,
    RemoteSnapshot,
)
from command_center.desktop import i18n
from command_center.desktop.pages.remote_pipeline import RemotePipelinePage
from command_center.desktop.settings import SettingsStore

CONFIG = GatewayConfig("https://gw.example:8443", "tok-secret", None)


def _health(**overrides) -> PipelineHealth:
    values = dict(
        observed_at="2026-08-26T00:00:00Z",
        ready=3,
        claimed=2,
        dead=0,
        succeeded_last_hour=7,
        oldest_ready_age_seconds=95,
        lanes=(Lane("aicc_worker", 2, 14),),
        auth_failures_30m=0,
        backlog_by_status={"OPEN": 5, "DONE": 40},
        escalations=(),
        monitor=MonitorVerdict("ok", 0, ()),
    )
    values.update(overrides)
    return PipelineHealth(**values)


def _snap(health=None, outcome=FetchOutcome.OK, freshness="fresh") -> RemoteSnapshot:
    return RemoteSnapshot(
        outcome=outcome,
        freshness=freshness,
        generated_at="2026-08-26T00:00:00Z",
        health=health,
    )


def _page(qtbot, fetch=None, config=CONFIG) -> RemotePipelinePage:
    page = RemotePipelinePage(lambda: config, fetch or (lambda _c: _snap(_health())))
    qtbot.addWidget(page)
    return page


def test_ok_state(qtbot):
    page = _page(qtbot)
    page.render(_snap(_health()))
    assert page.level is HealthLevel.OK
    assert page.status_label.text() == i18n.PIPELINE_LEVEL_LABELS["ok"]
    assert page.metric_values["ready"].text() == "3"
    assert page.metric_values["oldest_ready"].text() == "1 мин"
    assert page.metric_values["succeeded_last_hour"].text() == "7"
    assert "aicc_worker" in page.metric_values["active_lanes"].text()
    assert not page.auth_warning.isVisibleTo(page)
    assert page.backlog_table.rowCount() == 2
    assert page.escalations_table.rowCount() == 0
    assert page.escalations_empty.text() == i18n.PIPELINE_ESCALATIONS_EMPTY


def test_attention_state_with_auth_failures_and_escalations(qtbot):
    page = _page(qtbot)
    health = _health(
        auth_failures_30m=3,
        escalations=(Escalation("VOYN-1", "Нужно решение", "P1", "2026-08-25T22:00:00Z"),),
        monitor=MonitorVerdict("failing", 1, ("dead_letter_growth",)),
    )
    page.render(_snap(health))
    assert page.level is HealthLevel.ATTENTION
    assert i18n.PIPELINE_LEVEL_LABELS["attention"] in page.status_label.text()
    assert page.auth_warning.isVisibleTo(page)
    assert "перелогиньте Claude/Codex на worker-01" in page.auth_warning.text()
    assert "OWNER_NOTIFICATIONS.md" in page.auth_warning.text()
    assert "dead_letter_growth" in page.monitor_label.text()
    assert page.escalations_table.rowCount() == 1
    assert page.escalations_table.item(0, 0).text() == "VOYN-1"
    assert page.escalations_table.item(0, 2).text() == "P1"


def test_down_state_without_lanes(qtbot):
    page = _page(qtbot)
    page.render(_snap(_health(lanes=())))
    assert page.level is HealthLevel.DOWN
    assert page.status_label.text() == i18n.PIPELINE_LEVEL_LABELS["down"]
    assert page.metric_values["active_lanes"].text() == "0"


def test_unreachable_state_hides_data(qtbot):
    page = _page(qtbot)
    page.render(_snap(_health()))
    page.render(RemoteSnapshot(FetchOutcome.UNREACHABLE))
    assert page.level is None
    assert page.status_label.text() == i18n.PIPELINE_OUTCOME_MESSAGES["unreachable"]
    assert not page.metrics_group.isVisibleTo(page)
    assert not page.auth_warning.isVisibleTo(page)


def test_unauthorized_and_degraded_messages(qtbot):
    page = _page(qtbot)
    page.render(RemoteSnapshot(FetchOutcome.UNAUTHORIZED, http_status=401))
    assert page.status_label.text() == i18n.PIPELINE_OUTCOME_MESSAGES["unauthorized"]
    page.render(_snap(_health(), outcome=FetchOutcome.DEGRADED, freshness="degraded"))
    assert i18n.PIPELINE_OUTCOME_MESSAGES["degraded"] in page.status_label.text()
    assert page.metrics_group.isVisibleTo(page)  # last data still shown
    assert i18n.PIPELINE_FRESHNESS_LABELS["degraded"] in page.freshness_label.text()


def test_refresh_runs_off_the_gui_thread_and_renders(qtbot):
    import threading

    seen: list[object] = []
    gui = threading.get_ident()

    def fetch(config):
        seen.append((config, threading.get_ident()))
        return _snap(_health(lanes=()))

    page = _page(qtbot, fetch=fetch)
    runnable = page.refresh()
    with qtbot.waitSignal(runnable.signals.finished, timeout=5000):
        pass
    qtbot.waitUntil(lambda: page.level is HealthLevel.DOWN, timeout=5000)
    assert seen[0][0] is CONFIG
    assert seen[0][1] != gui


def test_fetch_exception_maps_to_invalid_response(qtbot):
    def fetch(_config):
        raise RuntimeError("boom")

    page = _page(qtbot, fetch=fetch)
    page.refresh()
    qtbot.waitUntil(
        lambda: page.status_label.text()
        == i18n.PIPELINE_OUTCOME_MESSAGES["invalid_response"],
        timeout=5000,
    )


def test_auto_refresh_timer_runs_only_while_visible(qtbot):
    page = _page(qtbot)
    assert page.timer.interval() == 60_000
    assert not page.timer.isActive()
    page.show()
    assert page.timer.isActive()
    page.hide()
    assert not page.timer.isActive()
    page.show()
    page.shutdown_workers()
    assert not page.timer.isActive()


def test_shell_unconfigured_pipeline_page(shell, qtbot):
    shell.navigate_to("pipeline")
    page = shell._pages["pipeline"]
    assert shell.current_section_key == "pipeline"
    qtbot.waitUntil(
        lambda: page.status_label.text()
        == i18n.PIPELINE_OUTCOME_MESSAGES["not_configured"],
        timeout=5000,
    )


def test_remote_settings_round_trip(qtbot, qapp, settings_store, settings_file):
    from command_center.desktop.app import build_shell

    shell, _ = build_shell(qapp, settings_store)
    qtbot.addWidget(shell)
    form = shell._settings_page.form
    assert form.remote_token_edit.echoMode() == form.remote_token_edit.EchoMode.Password
    form.remote_url_edit.setText(" https://gw.example:8443 ")
    form.remote_token_edit.setText("tok-secret")
    form.remote_pin_edit.setText("")
    form.save_remote_button.click()
    assert shell._remote_config() == GatewayConfig("https://gw.example:8443", "tok-secret", None)
    shell.shutdown()

    reopened = SettingsStore(QSettings(str(settings_file), QSettings.IniFormat))
    assert reopened.remote_gateway() == ("https://gw.example:8443", "tok-secret", None)

    second, _ = build_shell(qapp, reopened)
    qtbot.addWidget(second)
    second_form = second._settings_page.form
    assert second_form.remote_url_edit.text() == "https://gw.example:8443"
    assert second_form.remote_token_edit.text() == "tok-secret"

    reopened.set_remote_gateway(None, None, None)
    assert reopened.remote_gateway() == (None, None, None)
