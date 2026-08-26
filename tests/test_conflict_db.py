"""Repository-tier tests for the Wave-2 conflict table family
(``command_center.runtime.db.conflict``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v18 migration on a fresh db.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# --- migration ------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 18


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- create / get ---------------------------------------------------------


def test_create_and_get_conflict(db_path: Path) -> None:
    row = db.create_conflict(
        db_path, kind="merge", source_ref="pr:42", severity="sev2",
        project_ref="AICC",
    )
    assert row["status"] == "open"
    assert row["version"] == 0
    assert row["opened_at"] and row["resolved_at"] is None
    got = db.get_conflict(db_path, row["id"])
    assert got["kind"] == "merge"
    assert got["source_ref"] == "pr:42"
    assert got["project_ref"] == "AICC"


def test_create_conflict_rejects_bad_kind_and_severity(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_conflict(db_path, kind="nope")
    with pytest.raises(ValueError):
        db.create_conflict(db_path, kind="perf", severity="sev9")


def test_get_conflict_by_source_ref_dedup_primitive(db_path: Path) -> None:
    db.create_conflict(db_path, kind="perf", source_ref="incident:i1")
    found = db.get_conflict_by_source_ref(db_path, "incident:i1")
    assert found is not None and found["source_ref"] == "incident:i1"
    assert db.get_conflict_by_source_ref(db_path, "incident:none") is None
    # An empty source_ref never dedups.
    assert db.get_conflict_by_source_ref(db_path, "") is None


# --- list: filter + page --------------------------------------------------


def test_list_conflicts_filters_by_kind_status_owner(db_path: Path) -> None:
    db.create_conflict(db_path, kind="merge", owner="alice")
    db.create_conflict(db_path, kind="perf", owner="bob")
    db.create_conflict(db_path, kind="security", owner="alice")

    assert len(db.list_conflicts(db_path, kind="merge")) == 1
    assert len(db.list_conflicts(db_path, owner="alice")) == 2
    assert len(db.list_conflicts(db_path, status="open")) == 3
    assert len(db.list_conflicts(db_path, status="resolved")) == 0


def test_list_conflicts_pages(db_path: Path) -> None:
    for i in range(5):
        db.create_conflict(db_path, kind="budget", source_ref=f"b{i}")
    page = db.list_conflicts(db_path, limit=2, offset=0)
    page2 = db.list_conflicts(db_path, limit=2, offset=2)
    assert len(page) == 2 and len(page2) == 2
    assert {r["id"] for r in page}.isdisjoint({r["id"] for r in page2})


def test_list_conflicts_excludes_sensitive_projects_in_sql(db_path: Path) -> None:
    db.create_conflict(db_path, kind="merge", project_ref="AICC")
    db.create_conflict(db_path, kind="security", source_ref="secret", project_ref="BANK")
    db.create_conflict(db_path, kind="perf", project_ref=None)  # un-attributed kept

    visible = db.list_conflicts(db_path, exclude_projects=["BANK", "LEGAL"])
    refs = {r["project_ref"] for r in visible}
    assert "BANK" not in refs
    assert {"AICC", None} <= refs
    # The redacted row's source_ref never appears.
    assert all(r["source_ref"] != "secret" for r in visible)


# --- field updates (owner / mitigation) -----------------------------------


def test_update_conflict_fields_cas_and_bump(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="merge")
    updated = db.update_conflict_fields(
        db_path, row["id"], expected_version=0, fields={"owner": "alice"}
    )
    assert updated["owner"] == "alice"
    assert updated["version"] == 1

    with pytest.raises(db.LostUpdateError):
        db.update_conflict_fields(
            db_path, row["id"], expected_version=0, fields={"owner": "bob"}
        )


def test_update_conflict_fields_rejects_unknown_field(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="merge")
    with pytest.raises(ValueError):
        db.update_conflict_fields(
            db_path, row["id"], expected_version=0, fields={"status": "resolved"}
        )


def test_resolved_conflict_is_frozen_to_field_updates(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="merge", owner="alice", mitigation="plan")
    resolved = db.set_conflict_status(
        db_path, row["id"], expected_version=0, status="resolved"
    )
    with pytest.raises(db.ConflictResolvedError):
        db.update_conflict_fields(
            db_path, row["id"], expected_version=resolved["version"],
            fields={"owner": "bob"},
        )


# --- status transitions ---------------------------------------------------


def test_status_transition_stamps_resolved_at(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="perf")
    mitigating = db.set_conflict_status(
        db_path, row["id"], expected_version=0, status="mitigating"
    )
    assert mitigating["status"] == "mitigating"
    assert mitigating["resolved_at"] is None
    resolved = db.set_conflict_status(
        db_path, row["id"], expected_version=mitigating["version"], status="resolved"
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None


def test_illegal_transition_out_of_terminal_refused(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="perf")
    resolved = db.set_conflict_status(
        db_path, row["id"], expected_version=0, status="resolved"
    )
    with pytest.raises(db.InvalidConflictTransitionError):
        db.set_conflict_status(
            db_path, row["id"], expected_version=resolved["version"], status="mitigating"
        )


def test_status_transition_is_cas_guarded(db_path: Path) -> None:
    row = db.create_conflict(db_path, kind="perf")
    with pytest.raises(db.LostUpdateError):
        db.set_conflict_status(
            db_path, row["id"], expected_version=99, status="mitigating"
        )
