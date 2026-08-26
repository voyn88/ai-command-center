"""Repository-tier tests for the Wave-1 table families
(``command_center.runtime.db.wave1``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v15 migration on a fresh db (the
migration-on-empty-db check the task calls for).
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

    assert db.current_schema_version(db_path) == SCHEMA_VERSION


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- advisor_proposal -----------------------------------------------------


def test_create_and_get_advisor_proposal(db_path: Path) -> None:
    row = db.create_advisor_proposal(
        db_path, kind="trend", title="Adopt X", project_ref="AICC",
        body="body", expected_gain="high", effort="low",
    )
    assert row["status"] == "new"
    assert row["version"] == 0
    got = db.get_advisor_proposal(db_path, row["id"])
    assert got["title"] == "Adopt X"
    assert got["project_ref"] == "AICC"


def test_create_advisor_proposal_rejects_bad_kind_and_project(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_advisor_proposal(db_path, kind="nope", title="t", project_ref="AICC")
    with pytest.raises(ValueError):
        db.create_advisor_proposal(db_path, kind="trend", title="t", project_ref="")


def test_list_advisor_proposals_filters_and_pages(db_path: Path) -> None:
    for i in range(5):
        db.create_advisor_proposal(
            db_path, kind="ux", title=f"p{i}", project_ref="AICC"
        )
    db.create_advisor_proposal(db_path, kind="trend", title="other", project_ref="AIOS")

    aicc = db.list_advisor_proposals(db_path, project="AICC")
    assert len(aicc) == 5
    assert all(r["project_ref"] == "AICC" for r in aicc)

    page = db.list_advisor_proposals(db_path, project="AICC", limit=2, offset=0)
    page2 = db.list_advisor_proposals(db_path, project="AICC", limit=2, offset=2)
    assert len(page) == 2 and len(page2) == 2
    assert {r["id"] for r in page}.isdisjoint({r["id"] for r in page2})

    by_kind = db.list_advisor_proposals(db_path, kind="trend")
    assert {r["title"] for r in by_kind} == {"other"}


def test_advisor_proposal_status_transition_and_cas(db_path: Path) -> None:
    row = db.create_advisor_proposal(db_path, kind="ux", title="t", project_ref="AICC")
    accepted = db.set_advisor_proposal_status(
        db_path, row["id"], expected_version=0, status="accepted"
    )
    assert accepted["status"] == "accepted"
    assert accepted["version"] == 1

    # A stale version loses the CAS.
    with pytest.raises(db.LostUpdateError):
        db.set_advisor_proposal_status(
            db_path, row["id"], expected_version=0, status="dismissed"
        )


def test_advisor_proposal_illegal_transition_refused(db_path: Path) -> None:
    row = db.create_advisor_proposal(db_path, kind="ux", title="t", project_ref="AICC")
    dismissed = db.set_advisor_proposal_status(
        db_path, row["id"], expected_version=0, status="dismissed"
    )
    # dismissed is terminal — no further transition is allowed.
    with pytest.raises(db.InvalidAdvisorProposalTransitionError):
        db.set_advisor_proposal_status(
            db_path, row["id"], expected_version=dismissed["version"], status="accepted"
        )


def test_promote_advisor_proposal_records_link_and_is_terminal(db_path: Path) -> None:
    row = db.create_advisor_proposal(db_path, kind="trend", title="t", project_ref="AICC")
    promoted = db.promote_advisor_proposal(
        db_path, row["id"], expected_version=0, task_id="task-1"
    )
    assert promoted["status"] == "converted"
    assert promoted["promoted_task_id"] == "task-1"

    # A second promote is refused (converted is terminal) — no double task-link.
    with pytest.raises(db.InvalidAdvisorProposalTransitionError):
        db.promote_advisor_proposal(
            db_path, row["id"], expected_version=promoted["version"], task_id="task-2"
        )


# --- owner_item -----------------------------------------------------------


def test_owner_item_create_list_and_done(db_path: Path) -> None:
    a = db.create_owner_item(db_path, title="Call bank", detail="d", due="2026-08-13")
    db.create_owner_item(db_path, title="Sign form", done=True)
    assert a["done"] == 0

    done = db.set_owner_item_done(db_path, a["id"], expected_version=0, done=True)
    assert done["done"] == 1 and done["version"] == 1

    only_open = db.list_owner_items(db_path, done=False)
    assert all(r["done"] == 0 for r in only_open)


def test_owner_item_done_cas(db_path: Path) -> None:
    a = db.create_owner_item(db_path, title="t")
    db.set_owner_item_done(db_path, a["id"], expected_version=0, done=True)
    with pytest.raises(db.LostUpdateError):
        db.set_owner_item_done(db_path, a["id"], expected_version=0, done=False)


def test_owner_item_requires_title(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_owner_item(db_path, title="   ")


# --- digest_item ----------------------------------------------------------


def test_digest_item_roundtrips_refs(db_path: Path) -> None:
    row = db.create_digest_item(
        db_path, title="Weekly", body="b", category="ops", refs=["a", "b"]
    )
    assert row["refs"] == ["a", "b"]
    got = db.get_digest_item(db_path, row["id"])
    assert got["refs"] == ["a", "b"]
    assert got["category"] == "ops"


def test_digest_item_list_filters_by_category(db_path: Path) -> None:
    db.create_digest_item(db_path, title="a", category="ops")
    db.create_digest_item(db_path, title="b", category="sales")
    ops = db.list_digest_items(db_path, category="ops")
    assert {r["title"] for r in ops} == {"a"}


def test_digest_item_rejects_non_string_refs(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_digest_item(db_path, title="t", refs=[1, 2])  # type: ignore[list-item]


# --- redaction is applied in the SQL query (audit MED-2) ------------------


def test_list_advisor_proposals_exclude_projects_pages_over_visible_only(db_path: Path) -> None:
    # Interleave sensitive rows between visible ones. With redaction pushed into
    # the WHERE clause, a limit/offset page must return a *full* page of visible
    # rows — never one under-filled because a sensitive row fell inside it.
    for i in range(5):
        db.create_advisor_proposal(
            db_path, kind="trend", title=f"v{i}", project_ref="AICC"
        )
        db.create_advisor_proposal(
            db_path, kind="trend", title=f"s{i}", project_ref="BANK"
        )

    visible = db.list_advisor_proposals(db_path, exclude_projects=["BANK", "LEGAL"], limit=100)
    assert len(visible) == 5
    assert all(r["project_ref"] == "AICC" for r in visible)

    page1 = db.list_advisor_proposals(db_path, exclude_projects=["BANK"], limit=2, offset=0)
    page2 = db.list_advisor_proposals(db_path, exclude_projects=["BANK"], limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    # Pages are disjoint and contain only visible rows — offset math is honest.
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})
    assert all(r["project_ref"] == "AICC" for r in page1 + page2)


def test_list_owner_items_excludes_sensitive_keeps_unattributed(db_path: Path) -> None:
    db.create_owner_item(db_path, title="bank task", project_ref="BANK")
    db.create_owner_item(db_path, title="aicc task", project_ref="AICC")
    db.create_owner_item(db_path, title="no project")  # project_ref IS NULL
    rows = db.list_owner_items(db_path, exclude_projects=["BANK", "LEGAL"])
    assert {r["title"] for r in rows} == {"aicc task", "no project"}


def test_list_digest_items_excludes_sensitive_keeps_unattributed(db_path: Path) -> None:
    db.create_digest_item(db_path, title="bank", project_ref="BANK")
    db.create_digest_item(db_path, title="aicc", project_ref="AICC")
    db.create_digest_item(db_path, title="ambient")  # project_ref IS NULL
    rows = db.list_digest_items(db_path, exclude_projects=["BANK", "LEGAL"])
    assert {r["title"] for r in rows} == {"aicc", "ambient"}


def test_list_digest_items_for_day_excludes_sensitive(db_path: Path) -> None:
    db.create_digest_item(db_path, title="bank", project_ref="BANK", day="2026-08-12", position=0)
    db.create_digest_item(db_path, title="aicc", project_ref="AICC", day="2026-08-12", position=1)
    rows = db.list_digest_items_for_day(db_path, "2026-08-12", exclude_projects=["BANK"])
    assert {r["title"] for r in rows} == {"aicc"}
