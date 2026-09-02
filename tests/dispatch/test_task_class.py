"""Unit tests for the task_class generator (`dispatch.task_class`).

Every property is asserted directly against `task_class_for` with plain
strings — no database, no filesystem, matching the rest of `dispatch`.
"""

from __future__ import annotations

from command_center.dispatch.task_class import task_class_for


def test_composes_project_and_task_type():
    assert task_class_for(project="AICC", task_type="implementation") == (
        "AICC:implementation"
    )


def test_different_task_types_on_the_same_project_are_different_buckets():
    migration = task_class_for(project="AICC", task_type="review")
    frontend = task_class_for(project="AICC", task_type="implementation")
    assert migration != frontend


def test_same_task_type_on_different_projects_are_different_buckets():
    aicc = task_class_for(project="AICC", task_type="implementation")
    bank = task_class_for(project="BANK", task_type="implementation")
    assert aicc != bank


def test_missing_project_falls_back_to_a_sentinel_not_a_collision():
    bucket = task_class_for(project=None, task_type="implementation")
    assert bucket == "unassigned:implementation"


def test_missing_task_type_falls_back_to_a_sentinel_not_a_collision():
    bucket = task_class_for(project="AICC", task_type=None)
    assert bucket == "AICC:unspecified"


def test_blank_strings_are_treated_the_same_as_missing():
    assert task_class_for(project="  ", task_type="implementation") == (
        "unassigned:implementation"
    )
    assert task_class_for(project="AICC", task_type="") == "AICC:unspecified"


def test_surrounding_whitespace_is_trimmed():
    assert task_class_for(project=" AICC ", task_type=" review ") == (
        "AICC:review"
    )


def test_non_string_inputs_never_raise():
    assert task_class_for(project=123, task_type=None) == (  # type: ignore[arg-type]
        "unassigned:unspecified"
    )


def test_is_pure_and_deterministic():
    first = task_class_for(project="AICC", task_type="implementation")
    second = task_class_for(project="AICC", task_type="implementation")
    assert first == second
