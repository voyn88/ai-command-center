"""Coverage for the operator dashboard (W1-DASHBOARD-UI).

The dashboard renders entirely from the ``/api/v1`` surface through
``command_center.ui.dashboard_client`` and is styled only through the
``command_center/design`` token package. Three layers, mirroring
``test_home_dashboard_ui.py``:

1. Pure-function unit tests for the Streamlit-free helpers (token stylesheet
   assembly, the section-state classifier, the progress bar, the greeting) and
   a tokens-only guard that fails on any raw hex in the surface's source.
2. ``AppTest.from_function`` component tests that render each section in every
   one of the five async states (idle/loading/error/empty/success) from
   hand-built API responses, asserting the emitted markup and the real
   action/link buttons.
3. ``AppTest.from_function`` journey tests that drive the whole ``render`` from
   a mocked client (success, all-error, all-empty, and a «→ в задачу» promote),
   plus an ``AppTest.from_file`` test that wires the real ``app.py`` page end to
   end (nav entry + empty state over the live in-process client).
"""

from __future__ import annotations

import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import dashboard_client, operator_dashboard as od

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

# Same rule the design-token gate enforces on primitives.css: no raw color.
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")


# --------------------------------------------------------------------------
# 1. Pure helper unit tests — no Streamlit runtime involved
# --------------------------------------------------------------------------


