"""Shared fixtures/helpers for the chaos/concurrency suite (VOYN-W0-AICC-
CHAOS-CONCURRENCY-SUITE).

This directory collects the review -> adjudicate -> merge lifecycle's owner
scenarios into one explicit, by-name-recognizable suite instead of leaving
them scattered across tests/db/test_review_merge.py. Every fixture/helper
here is the exact one that suite already uses -- imported, not
reimplemented, so the chaos suite can never silently drift from what
production code actually does: `_ready`/`_done`/`_complete_review`/`_chain`
build task/PR/review state the same way a live tick would produce it, and
the `_snapshots`/`_test_repo_routes` autouse fixtures fake the same two
seams (`_pr_diff_and_head`, the planner's repo-route allowlist) the same
way. Importing an autouse fixture into this module registers it for every
test collected under tests/chaos/, exactly as test_review_merge.py already
does for `_test_repo_routes` (imported from tests.db.test_backlog_planner).

Every test in this package needs a real PostgreSQL server, supplied via
`AICC_TEST_PG_ADMIN_DSN` (see tests/db/conftest.py); with it unset, `rig`
skips and the whole suite skips with it, exactly like tests/db/*.
"""

from __future__ import annotations

from tests.db.conftest import (  # noqa: F401 -- re-exported fixtures, see module docstring
    admin_conn,
    admin_dsn,
    pg_database,
    psycopg,
    role_passwords,
    test_dsn,
)
from tests.db.test_review_merge import (  # noqa: F401 -- re-exported fixtures/helpers
    BASE,
    DIFF,
    SNAPSHOTS,
    _chain,
    _complete_review,
    _done,
    _ready,
    _snapshot,
    _snapshots,
    _test_repo_routes,
    rig,
)
