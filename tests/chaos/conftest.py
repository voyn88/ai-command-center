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

import json
import subprocess as sp

import pytest

from command_center.orchestrator import review_merge
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


@pytest.fixture(autouse=True)
def isolated_generated_dir():
    """Avoid initializing the Streamlit application in this backend-only suite."""
    yield None


def accepted_pull(head: str, *, state: str = "OPEN", merge_oid: str | None = None) -> str:
    """Return the current fail-closed shape of an independently accepted PR."""
    body = {
        "state": state,
        "headRefOid": head,
        "mergeStateStatus": "CLEAN",
        "author": {"login": "writer-bot"},
        "reviews": [
            {
                "body": f"ACCEPTANCE: ACCEPT {head}",
                "author": {"login": "acceptance-gate[bot]"},
            }
        ],
        "statusCheckRollup": [
            {"name": "CI", "conclusion": "SUCCESS"},
            {"name": "Final merge gate", "conclusion": "SUCCESS"},
            {
                "name": "Acceptance gate (independent verdict on exact SHA)",
                "conclusion": "SUCCESS",
            },
        ],
    }
    if merge_oid is not None:
        body["mergeCommit"] = {"oid": merge_oid}
    return json.dumps(body)


def stub_merge_gateway(monkeypatch, merged_state: dict[str, bool]) -> None:
    """Stub the dedicated merge identity and its sole privileged mutation."""
    monkeypatch.setattr(
        review_merge,
        "_merge_app_credentials",
        lambda: review_merge.github_app_auth.GitHubAppCredentials(
            "merge-app", "merge-installation", "/dev/null"
        ),
    )
    monkeypatch.setattr(
        review_merge.github_app_auth,
        "installation_token",
        lambda _credentials: "test-merge-token",
    )

    def merge(_repo_path, _pr_url, _token):
        merged_state["merged"] = True
        return sp.CompletedProcess(["gh", "pr", "merge"], 0, "merged", "")

    monkeypatch.setattr(review_merge, "_merge_pr_as_bot", merge)