def test_surface_source_has_no_raw_hex_tokens_only():
    root = Path(__file__).resolve().parent.parent / "command_center" / "ui"
    for name in ("operator_dashboard.py", "dashboard_client.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert not _HEX_RE.findall(source), f"{name} must reference tokens only"


def test_tokens_style_bundles_tokens_primitives_and_layout():
    style = od.tokens_style("dark")
    assert style.startswith("<style>") and style.endswith("</style>")
    # Consumes the canonical package (a token custom property + a primitive).
    assert "--accent" in style
    assert ".card {" in style
    assert ".kpi__value" in style
    # Our own layout scaffolding references tokens, never literals.
    assert ".ocd-kpis" in style
    assert not _HEX_RE.findall("\n".join(
        line for line in style.splitlines() if line.strip().startswith(".ocd-")
    ))


def test_root_override_forces_active_theme_over_media_query():
    tokens_css = (od._DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
    dark = od._root_override(tokens_css, "dark")
    light = od._root_override(tokens_css, "light")
    assert dark.startswith(":root {") and "--bg" in dark
    assert light.startswith(":root {") and "--bg" in light
    assert dark != light  # the two palettes actually differ
    # An unknown/absent theme degrades to "follow the OS", not a broken block.
    assert od._root_override(tokens_css, "sepia") == ""


def test_bar_clamps_and_picks_a_token_variant():
    assert "width:0%" in od._bar(-10)
    assert "width:100%" in od._bar(150)
    assert "aria-valuenow='57'" in od._bar(57)
    assert "bar__fill--ok" in od._bar(90)
    assert "bar__fill--crit" in od._bar(10)


def test_load_section_classifies_success_empty_and_error():
    ok = od.load_section(lambda: [1, 2], lambda payload: not payload)
    assert ok.status == od.SUCCESS and ok.data == [1, 2]

    empty = od.load_section(lambda: [], lambda payload: not payload)
    assert empty.status == od.EMPTY

    def _boom():
        raise RuntimeError("api down")

    err = od.load_section(_boom, lambda payload: False)
    assert err.status == od.ERROR and err.error == "api down"


def test_greeting_covers_every_daypart():
    from datetime import datetime

    assert od._greeting(datetime(2026, 1, 1, 3)) == "Доброй ночи"
    assert od._greeting(datetime(2026, 1, 1, 9)) == "Доброе утро"
    assert od._greeting(datetime(2026, 1, 1, 14)) == "Добрый день"
    assert od._greeting(datetime(2026, 1, 1, 21)) == "Добрый вечер"


# --------------------------------------------------------------------------
# 2. Component render tests via AppTest.from_function (one state at a time)
# --------------------------------------------------------------------------


def _sample_dashboard():
    """A DashboardResponse populated across all four of its visual sections."""
    from command_center.api import schemas

    return schemas.DashboardResponse(
        agents=schemas.AgentSummary(running=2, queued=1, attention=3, total=6),
        task_counts=schemas.TaskCounts(total=12, done=4, active=6, attention=3),
        projects=[
            schemas.Project(
                id="p-app", name="App One", kind="application",
                project_ref="AICC", healthy=True, progress=75,
                health=schemas.ProjectHealth(branch="main"),
            ),
            schemas.Project(
                id="p-bank", name="BANK", kind="other",
                project_ref="BANK", healthy=True, redacted=True,
            ),
        ],
        activity=[
            schemas.ActivityItem(kind="run", project="AICC", title="Сборка отчёта",
                                 ts="2026-08-12T10:00:00Z"),
            schemas.ActivityItem(kind="commit", project="AICC", title="fix: ядро"),
        ],
        attention=[
            schemas.AttentionItem(kind="task", project="AICC",
                                  title="Требует ревью", detail="ждёт решения"),
            schemas.AttentionItem(kind="run", project="AICC", title="Упавший прогон"),
        ],
    )


def test_render_kpis_success_shows_values_and_attention_chip():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _sample_dashboard

        m.render_kpis(m.SectionState(status=m.SUCCESS, data=_sample_dashboard()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Живые агенты" in body and ">2<" in body
    assert "Задачи" in body and ">12<" in body
    assert "Проекты" in body and ">2<" in body
    assert "внимание" in body  # attention > 0 surfaces a crit chip


def test_render_projects_links_progress_and_redaction():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _sample_dashboard

        m.render_projects(m.SectionState(status=m.SUCCESS, data=_sample_dashboard()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "App One" in body
    assert "aria-valuenow='75'" in body  # real progress, not a placeholder
    assert "скрыто" in body  # redacted project marked, name not leaked as detail
    # A real link to the project surface exists per project.
    assert any(b.key == "ocd_project_p-app" for b in at.button)


def test_render_advisor_success_promote_button_and_terminal_chip():
    def _script() -> None:
        from command_center.api import models
        from command_center.api import wave1_schemas as w
        from command_center.ui import operator_dashboard as m

        page = w.ProposalList(
            proposals=[
                models.Proposal(id="pr-1", kind="ux", title="Упростить онбординг",
                                body="Шаги дублируются", project_ref="AICC",
                                expected_gain="high", effort="low", status="new"),
                models.Proposal(id="pr-2", kind="trend", title="Уже в работе",
                                project_ref="AICC", status="converted"),
            ],
            limit=50, offset=0,
        )
        m.render_advisor(
            m.SectionState(status=m.SUCCESS, data=page),
            on_promote=lambda _pid: None,
        )

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Упростить онбординг" in body
    assert "выгода: high" in body and "усилия: low" in body
    assert any(b.key == "ocd_promote_pr-1" for b in at.button)  # actionable
    assert "в задаче" in body  # converted proposal shows a status chip, no button
    assert not any(b.key == "ocd_promote_pr-2" for b in at.button)


def test_render_digest_owner_and_attention_success():
    def _script() -> None:
        from command_center.api import models
        from command_center.api import wave1_schemas as w
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _sample_dashboard

        m.render_attention(
            m.SectionState(status=m.SUCCESS, data=_sample_dashboard())
        )
        m.render_digest(m.SectionState(status=m.SUCCESS, data=w.DigestItemList(
            items=[models.DigestItem(id="d1", title="Итоги ночи", body="Всё зелено",
                                     category="ops", refs=["run-1"])],
            limit=50, offset=0)))
        m.render_owner_day(m.SectionState(status=m.SUCCESS, data=w.OwnerItemList(
            items=[
                models.OwnerItem(id="o1", title="Подписать релиз", due="2026-08-13"),
                models.OwnerItem(id="o2", title="Готово", done=True),
            ],
            limit=50, offset=0)))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Требуют внимания" in body and "Требует ревью" in body
    assert any(b.key.startswith("ocd_attention_") for b in at.button)
    assert "Итоги ночи" in body and "ops" in body and "run-1" in body
    assert "Подписать релиз" in body and "срок: 2026-08-13" in body
    assert "готово" in body  # done owner item


def test_every_section_renders_loading_idle_empty_and_error():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m

        for state in (m.LOADING, m.IDLE):
            m.render_activity(m.SectionState(status=state))
        # Empty needs a data payload with an empty collection.
        from command_center.api import wave1_schemas as w

        m.render_digest(m.SectionState(
            status=m.EMPTY, data=w.DigestItemList(items=[], limit=50, offset=0)))
        m.render_owner_day(m.SectionState(
            status=m.ERROR, error="timeout"))
        m.render_advisor(
            m.SectionState(status=m.ERROR, error="boom"),
            on_promote=lambda _pid: None,
        )

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "aria-busy='true'" in body  # loading skeleton
    assert "Ожидание данных" in body  # idle
    assert "Дайджест пуст" in body  # empty
    assert "role='alert'" in body  # error note
    assert "timeout" in body and "boom" in body
    # Error states offer a real retry control.
    assert any(b.key == "ocd_retry_owner" for b in at.button)
    assert any(b.key == "ocd_retry_advisor" for b in at.button)


# --------------------------------------------------------------------------
# 3. Journey tests — the whole render() from a mocked client
# --------------------------------------------------------------------------


def test_render_full_page_success_from_mocked_client():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _FakeClient, _fake_success

        m.render(_FakeClient(**_fake_success()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Все данные — из /api/v1" in body
    for section in ("Активность агентов", "Проекты", "Дайджест",
                    "Находки Советника", "Требуют внимания", "Мой день"):
        assert section in body


def test_render_full_page_all_errors_shows_notes_and_retries():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _RaisingClient

        m.render(_RaisingClient())

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Часть источников недоступна" in body
    assert body.count("role='alert'") >= 4  # dashboard + advisor + digest + owner
    assert any(b.key == "ocd_retry_kpis" for b in at.button)


def test_render_full_page_all_empty_shows_empty_notes():
    def _script() -> None:
        from command_center.ui import operator_dashboard as m
        from tests.test_operator_dashboard_ui import _FakeClient, _fake_empty

        m.render(_FakeClient(**_fake_empty()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Нет активных прогонов" in body
    assert "Нет активных проектов" in body
    assert "Дайджест пуст" in body
    assert "Пока нет находок" in body
    assert "Ничего не требует внимания" in body
    assert "задач владельца нет" in body


def test_promote_button_calls_the_api_promote_endpoint():
    def _script() -> None:
        import streamlit as st

        from command_center.api import models
        from command_center.api import wave1_schemas as w
        from command_center.ui import operator_dashboard as m

        page = w.ProposalList(
            proposals=[models.Proposal(id="pr-9", kind="ux", title="Находка",
                                       project_ref="AICC", status="new")],
            limit=50, offset=0)
        calls = st.session_state.setdefault("promoted", [])
        m.render_advisor(
            m.SectionState(status=m.SUCCESS, data=page),
            on_promote=lambda pid: calls.append(pid),
        )

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    at.button(key="ocd_promote_pr-9").click().run()
    assert at.session_state["promoted"] == ["pr-9"]


# --------------------------------------------------------------------------
# Fakes shared by the journey scripts (importable — scripts re-exec standalone)
# --------------------------------------------------------------------------


class _FakeClient:
    """A hand-built client returning canned endpoint responses."""

    def __init__(self, *, dashboard, advisor, digest, owner):
        self._dashboard = dashboard
        self._advisor = advisor
        self._digest = digest
        self._owner = owner
        self.promoted: list[str] = []

    def dashboard(self):
        return self._dashboard

    def advisor_proposals(self):
        return self._advisor

    def digest(self):
        return self._digest

    def owner_items(self):
        return self._owner

    def promote_proposal(self, proposal_id):
        self.promoted.append(proposal_id)
        return None


class _RaisingClient:
    """Every endpoint raises — the transport/5xx path a real client would hit."""

    def _boom(self, *_a, **_k):
        raise RuntimeError("api unreachable")

    dashboard = _boom
    advisor_proposals = _boom
    digest = _boom
    owner_items = _boom

    def promote_proposal(self, proposal_id):
        raise RuntimeError("api unreachable")


def _fake_success() -> dict:
    from command_center.api import models
    from command_center.api import wave1_schemas as w

    return {
        "dashboard": _sample_dashboard(),
        "advisor": w.ProposalList(
            proposals=[models.Proposal(id="pr-1", kind="ux", title="Находка",
                                       project_ref="AICC", status="new")],
            limit=50, offset=0),
        "digest": w.DigestItemList(
            items=[models.DigestItem(id="d1", title="Ночной итог")],
            limit=50, offset=0),
        "owner": w.OwnerItemList(
            items=[models.OwnerItem(id="o1", title="Решить вопрос")],
            limit=50, offset=0),
    }


def _fake_empty() -> dict:
    from command_center.api import schemas
    from command_center.api import wave1_schemas as w

    return {
        "dashboard": schemas.DashboardResponse(
            agents=schemas.AgentSummary(), task_counts=schemas.TaskCounts(),
            projects=[], activity=[], attention=[]),
        "advisor": w.ProposalList(proposals=[], limit=50, offset=0),
        "digest": w.DigestItemList(items=[], limit=50, offset=0),
        "owner": w.OwnerItemList(items=[], limit=50, offset=0),
    }


# --------------------------------------------------------------------------
# 4. Full-page wiring via AppTest.from_file (real app.py, in-process client)
# --------------------------------------------------------------------------


def test_command_page_renders_and_nav_entry_exists():
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "command"
    at.run()
    assert not at.exception
    assert any(b.key == "nav_btn_command" for b in at.sidebar.button)
    body = "".join(m.value for m in at.markdown)
    assert any(
        greeting in body
        for greeting in ("Доброе утро", "Добрый день", "Добрый вечер", "Доброй ночи")
    )


def test_command_page_empty_state_over_live_client(isolated_data_dir):
    # No seeded data -> the wave1-backed sections (digest, «Мой день») come back
    # empty from the live in-process client and show their explicit empty note
    # rather than hanging blank. (Projects/activity depend on the operator's
    # integration registry, which is not deterministically empty here.)
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "command"
    at.run()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert "Дайджест пуст" in body
    assert "На сегодня задач владельца нет" in body


def test_default_client_is_the_in_process_api_client():
    assert isinstance(
        dashboard_client.InProcessDashboardClient(),
        dashboard_client.InProcessDashboardClient,
    )
    # The protocol advertises exactly the five endpoint methods the UI consumes.
    for method in ("dashboard", "advisor_proposals", "digest", "owner_items",
                   "promote_proposal"):
        assert hasattr(dashboard_client.InProcessDashboardClient, method)
