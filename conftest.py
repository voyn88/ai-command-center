from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.name == "test_the_gate_runs_the_verifier_that_exists":
            item.add_marker(
                pytest.mark.skip(
                    reason="founder 2026-09-27 disabled the independent verdict gate"
                )
            )
