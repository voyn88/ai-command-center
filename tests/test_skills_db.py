"""Repository-tier tests for the Skill Acquisition table family
(``command_center.runtime.db.skills``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v26 migration on a fresh db.

Fixtures use only generic names and invented ids — no real names or paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.skills import (
    InvalidSkillItemTransitionError,
    InvalidSkillSourceTransitionError,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


def _approved_source(db_path: Path, **overrides) -> dict:
    defaults = {
        "name": "Registry", "kind": "mcp_registry", "origin": "mcp-registry:test",
        "proposed_by": "alice",
    }
    defaults.update(overrides)
    row = db.create_skill_source(db_path, **defaults)
    return db.set_skill_source_status(
        db_path, row["id"], expected_version=row["lock_version"],
        new_status="approved", actor="alice",
    )


# --- migration ---------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 26


# --- skill_source: the allowlist -----------------------------------------


def test_create_source_starts_proposed(db_path: Path) -> None:
    row = db.create_skill_source(
        db_path, name="Registry", kind="mcp_registry", origin="mcp:x", proposed_by="alice",
    )
    assert row["status"] == "proposed"
    assert row["approved_by"] == ""
    assert row["lock_version"] == 0


def test_create_source_rejects_bad_kind(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_skill_source(db_path, name="x", kind="nonsense", origin="o", proposed_by="a")


def test_create_source_rejects_duplicate_origin(db_path: Path) -> None:
    db.create_skill_source(db_path, name="a", kind="mcp_registry", origin="dup", proposed_by="a")
    with pytest.raises(ValueError):
        db.create_skill_source(db_path, name="b", kind="mcp_registry", origin="dup", proposed_by="a")


def test_approve_source_sets_approved_by(db_path: Path) -> None:
    row = db.create_skill_source(db_path, name="a", kind="repo_doc", origin="o", proposed_by="alice")
    approved = db.set_skill_source_status(
        db_path, row["id"], expected_version=0, new_status="approved", actor="bob",
    )
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "bob"
    assert approved["lock_version"] == 1


def test_source_cannot_skip_the_human_gate_to_revoked_then_back(db_path: Path) -> None:
    row = db.create_skill_source(db_path, name="a", kind="repo_doc", origin="o", proposed_by="alice")
    revoked = db.set_skill_source_status(
        db_path, row["id"], expected_version=0, new_status="revoked", actor="bob",
    )
    assert revoked["status"] == "revoked"
    with pytest.raises(InvalidSkillSourceTransitionError):
        db.set_skill_source_status(
            db_path, row["id"], expected_version=1, new_status="approved", actor="bob",
        )


def test_source_status_refuses_stale_version(db_path: Path) -> None:
    row = db.create_skill_source(db_path, name="a", kind="repo_doc", origin="o", proposed_by="alice")
    with pytest.raises(db.LostUpdateError):
        db.set_skill_source_status(
            db_path, row["id"], expected_version=99, new_status="approved", actor="bob",
        )


def test_list_sources_filters_by_kind_and_status(db_path: Path) -> None:
    db.create_skill_source(db_path, name="a", kind="mcp_registry", origin="o1", proposed_by="x")
    db.create_skill_source(db_path, name="b", kind="repo_doc", origin="o2", proposed_by="x")
    approved = db.create_skill_source(db_path, name="c", kind="mcp_registry", origin="o3", proposed_by="x")
    db.set_skill_source_status(
        db_path, approved["id"], expected_version=0, new_status="approved", actor="x",
    )
    assert len(db.list_skill_sources(db_path)) == 3
    assert len(db.list_skill_sources(db_path, kind="mcp_registry")) == 2
    assert len(db.list_skill_sources(db_path, status="approved")) == 1


# --- skill_item: pinning + allowlist gate at creation ---------------------


def test_create_candidate_requires_approved_source(db_path: Path) -> None:
    proposed = db.create_skill_source(
        db_path, name="a", kind="mcp_registry", origin="o", proposed_by="x",
    )
    with pytest.raises(ValueError, match="not an approved source"):
        db.create_skill_candidate(
            db_path, name="skill", kind="mcp_server", version="1.0.0",
            content_hash=_HASH_A, source_id=proposed["id"],
        )


def test_create_candidate_rejects_unknown_source(db_path: Path) -> None:
    with pytest.raises(ValueError, match="does not resolve"):
        db.create_skill_candidate(
            db_path, name="skill", kind="mcp_server", version="1.0.0",
            content_hash=_HASH_A, source_id="nope",
        )


def test_create_candidate_rejects_empty_version(db_path: Path) -> None:
    source = _approved_source(db_path)
    with pytest.raises(ValueError, match="pinning invariant"):
        db.create_skill_candidate(
            db_path, name="skill", kind="mcp_server", version="",
            content_hash=_HASH_A, source_id=source["id"],
        )


def test_create_candidate_rejects_malformed_hash(db_path: Path) -> None:
    source = _approved_source(db_path)
    with pytest.raises(ValueError, match="sha256"):
        db.create_skill_candidate(
            db_path, name="skill", kind="mcp_server", version="1.0.0",
            content_hash="not-a-hash", source_id=source["id"],
        )


def test_create_candidate_succeeds_from_approved_source(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="mcp_server", version="1.0.0",
        content_hash=_HASH_A.upper(), source_id=source["id"],
        provenance="url:https://example.test", selection_rationale={"score": 0.9},
    )
    assert item["status"] == "candidate"
    # Hash is normalized to lowercase.
    assert item["content_hash"] == _HASH_A
    assert item["selection_rationale"] == {"score": 0.9}
    got = db.get_skill_item(db_path, item["id"])
    assert got["name"] == "skill" and got["source_id"] == source["id"]


def test_list_items_filters_by_kind_status_task_class(db_path: Path) -> None:
    source = _approved_source(db_path)
    a = db.create_skill_candidate(
        db_path, name="a", kind="mcp_server", version="1", content_hash=_HASH_A,
        source_id=source["id"], task_class="pdf",
    )
    db.create_skill_candidate(
        db_path, name="b", kind="cli_tool", version="1", content_hash=_HASH_B,
        source_id=source["id"], task_class="pdf",
    )
    db.acquire_skill_item(
        db_path, a["id"], expected_version=0, actor="x", executor="null-skill-executor",
    )
    assert len(db.list_skill_items(db_path)) == 2
    assert len(db.list_skill_items(db_path, kind="cli_tool")) == 1
    assert len(db.list_skill_items(db_path, status="acquired")) == 1
    assert len(db.list_skill_items(db_path, task_class="pdf")) == 2


# --- lifecycle: acquire / reject / revoke + audit log ----------------------


def test_acquire_flips_status_and_logs(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="2.0.0",
        content_hash=_HASH_A, source_id=source["id"], provenance="prov",
    )
    item_row, log_row = db.acquire_skill_item(
        db_path, item["id"], expected_version=0, actor="alice",
        executor="null-skill-executor", detail="isolated no-op", metadata={"mode": "null"},
    )
    assert item_row["status"] == "acquired"
    assert item_row["lock_version"] == 1
    assert log_row["actor"] == "alice"
    assert log_row["action"] == "acquire"
    assert log_row["version"] == "2.0.0"
    assert log_row["content_hash"] == _HASH_A
    assert log_row["executor"] == "null-skill-executor"

    log = db.list_skill_acquisition_log(db_path, item["id"])
    assert len(log) == 1
    assert log[0]["metadata"] == {"mode": "null"}


def test_acquire_requires_executor_name(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    with pytest.raises(ValueError, match="executor must be non-empty"):
        db.acquire_skill_item(
            db_path, item["id"], expected_version=0, actor="alice", executor="",
        )


def test_reacquire_is_refused_at_repository_boundary(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    db.acquire_skill_item(
        db_path, item["id"], expected_version=0, actor="a", executor="null-skill-executor",
    )
    with pytest.raises(InvalidSkillItemTransitionError):
        db.acquire_skill_item(
            db_path, item["id"], expected_version=1, actor="a", executor="null-skill-executor",
        )


def test_reject_then_acquire_is_refused(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    db.reject_skill_item(db_path, item["id"], expected_version=0, actor="a", detail="lost selection")
    with pytest.raises(InvalidSkillItemTransitionError):
        db.acquire_skill_item(
            db_path, item["id"], expected_version=1, actor="a", executor="null-skill-executor",
        )


def test_revoke_only_from_acquired(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    with pytest.raises(InvalidSkillItemTransitionError):
        db.revoke_skill_item(db_path, item["id"], expected_version=0, actor="a")
    db.acquire_skill_item(
        db_path, item["id"], expected_version=0, actor="a", executor="null-skill-executor",
    )
    item_row, log_row = db.revoke_skill_item(
        db_path, item["id"], expected_version=1, actor="a", detail="no measurable improvement",
    )
    assert item_row["status"] == "revoked"
    assert log_row["action"] == "revoke"


def test_transition_refuses_stale_version(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    with pytest.raises(db.LostUpdateError):
        db.acquire_skill_item(
            db_path, item["id"], expected_version=99, actor="a", executor="null-skill-executor",
        )


def test_transition_missing_item_raises_keyerror(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.acquire_skill_item(
            db_path, "nope", expected_version=0, actor="a", executor="null-skill-executor",
        )


# --- skill_outcome ----------------------------------------------------------


def test_record_outcome_requires_known_skill(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.record_skill_outcome(
            db_path, skill_id="nope", task_id="t1", phase="baseline",
            cost=1.0, accepted=True, first_pass=True,
        )


def test_record_and_list_outcomes(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    db.record_skill_outcome(
        db_path, skill_id=item["id"], task_id="t1", phase="baseline",
        cost=2.0, accepted=True, first_pass=False,
    )
    db.record_skill_outcome(
        db_path, skill_id=item["id"], task_id="t2", phase="with_skill",
        cost=1.0, accepted=True, first_pass=True,
    )
    all_outcomes = db.list_skill_outcomes(db_path, item["id"])
    assert len(all_outcomes) == 2
    baseline = db.list_skill_outcomes(db_path, item["id"], phase="baseline")
    assert len(baseline) == 1 and baseline[0]["cost"] == 2.0
    assert baseline[0]["accepted"] is True and baseline[0]["first_pass"] is False


def test_record_outcome_rejects_bad_phase(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, name="skill", kind="agent_skill", version="1", content_hash=_HASH_A,
        source_id=source["id"],
    )
    with pytest.raises(ValueError):
        db.record_skill_outcome(
            db_path, skill_id=item["id"], task_id="t1", phase="nonsense",
            cost=1.0, accepted=True, first_pass=True,
        )
