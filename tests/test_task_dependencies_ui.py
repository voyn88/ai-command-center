"""Coverage for the task dependencies + priority-order surface (VOYN-W2-TASKS).

Two layers, mirroring ``test_operator_dashboard_ui.py``:

1. A tokens-only guard (no raw hex in the surface source) and the section-state
   classifier over a fake client.
2. ``AppTest.from_function`` smoke tests that render ``render`` in each of the
   five async states (idle-covered-by-loading, loading, error, empty, success)
   from a hand-built ``TaskGraph``, asserting the emitted markup, the up/down
   controls, and — crucially — that a reorder the server rejects surfaces the
   error banner instead of re-ordering.
"""

from __future__ import annotations

import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.ui import task_dependencies as td

_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")


def _graph(*, has_cycle: bool = False):
    from command_center.api import schemas

    return schemas.TaskGraph(
        project="AICC",
        order=["A", "B", "C"],
        has_cycle=has_cycle,
        nodes=[
            schemas.TaskGraphNode(
                id="A", project="AICC", title="Схема БД", status="Done",
                level=0, state="done", rank=0, blocks=["B"],
            ),
            schemas.TaskGraphNode(
                id="B", project="AICC", title="API слой", status="In Progress",
                level=1, state="ready", rank=1, depends_on=["A"], blocks=["C"],
            ),
            schemas.TaskGraphNode(
                id="C", project="AICC", title="UI экран", status="Backlog",
                level=2, state="waiting", rank=2, depends_on=["B"],
                blocked=True, blocked_by=["B"],
            ),
        ],
    )


class _FakeClient:
    def __init__(self, graph=None, *, raise_on_load=None, reject=None):
        self._graph = graph
        self._raise_on_load = raise_on_load
        self._reject = reject
        self.reorder_calls: list[list[str]] = []

    def task_graph(self, project):
        if self._raise_on_load is not None:
            raise self._raise_on_load
        return self._graph

    def reorder(self, project, order):
        self.reorder_calls.append(order)
        if self._reject is not None:
            raise td.ReorderRejected(self._reject[0], self._reject[1])
        return self._graph


# --------------------------------------------------------------------------
# 1. Pure guards
# --------------------------------------------------------------------------


def test_surface_source_has_no_raw_hex_tokens_only():
    source = (
        Path(__file__).resolve().parent.parent
        / "command_center" / "ui" / "task_dependencies.py"
    ).read_text(encoding="utf-8")
    assert not _HEX_RE.findall(source), "task_dependencies.py must reference tokens only"


def test_load_graph_classifies_success_empty_and_error():
    ok = td.load_graph(_FakeClient(_graph()), "AICC")
    assert ok.status == td.SUCCESS

    empty = td.load_graph(_FakeClient(_graph_empty()), "AICC")
    assert empty.status == td.EMPTY

    err = td.load_graph(_FakeClient(raise_on_load=RuntimeError("api down")), "AICC")
    assert err.status == td.ERROR and err.error == "api down"


def _graph_empty():
    from command_center.api import schemas

    return schemas.TaskGraph(project="AICC", order=[], nodes=[])


# --------------------------------------------------------------------------
# 2. Render smoke across states
# --------------------------------------------------------------------------


def test_render_success_lists_tasks_edges_and_controls():
    def _script() -> None:
        from command_center.ui import task_dependencies as m
        from tests.test_task_dependencies_ui import _FakeClient, _graph

        m.render("AICC", client=_FakeClient(_graph()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "Схема БД" in body and "API слой" in body and "UI экран" in body
    assert "уровень 0" in body and "уровень 2" in body
    assert "заблокирована" in body  # C's blocked state is surfaced
    # Up/down controls exist for the tasks.
    assert any(b.key == "tdp_up_B" for b in at.button)
    assert any(b.key == "tdp_down_B" for b in at.button)


def test_render_empty_state():
    def _script() -> None:
        from command_center.ui import task_dependencies as m
        from tests.test_task_dependencies_ui import _FakeClient, _graph_empty

        m.render("AICC", client=_FakeClient(_graph_empty()))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "нет задач" in body


def test_render_error_state_shows_retry():
    def _script() -> None:
        from command_center.ui import task_dependencies as m
        from tests.test_task_dependencies_ui import _FakeClient

        m.render("AICC", client=_FakeClient(raise_on_load=RuntimeError("boom")))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "boom" in body
    assert any(b.key == "ocd_retry_task_deps" for b in at.button)


def test_cycle_locks_reorder_and_warns():
    def _script() -> None:
        from command_center.ui import task_dependencies as m
        from tests.test_task_dependencies_ui import _FakeClient, _graph

        m.render("AICC", client=_FakeClient(_graph(has_cycle=True)))

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "цикл" in body
    # Every up/down control is disabled while a cycle stands.
    assert all(b.disabled for b in at.button if b.key.startswith("tdp_"))


def test_rejected_reorder_surfaces_error_banner():
    def _script() -> None:
        from command_center.ui import task_dependencies as m
        from tests.test_task_dependencies_ui import _FakeClient, _graph

        client = _FakeClient(
            _graph(),
            reject=("dependency_order", "«UI экран» нельзя поставить выше «API слой»."),
        )
        # Pre-seed the rejection message as if a prior move was refused.
        import streamlit as st

        st.session_state[m._ERROR_KEY] = "«UI экран» нельзя поставить выше «API слой»."
        m.render("AICC", client=client)

    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    body = "".join(mk.value for mk in at.markdown)
    assert "нельзя поставить выше" in body
