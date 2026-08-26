"""Repository-tier tests for the Wave-3 Council table family
(``command_center.runtime.db.council``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v22 migration on a fresh db.

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

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 22


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- create motion --------------------------------------------------------


def test_create_and_get_motion(db_path: Path) -> None:
    row = db.create_motion(
        db_path, title="Adopt X", proposed_by="chair", quorum=3, project_ref="AICC"
    )
    assert row["status"] == "open"
    assert row["version"] == 0
    assert row["quorum"] == 3
    assert row["opened_at"] and row["decided_at"] is None
    got = db.get_motion(db_path, row["id"])
    assert got["title"] == "Adopt X" and got["proposed_by"] == "chair"


def test_create_motion_rejects_bad_inputs(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_motion(db_path, title="", proposed_by="chair")
    with pytest.raises(ValueError):
        db.create_motion(db_path, title="X", proposed_by="")
    with pytest.raises(ValueError):
        db.create_motion(db_path, title="X", proposed_by="chair", quorum=0)


def test_create_motion_journals_open_event(db_path: Path) -> None:
    row = db.create_motion(db_path, title="X", proposed_by="chair")
    events = db.list_events(db_path, row["id"])
    assert [e["event_type"] for e in events] == ["motion_opened"]
    assert events[0]["seq"] == 1 and events[0]["actor"] == "chair"


def test_get_motion_by_source_ref_dedup_primitive(db_path: Path) -> None:
    db.create_motion(db_path, title="X", proposed_by="a", source_ref="proposal:p1")
    found = db.get_motion_by_source_ref(db_path, "proposal:p1")
    assert found is not None and found["source_ref"] == "proposal:p1"
    assert db.get_motion_by_source_ref(db_path, "proposal:none") is None
    assert db.get_motion_by_source_ref(db_path, "") is None


# --- list motions ---------------------------------------------------------


def test_list_motions_filters_and_pages(db_path: Path) -> None:
    db.create_motion(db_path, title="a", proposed_by="chair", project_ref="AICC")
    db.create_motion(db_path, title="b", proposed_by="chair", project_ref="AIOS")
    db.create_motion(db_path, title="c", proposed_by="chair", project_ref="AICC")
    assert len(db.list_motions(db_path)) == 3
    aicc = db.list_motions(db_path, project="AICC")
    assert len(aicc) == 2
    page = db.list_motions(db_path, limit=1, offset=0)
    assert len(page) == 1


def test_list_motions_excludes_sensitive_projects_in_sql(db_path: Path) -> None:
    db.create_motion(db_path, title="ok", proposed_by="chair", project_ref="AICC")
    db.create_motion(db_path, title="secret", proposed_by="chair", project_ref="BANK")
    visible = db.list_motions(db_path, exclude_projects=["BANK", "LEGAL"])
    refs = {m["project_ref"] for m in visible}
    assert refs == {"AICC"}


# --- votes: one per voter, roles recorded ---------------------------------


def test_cast_vote_records_role_and_kind(db_path: Path) -> None:
    m = db.create_motion(db_path, title="X", proposed_by="chair")
    v = db.cast_vote(
        db_path, motion_id=m["id"], voter_id="security", role="security",
        choice="yes", voter_kind="ai", rationale="safe",
    )
    assert v["role"] == "security" and v["choice"] == "yes"
    stored = db.list_votes(db_path, m["id"])
    assert len(stored) == 1 and stored[0]["voter_id"] == "security"


def test_double_vote_is_refused(db_path: Path) -> None:
    m = db.create_motion(db_path, title="X", proposed_by="chair")
    db.cast_vote(db_path, motion_id=m["id"], voter_id="chair", role="chair", choice="yes")
    with pytest.raises(db.DoubleVoteError):
        db.cast_vote(db_path, motion_id=m["id"], voter_id="chair", role="chair", choice="no")
    assert db.count_votes(db_path, m["id"]) == 1


def test_cast_vote_rejects_bad_choice_and_kind_and_empty_role(db_path: Path) -> None:
    m = db.create_motion(db_path, title="X", proposed_by="chair")
    with pytest.raises(ValueError):
        db.cast_vote(db_path, motion_id=m["id"], voter_id="v", role="r", choice="maybe")
    with pytest.raises(ValueError):
        db.cast_vote(db_path, motion_id=m["id"], voter_id="v", role="r", choice="yes", voter_kind="bot")
    with pytest.raises(ValueError):
        db.cast_vote(db_path, motion_id=m["id"], voter_id="v", role="", choice="yes")


def test_vote_on_missing_motion_raises(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.cast_vote(db_path, motion_id="nope", voter_id="v", role="r", choice="yes")


# --- decision: tally, immutability, roles snapshot ------------------------


def _open_with_votes(db_path: Path, choices: dict[str, str], quorum: int = 1) -> dict:
    m = db.create_motion(db_path, title="X", proposed_by="chair", quorum=quorum)
    for voter, choice in choices.items():
        db.cast_vote(
            db_path, motion_id=m["id"], voter_id=voter, role=voter, choice=choice
        )
    return m


def test_record_decision_is_immutable_and_snapshots_roles(db_path: Path) -> None:
    m = _open_with_votes(db_path, {"chair": "yes", "security": "yes", "product": "no"}, quorum=3)
    decision = db.record_decision(
        db_path, motion_id=m["id"], expected_version=0, outcome="approved",
        tally={"yes": 2, "no": 1, "abstain": 0},
        roles=[{"voter_id": "chair", "voter_kind": "ai", "role": "chair", "choice": "yes"}],
        rationale="majority yes", quorum=3,
    )
    assert decision["outcome"] == "approved"
    assert decision["tally"] == {"yes": 2, "no": 1, "abstain": 0}
    assert decision["roles"][0]["role"] == "chair"
    # motion is now terminal
    assert db.get_motion(db_path, m["id"])["status"] == "decided"
    # immutable: a second record is refused (motion no longer open)
    with pytest.raises(db.MotionNotOpenError):
        db.record_decision(
            db_path, motion_id=m["id"], expected_version=1, outcome="rejected",
            tally={}, roles=[], rationale="x", quorum=3,
        )


def test_record_decision_rejects_bad_outcome(db_path: Path) -> None:
    m = _open_with_votes(db_path, {"chair": "yes"})
    with pytest.raises(ValueError):
        db.record_decision(
            db_path, motion_id=m["id"], expected_version=0, outcome="maybe",
            tally={}, roles=[], rationale="", quorum=1,
        )


def test_get_and_list_decisions(db_path: Path) -> None:
    m = _open_with_votes(db_path, {"chair": "yes"})
    db.record_decision(
        db_path, motion_id=m["id"], expected_version=0, outcome="approved",
        tally={"yes": 1, "no": 0, "abstain": 0}, roles=[], rationale="ok", quorum=1,
    )
    got = db.get_decision(db_path, m["id"])
    assert got is not None and got["outcome"] == "approved"
    listed = db.list_decisions(db_path)
    assert len(listed) == 1
    assert db.list_decisions(db_path, outcome="rejected") == []


def test_list_decisions_excludes_named_motions(db_path: Path) -> None:
    m = _open_with_votes(db_path, {"chair": "yes"})
    db.record_decision(
        db_path, motion_id=m["id"], expected_version=0, outcome="approved",
        tally={}, roles=[], rationale="", quorum=1,
    )
    assert db.list_decisions(db_path, exclude_motions=[m["id"]]) == []


# --- withdraw + journal ---------------------------------------------------


def test_withdraw_motion_is_terminal_and_journaled(db_path: Path) -> None:
    m = db.create_motion(db_path, title="X", proposed_by="chair")
    withdrawn = db.withdraw_motion(db_path, m["id"], expected_version=0)
    assert withdrawn["status"] == "withdrawn"
    types = [e["event_type"] for e in db.list_events(db_path, m["id"])]
    assert types == ["motion_opened", "motion_withdrawn"]
    # cannot decide a withdrawn motion
    with pytest.raises(db.MotionNotOpenError):
        db.record_decision(
            db_path, motion_id=m["id"], expected_version=1, outcome="approved",
            tally={}, roles=[], rationale="", quorum=1,
        )


def test_full_journal_orders_by_seq(db_path: Path) -> None:
    m = _open_with_votes(db_path, {"chair": "yes", "security": "no"}, quorum=2)
    db.record_decision(
        db_path, motion_id=m["id"], expected_version=0, outcome="deferred",
        tally={"yes": 1, "no": 1, "abstain": 0}, roles=[], rationale="tie", quorum=2,
    )
    events = db.list_events(db_path, m["id"])
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["event_type"] for e in events] == [
        "motion_opened", "vote_cast", "vote_cast", "decision_recorded"
    ]
