"""API-boundary coverage for the task graph + reorder write (VOYN-W2-TASKS).

Drives the read (``service.task_graph``) and the write
(``wave1_service.reorder_tasks``) against a real, isolated tasks store (the
``AICC_DATA_DIR`` sandbox from ``conftest``), proving that:

* the graph read surfaces dependencies, blocked state and dependency levels;
* a legal reorder is persisted as ``priority_rank`` and reflected on re-read;
* a reorder that would out-rank a dependency is rejected and *nothing* is
  written (the persisted order is unchanged);
* a cyclic set is refused.
"""

from __future__ import annotations

import pytest

from command_center.api import service, wave1_service
from command_center.tasks_repository import create_task

ROOT = service.ROOT


def _make(project: str, title: str, *, depends_on=None, status="Backlog") -> dict:
    return create_task(
        ROOT, project=project, title=title, task_type="implementation",
        status=status, depends_on=depends_on or [],
    )


def test_task_graph_surfaces_dependencies_levels_and_blocked_state():
    a = _make("AICC", "Схема БД", status="Done")
    b = _make("AICC", "API слой", depends_on=[a["id"]])
    c = _make("AICC", "UI экран", depends_on=[b["id"]])

    graph = service.task_graph("AICC")
    by_id = {n.id: n for n in graph.nodes}
    assert set(by_id) == {a["id"], b["id"], c["id"]}
    assert by_id[a["id"]].level == 0
    assert by_id[c["id"]].level == 2
    assert by_id[b["id"]].depends_on == [a["id"]]
    # C is blocked: B (its dependency) is not Done.
    assert by_id[c["id"]].blocked is True
    assert by_id[c["id"]].blocked_by == [b["id"]]
    assert graph.has_cycle is False


def test_legal_reorder_persists_priority_rank():
    a = _make("AICC", "Independent A")
    b = _make("AICC", "Independent B")
    c = _make("AICC", "Independent C")
    # No dependencies -> any order is legal.
    new_order = [c["id"], a["id"], b["id"]]
    graph = wave1_service.reorder_tasks("AICC", new_order)
    assert graph.order == new_order
    # Re-read confirms the persisted rank round-trips.
    assert service.task_graph("AICC").order == new_order


def test_reorder_that_out_ranks_a_dependency_is_rejected_and_persists_nothing():
    a = _make("AICC", "Схема БД")
    b = _make("AICC", "API слой", depends_on=[a["id"]])
    before = service.task_graph("AICC").order

    with pytest.raises(wave1_service.TaskReorderRejected) as exc:
        # B above A violates the dependency invariant.
        wave1_service.reorder_tasks("AICC", [b["id"], a["id"]])
    assert exc.value.code == "dependency_order"
    # Nothing was written — the order is exactly what it was.
    assert service.task_graph("AICC").order == before


def test_reorder_of_a_cyclic_set_is_refused():
    a = _make("AICC", "A")
    b = _make("AICC", "B")
    # Introduce a cycle A<->B by editing depends_on through the repository.
    from command_center import tasks_repository

    def _mut(tasks):
        by = {t["id"]: t for t in tasks}
        by[a["id"]]["depends_on"] = [b["id"]]
        by[b["id"]]["depends_on"] = [a["id"]]

    tasks_repository.mutate_tasks(ROOT, _mut)

    with pytest.raises(wave1_service.TaskReorderRejected) as exc:
        wave1_service.reorder_tasks("AICC", [a["id"], b["id"]])
    assert exc.value.code == "dependency_cycle"


def test_reorder_of_a_sensitive_project_is_refused():
    with pytest.raises(wave1_service.TaskReorderRejected):
        wave1_service.reorder_tasks("BANK", [])
