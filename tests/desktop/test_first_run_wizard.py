"""First-run wizard UI tests (D-1) — offscreen Qt, injected probes only."""

from __future__ import annotations

import os

import pytest

from command_center.application.first_run import (
    HealthCheckItem,
    HealthStatus,
    initialize_workspace,
)
from command_center.desktop import i18n
from command_center.desktop.pages.first_run_wizard import FirstRunWizard

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="first-run wizard journey is POSIX-gated (D-1)"
)


def _checks(*items: HealthCheckItem):
    return lambda: tuple(items)


GIT_OK = HealthCheckItem("git", "Git", HealthStatus.OK, "/usr/bin/git")
GH_WARN = HealthCheckItem(
    "gh",
    "GitHub CLI (gh)",
    HealthStatus.WARNING,
    "Установлен, вход не выполнен",
    "Выполните в терминале: `gh auth login`",
)
UV_MISSING = HealthCheckItem(
    "uv", "uv", HealthStatus.ERROR, "Не найден в PATH", "Установите uv"
)


@pytest.fixture
def wizard_factory(qtbot, settings_store, tmp_path):
    def make(checks=_checks(GIT_OK, GH_WARN, UV_MISSING), initialize=None):
        wizard = FirstRunWizard(
            settings_store,
            health_checks=checks,
            initialize=initialize
            or (lambda raw: initialize_workspace(raw, environ={})),
        )
        qtbot.addWidget(wizard)
        return wizard

    return make


def test_checklist_renders_every_item_with_status_and_hint(wizard_factory):
    wizard = wizard_factory()
    assert [row.item.check_id for row in wizard.check_rows] == ["git", "gh", "uv"]
    statuses = [row.status_label.text() for row in wizard.check_rows]
    assert statuses == [
        i18n.FIRST_RUN_STATUS_LABELS["ok"],
        i18n.FIRST_RUN_STATUS_LABELS["warning"],
        i18n.FIRST_RUN_STATUS_LABELS["error"],
    ]
    # A hard error surfaces the note; hints stay attached to their rows.
    assert wizard._errors_note.isVisibleTo(wizard)


def test_recheck_rebuilds_the_list_from_fresh_probe_results(wizard_factory):
    results = {"items": (UV_MISSING,)}
    wizard = wizard_factory(checks=lambda: results["items"])
    assert len(wizard.check_rows) == 1
    results["items"] = (GIT_OK, GH_WARN)
    wizard.recheck_button.click()
    assert [row.item.check_id for row in wizard.check_rows] == ["git", "gh"]
    assert not wizard._errors_note.isVisibleTo(wizard)


def test_invalid_workspace_root_shows_russian_error_and_keeps_dialog_open(
    wizard_factory,
):
    wizard = wizard_factory()
    wizard.workspace_edit.setText("relative/path")
    wizard.continue_button.click()
    assert wizard.result() != int(wizard.DialogCode.Accepted)
    assert wizard.validation_label.isVisibleTo(wizard)
    assert "абсолютным" in wizard.validation_label.text()


def test_valid_workspace_root_initializes_persists_and_accepts(
    wizard_factory, settings_store, tmp_path
):
    env: dict[str, str] = {}
    wizard = wizard_factory(
        initialize=lambda raw: initialize_workspace(raw, environ=env)
    )
    target = tmp_path / "workspace"
    wizard.workspace_edit.setText(str(target))
    wizard.continue_button.click()

    assert wizard.result() == int(wizard.DialogCode.Accepted)
    assert wizard.workspace_root == str(target.resolve())
    assert settings_store.workspace_root() == str(target.resolve())
    assert (target / "data").is_dir()
    assert env["AICC_DATA_DIR"] == str(target.resolve() / "data")


def test_initialize_failure_is_shown_inline_not_raised(wizard_factory, tmp_path):
    def failing(raw: str):
        raise OSError("Диск переполнен")

    wizard = wizard_factory(initialize=failing)
    wizard.workspace_edit.setText(str(tmp_path / "ws"))
    wizard.continue_button.click()
    assert wizard.result() != int(wizard.DialogCode.Accepted)
    assert "Диск переполнен" in wizard.validation_label.text()


def test_quit_rejects_without_writing_configuration(wizard_factory, settings_store):
    wizard = wizard_factory()
    wizard.quit_button.click()
    assert wizard.result() == int(wizard.DialogCode.Rejected)
    assert wizard.workspace_root is None
    assert settings_store.workspace_root() is None
