"""Coverage for the Home dashboard (`command_center.ui.home_dashboard` +
`app.py`'s `render_home_dashboard`).

Three layers, mirroring `test_workspace_home_ui.py`/`test_execution_center_ui.py`:

1. Pure-function unit tests for the small presentation helpers in
   `command_center.ui.home_dashboard` that build HTML strings with no
   Streamlit call inside them (`_esc`, `_bar`, `_sparkline`, `_pal`) — these
   run with a plain `import`, no `AppTest` needed.
2. `AppTest.from_function` tests that render each public component of the
   module in isolation (KPI tiles, queue rows, kanban overview, health gauge,
   metric list, supervisor status) and assert on the emitted markdown.
3. `AppTest.from_file` tests that drive the real `app.py` page (`nav_page =
   "dashboard"`) end to end: empty state, KPI/kanban counts computed from real
   tasks, quick-action navigation, and — the point of the "no fabricated
   numbers" requirement — a genuinely running fake-Claude session so the
   queue's percentage is the real `live_progress`, not a mock value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from command_center import models, tasks_repository
from command_center.runtime import db as runtime_db
from command_center.ui import home_dashboard

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


# --------------------------------------------------------------------------
# 1. Pure helper unit tests — no Streamlit runtime involved
# --------------------------------------------------------------------------


def test_esc_escapes_html_and_renders_missing_as_em_dash():
    assert home_dashboard._esc(None) == "—"
    assert home_dashboard._esc("<script>") == "&lt;script&gt;"
    assert home_dashboard._esc(42) == "42"


def test_bar_clamps_percentage_into_0_100_range():
    assert "width:0%" in home_dashboard._bar(-5, "green")
    assert "width:100%" in home_dashboard._bar(150, "green")
    assert "width:57%" in home_dashboard._bar(57, "green")
    assert "var(--hx-green)" in home_dashboard._bar(50, "green")


def test_sparkline_empty_for_fewer_than_two_points():
    assert home_dashboard._sparkline((), "blue") == ""
    assert home_dashboard._sparkline((5,), "blue") == ""


def test_sparkline_draws_a_polyline_matching_series_length():
    series = (1, 4, 2, 9, 3)
    svg = home_dashboard._sparkline(series, "blue")
    assert svg.startswith("<svg")
    assert "polyline" in svg
    # One coordinate pair per data point.
    points_attr = svg.split("points='")[1].split("'")[0]
    assert len(points_attr.split()) == len(series)


def test_pal_dark_and_light_tokens_differ_and_share_keys():
    dark = home_dashboard._pal(True)
    light = home_dashboard._pal(False)
    assert dark.keys() == light.keys()
    assert dark["bg"] != light["bg"]
    assert dark["text"] != light["text"]


# --------------------------------------------------------------------------
# 2. Component render tests via AppTest.from_function
# --------------------------------------------------------------------------


def _component_script() -> None:
    """Re-exec'd as a standalone script by AppTest — imports must be local,
    the script sees none of this module's top-level names."""
    from command_center.ui import home_dashboard as hd

    hd.kpi_tiles([
        hd.Kpi("Проекты", 3, "все активны", "📁", "violet", (1, 2, 3, 4)),
        hd.Kpi("Ревью", 0, "0 ожидают", "★", "amber"),
    ])
    hd.card_open("Очередь выполнения", "Все")
    hd.queue_rows([
        {"icon": "⚙", "name": "Реализовать X", "meta": "AIOS", "pct": 42,
         "accent": "green", "status": "Running", "status_accent": "green"},
        {"icon": "⚙", "name": "Без прогресса", "meta": "BANK", "pct": None,
         "accent": "indigo", "status": "Waiting", "status_accent": "amber"},
    ])
    hd.queue_footer(total=5, running=1, completed=3, failed=1)
    hd.card_close()
    hd.simple_rows([
        {"icon": "▪", "name": "AIOS", "meta": "2 активных", "right": "OK", "right_accent": "green"},
        {"icon": "▪", "name": "Без статуса", "meta": ""},
    ])
    hd.kanban_overview([("Backlog", 4, "slate"), ("Done", 2, "green")])
    hd.health_gauge(150, "Отлично")  # deliberately out of range to prove clamping
    hd.metric_list([("Задачи завершены", 80, "green")])
    hd.supervisor_status(94, "Автопилот включён")


