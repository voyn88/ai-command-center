"""Fleet page wiring: nav visibility under AICC_PG_* and read-only render."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import pytest

from command_center.db.fleet_read import filter_nav_for_postgres
from command_center.runtime import identity
from command_center.ui import fleet_panel, sidebar

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def _stable_process_identity(monkeypatch):
    """Hosts without ``/proc/.../boot_id`` cannot capture a live identity;
    pin a synthetic one so AppTest can construct the Session Supervisor."""
    fake = identity.ProcessIdentity(
        pid=1,
        start_time="procfs-boot:00000000-0000-0000-0000-000000000000:1",
        command="pytest",
    )
    monkeypatch.setattr(identity, "capture_identity", lambda pid, **kwargs: fake)


_NAV = {
    "dashboard": ("Обзор", ":material/dashboard:"),
    "execution_center": ("Live Execution Center", ":material/bolt:"),
    "fleet": ("Fleet", ":material/dns:"),
    "projects": ("Проекты", ":material/folder_open:"),
}


class _FakeStore:
    def tasks_by_status_wave(self):
        return [{"status": "OPEN", "wave": "0", "count": 2}]

    def queue_by_state(self, *, reason_limit=20):
        return {
            "counts": [{"state": "ready", "count": 1}],
            "dead_reasons": [],
            "attempt_reasons": [],
        }

    def recent_attempts_per_executor(self, *, per_executor=10, executor_limit=20):
        return [
            {
                "attempt_id": "att_1",
                "work_item_id": "wki_1",
                "attempt_no": 1,
                "claimed_by_role": "aicc_w_wrk_voyn_worker_01",
                "state": "succeeded",
                "outcome_reason": None,
                "created_at": "t",
                "updated_at": "t",
            }
        ]

    def open_monitor_findings(self, *, limit=100):
        return [
            {
                "finding_id": 1,
                "source": "worker-01",
                "failure": "deploy_lag",
                "task_id": "VOYN-MON-1",
                "opened_at": "t",
                "seen_at": "t",
            }
        ]

    def merged_prs_per_day(self, *, days=30):
        return [{"day": "2026-09-09", "merges": 3}]


def test_fleet_is_grouped_in_osnovnoe_when_present_in_nav() -> None:
    groups = {title: keys for title, keys, _ in sidebar._grouped(_NAV)}
    assert "fleet" in groups["Основное"]


def test_fleet_hidden_from_nav_without_pg() -> None:
    assert "fleet" not in filter_nav_for_postgres(_NAV, env={})
    assert "fleet" not in filter_nav_for_postgres(_NAV, env={"AICC_PG_HOST": ""})


def test_fleet_kept_in_nav_with_pg_host() -> None:
    assert "fleet" in filter_nav_for_postgres(
        _NAV, env={"AICC_PG_HOST": "127.0.0.1"}
    )


def test_fleet_panel_explains_missing_pg(monkeypatch) -> None:
    monkeypatch.delenv("AICC_PG_HOST", raising=False)

    def _script() -> None:
        from command_center.ui import fleet_panel as panel

        panel.render()

    at = AppTest.from_function(_script, default_timeout=10).run()
    assert not at.exception
    assert any("AICC_PG" in info.value for info in at.info)


def test_fleet_panel_renders_sections_from_store(monkeypatch) -> None:
    monkeypatch.setenv("AICC_PG_HOST", "127.0.0.1")
    monkeypatch.setattr(fleet_panel, "ensure_pool", lambda: None)

    def _script() -> None:
        from command_center.ui import fleet_panel as panel
        from tests.test_fleet_panel import _FakeStore

        panel.render(store=_FakeStore())

    at = AppTest.from_function(_script, default_timeout=10).run()
    assert not at.exception
    body = " ".join(item.value for item in at.markdown)
    assert "Задачи по статусу и волне" in body
    assert "Очередь по состоянию" in body
    assert "Недавние попытки по исполнителю" in body
    assert "Открытые monitor findings" in body
    assert "Смерженные PR по дням" in body


def test_fleet_page_route_renders_without_pg(
    monkeypatch, isolated_data_dir, _stable_process_identity
) -> None:
    monkeypatch.delenv("AICC_PG_HOST", raising=False)
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "fleet"
    at.run()
    assert not at.exception
    assert any("AICC_PG" in info.value for info in at.info)
    keys = {button.key for button in at.sidebar.button}
    assert "nav_btn_fleet" not in keys


def test_fleet_nav_button_appears_when_pg_host_set(
    monkeypatch, isolated_data_dir, _stable_process_identity
) -> None:
    monkeypatch.setenv("AICC_PG_HOST", "127.0.0.1")
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "dashboard"
    at.run()
    assert not at.exception
    keys = {button.key for button in at.sidebar.button}
    assert "nav_btn_fleet" in keys
