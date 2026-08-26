"""A guard against the PostgreSQL suite silently skipping in CI.

Every test in `tests/db` skips when `AICC_TEST_PG_ADMIN_DSN` is unset, which is
what keeps `pytest -q` green on a laptop with no Docker. The same mechanism
means a renamed environment variable, or a service container that fails to come
up, would produce a fully green build with none of the database behaviour
actually verified — the failure mode where a gate reports success precisely
because it ran nothing.
"""

from __future__ import annotations

import os

import pytest

from tests.db.conftest import ADMIN_DSN_ENV


@pytest.mark.skipif(os.environ.get("CI") != "true", reason="only meaningful on CI")
def test_postgres_suite_is_not_silently_skipped_on_ci() -> None:
    assert os.environ.get(ADMIN_DSN_ENV), (
        f"{ADMIN_DSN_ENV} is unset on CI, so the entire tests/db suite would skip "
        "and report green. Check the PostgreSQL service container in "
        ".github/workflows/ci.yml."
    )