def _run_component_script() -> AppTest:
    return AppTest.from_function(_component_script, default_timeout=30).run()


def _delivery_component_script() -> None:
    from command_center import dashboard_truth as truth
    from command_center.ui import home_dashboard as hd

    hd.delivery_rows(
        [
            {
                "run_id": f"run-{index}",
                "state": state,
                "label": label,
                "description": "Проверяемое описание",
                "accent": "slate",
                "semantic_role": "status",
            }
            for index, (state, label) in enumerate(
                [
                    (truth.UNKNOWN, "Доказательства неизвестны"),
                    (truth.UNACCEPTED, "Кандидат не принят"),
                    (truth.ACCEPTED_UNDEPLOYED, "Принят, deploy неизвестен"),
                    (truth.DEPLOYED, "Deploy подтверждён"),
                    (truth.STALE_RUNTIME, "Runtime устарел"),
                    (truth.RUNTIME_MISMATCH, "Runtime SHA не совпадает"),
                ]
            )
        ],
        window_label="Последние 6 из 6 запусков (лимит окна: 200)",
    )


def test_kpi_tiles_render_labels_values_and_sparkline():
    at = _run_component_script()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert "Проекты" in body and ">3<" in body
    assert "Ревью" in body and ">0<" in body
    assert "<svg" in body  # sparkline drawn for the non-empty series


def test_queue_rows_show_real_percentage_or_em_dash_when_absent():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "42%" in body
    assert "Реализовать X" in body
    assert "<div class='hx-pct'>—</div>" in body  # no progress -> em dash, not a fake number
    assert "Waiting" in body


def test_queue_footer_shows_real_counts():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "Всего запусков (runtime.db): <b style='color:var(--hx-text)'>5</b>" in body
    assert "В работе в окне: 1" in body
    assert "Завершено в окне: 3" in body
    assert "Ошибки в окне: 1" in body


def test_simple_rows_render_with_and_without_right_status():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "AIOS" in body and "2 активных" in body and "OK" in body
    assert "Без статуса" in body


def test_kanban_overview_renders_each_column_count():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "Backlog" in body and "<div class='hx-col-num'>4</div>" in body
    assert "Done" in body and "<div class='hx-col-num'>2</div>" in body


def test_health_gauge_clamps_percent_and_shows_label():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert ">100%<" in body  # clamped from 150
    assert "Отлично" in body


def test_metric_list_and_supervisor_status_render():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "Задачи завершены" in body and "80%" in body
    assert "AI Supervisor" in body and "Автопилот включён" in body and "94%" in body


def test_dashboard_markup_exposes_status_text_progress_names_and_accessible_graphics():
    at = _run_component_script()
    body = "".join(m.value for m in at.markdown)
    assert "role='status'" in body
    assert "aria-hidden='true'" in body
    assert "role='progressbar'" in body
    assert "aria-valuenow='42'" in body
    assert "role='img'" in body
    assert "aria-label='Здоровье проекта: 100%, Отлично'" in body


def test_delivery_markup_visibly_and_semantically_names_all_truth_states():
    at = AppTest.from_function(_delivery_component_script, default_timeout=30).run()
    body = "".join(m.value for m in at.markdown)
    for text in (
        "Доказательства неизвестны",
        "Кандидат не принят",
        "Принят, deploy неизвестен",
        "Deploy подтверждён",
        "Runtime устарел",
        "Runtime SHA не совпадает",
    ):
        assert text in body
    assert body.count("role='status'") == 6


def _light_css_script() -> None:
    from command_center.ui import home_dashboard as hd

    hd.theme.current_theme_type = lambda: "light"
    hd.inject_css()


