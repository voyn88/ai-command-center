"""Unit tests for the priority-ordering invariant (VOYN-W2-TASKS).

The acceptance criterion of this task: *reordering never breaks dependency
invariants*. These tests pin the single domain rule that guarantees it —
``command_center.task_ordering.validated_order`` and the ``reorder``/``move``
primitives built on it — with no Streamlit and no store:

* a valid order (dependencies ahead of dependents) is preserved;
* an order that places a task above a task it depends on is rejected;
* a set containing a dependency cycle is rejected (cycle detection intact);
* cross-project (out-of-set) dependencies do not constrain the local order;
* the up/down ``move`` control rejects an illegal nudge and keeps the old order.
"""

from __future__ import annotations

import pytest

from command_center import task_ordering
from command_center.task_ordering import ReorderError


def _task(task_id: str, *, depends_on: list[str] | None = None, **extra: object) -> dict:
    return {"id": task_id, "title": task_id, "depends_on": depends_on or [], **extra}


def _by_id(*tasks: dict) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}


# --------------------------------------------------------------------------
# validated_order — the invariant
# --------------------------------------------------------------------------


def test_valid_order_with_dependency_ahead_is_preserved():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    order = ["A", "B"]
    assert task_ordering.validated_order(order, _by_id(a, b)) == ["A", "B"]


def test_order_that_ranks_a_task_above_its_dependency_is_rejected():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    # B depends on A, so B above A is illegal.
    with pytest.raises(ReorderError) as exc:
        task_ordering.validated_order(["B", "A"], _by_id(a, b))
    assert exc.value.code == task_ordering.CODE_DEPENDENCY_ORDER
    # The message names the dependency the operator must respect.
    assert "A" in exc.value.message


def test_transitive_chain_must_stay_in_dependency_order():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    c = _task("C", depends_on=["B"])
    by_id = _by_id(a, b, c)
    assert task_ordering.validated_order(["A", "B", "C"], by_id) == ["A", "B", "C"]
    with pytest.raises(ReorderError):
        # C ahead of B breaks the chain even though A..C order looks partly sorted.
        task_ordering.validated_order(["A", "C", "B"], by_id)


def test_a_cycle_is_detected_and_rejected():
    a = _task("A", depends_on=["B"])
    b = _task("B", depends_on=["A"])
    with pytest.raises(ReorderError) as exc:
        task_ordering.validated_order(["A", "B"], _by_id(a, b))
    assert exc.value.code == task_ordering.CODE_CYCLE


def test_find_cycle_reports_a_path_and_none_for_a_dag():
    a = _task("A", depends_on=["B"])
    b = _task("B", depends_on=["C"])
    c = _task("C", depends_on=["A"])
    cycle = task_ordering.find_cycle([a, b, c])
    assert cycle is not None
    # The reported path closes on itself.
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"A", "B", "C"}

    d = _task("D")
    e = _task("E", depends_on=["D"])
    assert task_ordering.find_cycle([d, e]) is None


def test_cross_project_dependency_does_not_constrain_the_local_order():
    # T depends on OUT, which is not part of this project's ordering set.
    t = _task("T", depends_on=["OUT"])
    other = _task("S")
    # OUT absent from the order set -> it cannot pin T's local position.
    assert task_ordering.validated_order(["T", "S"], _by_id(t, other)) == ["T", "S"]
    assert task_ordering.validated_order(["S", "T"], _by_id(t, other)) == ["S", "T"]


def test_unknown_and_duplicate_ids_are_rejected():
    a = _task("A")
    with pytest.raises(ReorderError) as unknown:
        task_ordering.validated_order(["A", "ghost"], _by_id(a))
    assert unknown.value.code == task_ordering.CODE_UNKNOWN_TASK
    with pytest.raises(ReorderError) as dup:
        task_ordering.validated_order(["A", "A"], _by_id(a))
    assert dup.value.code == task_ordering.CODE_DUPLICATE


# --------------------------------------------------------------------------
# reorder / move primitives
# --------------------------------------------------------------------------


def test_reorder_moves_a_task_when_the_result_is_legal():
    a, b, c = _task("A"), _task("B"), _task("C")
    # No dependencies -> any order is legal.
    assert task_ordering.reorder(["A", "B", "C"], "C", 0, _by_id(a, b, c)) == [
        "C", "A", "B",
    ]


def test_reorder_rejects_a_move_that_would_out_rank_a_dependency():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    # Moving B to the top would place it above A.
    with pytest.raises(ReorderError):
        task_ordering.reorder(["A", "B"], "B", 0, _by_id(a, b))


def test_move_up_is_rejected_when_it_crosses_a_dependency():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    with pytest.raises(ReorderError):
        task_ordering.move(["A", "B"], "B", -1, _by_id(a, b))


def test_move_down_past_a_dependent_is_rejected():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    # A moving below B would leave B (its dependent) ranked above it.
    with pytest.raises(ReorderError):
        task_ordering.move(["A", "B"], "A", +1, _by_id(a, b))


def test_reorder_clamps_out_of_range_target_without_error():
    a, b = _task("A"), _task("B")
    assert task_ordering.reorder(["A", "B"], "A", 99, _by_id(a, b)) == ["B", "A"]


# --------------------------------------------------------------------------
# default_order — healing a persisted order
# --------------------------------------------------------------------------


def test_default_order_follows_persisted_rank():
    a = _task("A", priority_rank=2)
    b = _task("B", priority_rank=0)
    c = _task("C", priority_rank=1)
    assert task_ordering.default_order([a, b, c]) == ["B", "C", "A"]


def test_default_order_heals_a_rank_that_violates_dependencies():
    # Persisted rank puts the dependent B ahead of its dependency A (drifted).
    a = _task("A", depends_on=[], priority_rank=1)
    b = _task("B", depends_on=["A"], priority_rank=0)
    healed = task_ordering.default_order([a, b])
    # Healed to a legal, dependency-respecting order.
    assert healed == ["A", "B"]
    task_ordering.validated_order(healed, _by_id(a, b))  # does not raise


def test_unranked_tasks_sink_to_the_end_in_stable_order():
    a = _task("A")  # no rank
    b = _task("B", priority_rank=0)
    c = _task("C")  # no rank
    assert task_ordering.default_order([a, b, c]) == ["B", "A", "C"]
