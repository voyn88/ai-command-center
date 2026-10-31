"""Tests for command_center.bench.cases."""

from __future__ import annotations

import pytest

from command_center.bench.cases import CASES, CASES_BY_ID
from command_center.bench.types import CATEGORIES, BenchCase


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_category_is_represented():
    categories_present = {case.category for case in CASES}
    assert categories_present == set(CATEGORIES)


def test_every_category_has_at_least_three_cases():
    for category in CATEGORIES:
        count = sum(1 for case in CASES if case.category == category)
        assert count >= 3, f"{category} has only {count} case(s)"


def test_every_case_has_a_nonempty_rubric():
    for case in CASES:
        assert case.rubric
        assert all(isinstance(item, str) and item for item in case.rubric)


def test_cases_by_id_matches_cases():
    assert set(CASES_BY_ID) == {case.id for case in CASES}
    for case_id, case in CASES_BY_ID.items():
        assert case.id == case_id


def test_severity_five_cases_exist_for_critical_and_security():
    for category in ("critical", "security"):
        severities = [c.severity for c in CASES if c.category == category]
        assert max(severities) == 5


@pytest.mark.parametrize("category", ["not-a-real-category", "", "Critical"])
def test_unknown_category_rejected(category):
    with pytest.raises(ValueError):
        BenchCase(
            id="x",
            category=category,
            title="t",
            prompt="p",
            severity=1,
            rubric=("r",),
        )


def test_severity_out_of_range_rejected():
    with pytest.raises(ValueError):
        BenchCase(id="x", category="code", title="t", prompt="p", severity=0, rubric=("r",))
    with pytest.raises(ValueError):
        BenchCase(id="x", category="code", title="t", prompt="p", severity=6, rubric=("r",))


def test_empty_rubric_rejected():
    with pytest.raises(ValueError):
        BenchCase(id="x", category="code", title="t", prompt="p", severity=1, rubric=())