def test_dashboard_css_has_visible_focus_reflow_and_theme_specific_contrast_tokens():
    at = AppTest.from_function(_light_css_script, default_timeout=30).run()
    body = "".join(m.value for m in at.markdown)
    assert ":focus-visible" in body
    assert "outline:3pxsolid" in body.replace(" ", "")
    assert "@media (max-width: 420px)" in body
    assert "--hx-green: #15803d" in body
    assert '[data-testid="stCaptionContainer"] p' in body
    assert 'kbd[aria-label^="Shortcut"]' in body
    assert 'a[aria-label="Link to heading"]' in body
    compact = body.replace(" ", "")
    assert "opacity:1!important" in compact
    assert "min-width:24px" in compact
    assert "min-height:24px" in compact


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_dashboard_status_tokens_meet_wcag_aa_normal_text_contrast():
    assert all(
        _contrast_ratio(color, "#141b2e") >= 4.5
        for color in home_dashboard._DARK_ACCENTS.values()
    )
    assert all(
        _contrast_ratio(color, "#ffffff") >= 4.5
        for color in home_dashboard._LIGHT_ACCENTS.values()
    )


# --------------------------------------------------------------------------
# 3. Full page tests via AppTest.from_file (real app.py, nav_page="dashboard")
# --------------------------------------------------------------------------


def _at_on_dashboard(section: str | None = None) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "dashboard"
    if section is not None:
        at.session_state["dashboard_section"] = section
    at.run()
    return at


def _task(task_id, title, *, status="Backlog", project="AIOS", launch_status=None, created_at="2026-01-01"):
    return {
        "id": task_id,
        "title": title,
        "goal": title,
        "status": status,
        "project": project,
        "launch_status": launch_status,
        "created_at": created_at,
    }


def test_dashboard_page_renders_and_nav_entry_exists():
    at = _at_on_dashboard()
    assert not at.exception
    assert any(b.key == "nav_btn_dashboard" for b in at.sidebar.button)
    body = " ".join(m.value for m in at.markdown)
    assert any(
        greeting in body for greeting in ("Доброе утро", "Добрый день", "Добрый вечер")
    )


def test_dashboard_empty_state_shows_no_fabricated_activity():
    at = _at_on_dashboard()
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("Сейчас ничего не выполняется" in c for c in captions)
    body = "".join(m.value for m in at.markdown)
    assert "Нет активных проектов" in body
    assert "Нет активных прогонов" in body


def test_dashboard_kpis_and_kanban_reflect_real_tasks(isolated_data_dir):
    root = isolated_data_dir
    tasks_repository.save_tasks(
        root,
        [
            _task("t1", "Задача 1", status="Backlog"),
            _task("t2", "Задача 2", status="In Progress"),
            _task("t3", "Задача 3", status="Done"),
            _task("t4", "Задача 4", status="Review", launch_status="Needs Review"),
        ],
    )

    at = _at_on_dashboard()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)

    # KPI "Задачи" is the canonical total (4), while "Ревью" is the Review
    # planning lane (1); neither silently changes meaning between widgets.
    assert (
        "<div class='hx-kpi-label'>Задачи</div>"
        "<div class='hx-kpi-value'>4</div>"
    ) in body
    assert (
        "<div class='hx-kpi-label'>Ревью</div>"
        "<div class='hx-kpi-value'>1</div>"
    ) in body

    # Kanban overview shows real per-status counts, not zeros/placeholders.
    for status in models.KANBAN_STATUSES:
        assert status in body
    assert "<div class='hx-col-num'>1</div>" in body  # Backlog/In Progress/Review/Done each have exactly 1


def test_dashboard_snapshot_surfaces_blocked_and_unknown_status_counts(
    isolated_data_dir,
):
    tasks_repository.save_tasks(
        isolated_data_dir,
        [
            _task("active", "Active", status="Backlog"),
            _task("blocked", "Blocked legacy row", status="Blocked"),
            _task("done", "Done", status="Done"),
            _task("unknown", "Unknown", status="Custom State"),
        ],
    )

    at = _at_on_dashboard()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert (
        "<div class='hx-kpi-label'>Задачи</div>"
        "<div class='hx-kpi-value'>4</div>"
    ) in body
    assert "<div class='hx-col-name'>Blocked</div><div class='hx-col-num'>1</div>" in body
    assert "<div class='hx-col-name'>Другие</div><div class='hx-col-num'>1</div>" in body


