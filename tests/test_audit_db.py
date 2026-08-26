"""Repository-tier tests for the Wave-2 Audit table families
(``command_center.runtime.db.audit``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v19 migration on a fresh db.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids —
no real names or paths — keeping the public-repo privacy gate green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.audit import (
    InvalidAuditFindingTransitionError,
    InvalidAuditRunTransitionError,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# --- migration ------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 19


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- audit_run ------------------------------------------------------------


def test_create_and_get_audit_run(db_path: Path) -> None:
    row = db.create_audit_run(db_path, project_ref="AICC", checks=["lint", "deps"])
    assert row["status"] == "running"
    assert row["version"] == 0
    assert row["checks"] == ["lint", "deps"]  # decoded from JSON
    got = db.get_audit_run(db_path, row["id"])
    assert got["project_ref"] == "AICC"
    assert got["checks"] == ["lint", "deps"]


def test_create_audit_run_rejects_empty_project_and_bad_status(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_audit_run(db_path, project_ref="")
    with pytest.raises(ValueError):
        db.create_audit_run(db_path, project_ref="AICC", status="bogus")


def test_set_audit_run_status_transitions_and_cas(db_path: Path) -> None:
    row = db.create_audit_run(db_path, project_ref="AICC")
    done = db.set_audit_run_status(
        db_path, row["id"], expected_version=0, status="completed", finding_count=3
    )
    assert done["status"] == "completed"
    assert done["finding_count"] == 3
    assert done["completed_at"] is not None
    assert done["version"] == 1
    # terminal state cannot move again
    with pytest.raises(InvalidAuditRunTransitionError):
        db.set_audit_run_status(db_path, row["id"], expected_version=1, status="running")
    # stale writer loses
    with pytest.raises(db.LostUpdateError):
        db.set_audit_run_status(db_path, row["id"], expected_version=0, status="failed")


def test_list_audit_runs_filters_and_pages(db_path: Path) -> None:
    for _ in range(3):
        db.create_audit_run(db_path, project_ref="AICC")
    db.create_audit_run(db_path, project_ref="AIOS")
    assert len(db.list_audit_runs(db_path)) == 4
    assert len(db.list_audit_runs(db_path, project="AICC")) == 3
    assert len(db.list_audit_runs(db_path, limit=2)) == 2
    assert len(db.list_audit_runs(db_path, statuses=["running"])) == 4
    assert db.list_audit_runs(db_path, statuses=[]) == []


# --- audit_finding: the status + owner invariant --------------------------


def test_create_finding_requires_owner(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    with pytest.raises(ValueError):
        db.create_audit_finding(
            db_path, run_id=run["id"], category="lint", summary="x", owner=""
        )
    with pytest.raises(ValueError):
        db.create_audit_finding(
            db_path, run_id=run["id"], category="lint", summary="x", owner="   "
        )


def test_create_finding_always_has_status_and_owner(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    row = db.create_audit_finding(
        db_path, run_id=run["id"], category="security", summary="hardcoded secret",
        owner="security", severity="high", file_path="a.py", loc="10:4",
        project_ref="AICC",
    )
    assert row["status"] == "open"  # default, always present
    assert row["owner"] == "security"  # always present
    got = db.get_audit_finding(db_path, row["id"])
    assert got["status"] == "open" and got["owner"] == "security"


def test_create_finding_validates_category_severity_status(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    for bad in (
        dict(category="bogus", severity="high"),
        dict(category="lint", severity="apocalyptic"),
        dict(category="lint", severity="low", status="wat"),
    ):
        with pytest.raises(ValueError):
            db.create_audit_finding(
                db_path, run_id=run["id"], summary="x", owner="o", **bad
            )


def test_finding_status_transitions_and_reopen(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    f = db.create_audit_finding(
        db_path, run_id=run["id"], category="lint", summary="x", owner="engineering"
    )
    acked = db.set_audit_finding_status(db_path, f["id"], expected_version=0, status="ack")
    assert acked["status"] == "ack" and acked["version"] == 1
    fixed = db.set_audit_finding_status(db_path, f["id"], expected_version=1, status="fixed")
    assert fixed["status"] == "fixed"
    # fixed -> ack is not allowed; fixed -> open (reopen) is
    with pytest.raises(InvalidAuditFindingTransitionError):
        db.set_audit_finding_status(db_path, f["id"], expected_version=2, status="ack")
    reopened = db.set_audit_finding_status(db_path, f["id"], expected_version=2, status="open")
    assert reopened["status"] == "open"


def test_promote_finding_records_link_and_acks(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    f = db.create_audit_finding(
        db_path, run_id=run["id"], category="deps", summary="x", owner="platform"
    )
    promoted = db.promote_audit_finding(
        db_path, f["id"], expected_version=0, task_id="task-123"
    )
    assert promoted["promoted_task_id"] == "task-123"
    assert promoted["status"] == "ack"
    # a fixed finding cannot be promoted
    g = db.create_audit_finding(
        db_path, run_id=run["id"], category="deps", summary="y", owner="platform"
    )
    db.set_audit_finding_status(db_path, g["id"], expected_version=0, status="fixed")
    with pytest.raises(InvalidAuditFindingTransitionError):
        db.promote_audit_finding(db_path, g["id"], expected_version=1, task_id="t")


def test_list_findings_filters_by_run_status_owner(db_path: Path) -> None:
    run = db.create_audit_run(db_path, project_ref="AICC")
    db.create_audit_finding(db_path, run_id=run["id"], category="lint", summary="a", owner="engineering")
    db.create_audit_finding(db_path, run_id=run["id"], category="security", summary="b", owner="security")
    other = db.create_audit_run(db_path, project_ref="AICC")
    db.create_audit_finding(db_path, run_id=other["id"], category="lint", summary="c", owner="engineering")
    assert len(db.list_audit_findings(db_path, run_id=run["id"])) == 2
    assert len(db.list_audit_findings(db_path, owner="security")) == 1
    assert len(db.list_audit_findings(db_path, owner="engineering")) == 2
    assert len(db.list_audit_findings(db_path, status="open")) == 3


# --- redaction is in SQL --------------------------------------------------


def test_redaction_excludes_sensitive_projects_in_query(db_path: Path) -> None:
    db.create_audit_run(db_path, project_ref="AICC")
    db.create_audit_run(db_path, project_ref="BANK")
    db.create_audit_run(db_path, project_ref="LEGAL")
    runs = db.list_audit_runs(db_path, exclude_projects=["BANK", "LEGAL"])
    assert {r["project_ref"] for r in runs} == {"AICC"}


def test_finding_redaction_pages_over_visible_rows(db_path: Path) -> None:
    # A sensitive finding inside a page must not shrink the visible page — the
    # exclusion is in the WHERE clause, not a post-filter (the MED-2 lesson).
    run_a = db.create_audit_run(db_path, project_ref="AICC")
    run_b = db.create_audit_run(db_path, project_ref="BANK")
    for i in range(3):
        db.create_audit_finding(
            db_path, run_id=run_a["id"], category="lint", summary=f"a{i}",
            owner="engineering", project_ref="AICC",
        )
        db.create_audit_finding(
            db_path, run_id=run_b["id"], category="lint", summary=f"b{i}",
            owner="engineering", project_ref="BANK",
        )
    visible = db.list_audit_findings(db_path, exclude_projects=["BANK"], limit=3)
    assert len(visible) == 3
    assert all(f["project_ref"] == "AICC" for f in visible)