def test_dashboard_analytics_and_executive_use_the_same_task_snapshot(
    isolated_data_dir,
):
    tasks_repository.save_tasks(
        isolated_data_dir,
        [
            _task("active", "Active", status="Backlog"),
            _task("blocked", "Blocked", status="Blocked"),
            _task("done", "Done", status="Done"),
            _task("unknown", "Unknown", status="Custom State"),
        ],
    )

    analytics = _at_on_dashboard("Аналитика")
    assert not analytics.exception
    analytics_metrics = {metric.label: metric.value for metric in analytics.metric}
    assert analytics_metrics["Всего задач"] == "4"
    assert analytics_metrics["Активные"] == "1"
    assert analytics_metrics["В статусе Blocked"] == "1"
    assert analytics_metrics["Выполнено"] == "1 (25%)"
    assert analytics_metrics["Другие статусы"] == "1"

    executive = AppTest.from_file(APP_PATH, default_timeout=30)
    executive.session_state["nav_page"] = "executive"
    executive.run()
    assert not executive.exception
    executive_metrics = {metric.label: metric.value for metric in executive.metric}
    assert executive_metrics["Всего задач"] == "4"
    assert executive_metrics["Активные"] == "1"
    assert executive_metrics["В статусе Blocked"] == "1"
    assert executive_metrics["Выполнено"] == "1 (25%)"
    assert executive_metrics["Другие статусы"] == "1"


def test_dashboard_quick_action_navigates_to_create_page():
    at = _at_on_dashboard()
    at = at.button(key="home_qa_create").click().run()
    assert at.session_state["nav_page"] == "create"


def test_dashboard_open_execution_center_button_navigates():
    at = _at_on_dashboard()
    at = at.button(key="home_open_exec").click().run()
    assert at.session_state["nav_page"] == "execution_center"


def test_dashboard_localization_is_russian():
    at = _at_on_dashboard()
    body = "".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    for label in (
            "Проекты", "Живые прогоны", "Задачи", "Ревью",
        "Очередь выполнения", "Здоровье проекта", "Обзор Kanban",
        "Быстрые действия", "Активные агенты", "AI Supervisor",
    ):
        assert label in body


def _wait_for_report(db_path, run_id: str, *, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime_db.get_report(db_path, run_id) is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"run {run_id!r} did not finish in the background within {timeout}s")


@pytest.mark.serial  # real subprocess + DB running-then-completed timing; flaky when xdist saturates all cores
def test_dashboard_queue_and_footer_reflect_a_genuinely_running_then_completed_session(
    git_repo, configure_project_repo, fake_claude
):
    """The queue row and its footer counts must come from a genuinely running
    fake-Claude session's real state — never a fixed mock number: while the
    process is alive the run shows up in the queue at all (not fabricated
    into existence), and once it actually finishes the footer's "Завершено"
    count increases by exactly one, matching the real, observed run."""
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "2"
    configure_project_repo("AIOS", git_repo)

    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "execution_center"
    at.run()
    at.selectbox(key="exec_center_launch_project").select("AIOS").run()
    at.selectbox(key="exec_center_launch_task_type").select("review").run()
    at.text_area(key="exec_center_launch_instruction").set_value("do the thing").run()
    at.checkbox(key="exec_center_launch_confirm").check().run()
    at = at.button(key="exec_center_launch_btn").click().run()
    assert not at.exception

    run_id = runtime_db.list_runs(runtime_db.resolve_db_path(), limit=1)[0]["id"]

    # Same AppTest session's cached Supervisor/singleton — switch to Home
    # while the fake process is still alive (2s sleep).
    at.session_state["nav_page"] = "dashboard"
    at = at.run()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert "hx-row" in body  # the live session shows up as a real queue row
    assert "Всего запусков (runtime.db): <b style='color:var(--hx-text)'>1</b>" in body
    assert "Завершено в окне: 0" in body  # not finished yet — no fabricated completion

    _wait_for_report(runtime_db.resolve_db_path(), run_id)

    at.session_state["nav_page"] = "dashboard"
    at = at.run()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert "Завершено в окне: 1" in body  # now real, observed, exactly the one run
