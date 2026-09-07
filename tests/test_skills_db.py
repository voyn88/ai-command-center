"""Repository-tier tests for the skill-acquisition table family
(``command_center.runtime.db.skills``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state.

Fixtures use only generic names and invented ids — no real names or paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.skills import (
    InvalidSkillItemTransitionError,
    InvalidSkillSourceTransitionError,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


def _approved_source(db_path: Path, *, origin: str = "https://example.test/registry") -> dict:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin=origin)
    return db.transition_skill_source(
        db_path, source["id"], expected_version=0, to_status="approved", actor="alice",
    )


# --- migration --------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 26


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- skill_source: create / get / list --------------------------------------


def test_create_skill_source_is_always_proposed(db_path: Path) -> None:
    row = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    assert row["status"] == "proposed"
    assert row["lock_version"] == 0
    assert row["created_at"] and row["updated_at"]


def test_create_skill_source_rejects_bad_kind(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_skill_source(db_path, kind="nonsense", origin="o-1")


def test_create_skill_source_rejects_empty_origin(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_skill_source(db_path, kind="mcp_registry", origin="   ")


def test_create_skill_source_rejects_duplicate_origin_by_name(db_path: Path) -> None:
    db.create_skill_source(db_path, kind="mcp_registry", origin="dup")
    with pytest.raises(ValueError, match="already proposed"):
        db.create_skill_source(db_path, kind="mcp_registry", origin="dup")


def test_create_skill_source_id_collision_is_not_mislabeled_as_origin(db_path: Path) -> None:
    """A caller-supplied ``source_id`` colliding with an existing row is a
    distinct failure from a duplicate ``origin`` — the message must say so,
    not reuse the origin-uniqueness wording (the two constraints must not be
    conflated)."""
    first = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1", source_id="dup-id")
    with pytest.raises(ValueError) as excinfo:
        db.create_skill_source(db_path, kind="mcp_registry", origin="o-2", source_id="dup-id")
    assert "already proposed" not in str(excinfo.value)
    # The original row is untouched.
    assert db.get_skill_source(db_path, "dup-id")["origin"] == first["origin"]


def test_get_missing_source_returns_none(db_path: Path) -> None:
    assert db.get_skill_source(db_path, "nope") is None


def test_get_skill_source_by_origin(db_path: Path) -> None:
    created = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    found = db.get_skill_source_by_origin(db_path, "o-1")
    assert found is not None and found["id"] == created["id"]
    assert db.get_skill_source_by_origin(db_path, "nope") is None


def test_list_skill_sources_filters_by_kind_and_status_and_pages(db_path: Path) -> None:
    db.create_skill_source(db_path, kind="mcp_registry", origin="a")
    db.create_skill_source(db_path, kind="repo_doc", origin="b")
    approved = db.create_skill_source(db_path, kind="mcp_registry", origin="c")
    db.transition_skill_source(
        db_path, approved["id"], expected_version=0, to_status="approved", actor="alice",
    )

    assert len(db.list_skill_sources(db_path)) == 3
    assert len(db.list_skill_sources(db_path, kind="mcp_registry")) == 2
    assert len(db.list_skill_sources(db_path, status="approved")) == 1
    assert len(db.list_skill_sources(db_path, status="proposed")) == 2
    page = db.list_skill_sources(db_path, limit=1, offset=0)
    assert len(page) == 1


# --- skill_source: transition (CAS + allowed edges) -------------------------


def test_approve_then_revoke_source(db_path: Path) -> None:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    approved = db.transition_skill_source(
        db_path, source["id"], expected_version=0, to_status="approved", actor="alice",
    )
    assert approved["status"] == "approved" and approved["lock_version"] == 1

    revoked = db.transition_skill_source(
        db_path, source["id"], expected_version=1, to_status="revoked", actor="alice",
    )
    assert revoked["status"] == "revoked" and revoked["lock_version"] == 2


def test_transition_source_missing_raises_keyerror(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.transition_skill_source(
            db_path, "nope", expected_version=0, to_status="approved", actor="alice",
        )


def test_transition_source_version_mismatch_raises_lost_update(db_path: Path) -> None:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    with pytest.raises(db.LostUpdateError):
        db.transition_skill_source(
            db_path, source["id"], expected_version=99, to_status="approved", actor="alice",
        )


def test_transition_source_disallowed_edge_raises(db_path: Path) -> None:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    db.transition_skill_source(
        db_path, source["id"], expected_version=0, to_status="revoked", actor="alice",
    )
    with pytest.raises(InvalidSkillSourceTransitionError):
        # revoked is terminal
        db.transition_skill_source(
            db_path, source["id"], expected_version=1, to_status="approved", actor="alice",
        )


def test_transition_source_requires_actor(db_path: Path) -> None:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")
    with pytest.raises(ValueError):
        db.transition_skill_source(
            db_path, source["id"], expected_version=0, to_status="approved", actor="  ",
        )


# --- skill_item: create only against an approved source (atomic) -----------


def test_create_candidate_against_approved_source(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="fetcher", kind="mcp_server",
        content_hash="sha256:abc",
    )
    assert item["status"] == "candidate"
    assert item["source_id"] == source["id"]


def test_create_candidate_missing_source_raises_keyerror(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.create_skill_candidate(
            db_path, source_id="nope", name="x", kind="mcp_server", content_hash="h",
        )


def test_create_candidate_unapproved_source_raises_valueerror(db_path: Path) -> None:
    source = db.create_skill_source(db_path, kind="mcp_registry", origin="o-1")  # proposed
    with pytest.raises(ValueError, match="not approved"):
        db.create_skill_candidate(
            db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
        )


def test_create_candidate_revoked_source_raises_valueerror(db_path: Path) -> None:
    source = _approved_source(db_path)
    db.transition_skill_source(
        db_path, source["id"], expected_version=1, to_status="revoked", actor="alice",
    )
    with pytest.raises(ValueError, match="not approved"):
        db.create_skill_candidate(
            db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
        )


def test_create_candidate_rejects_empty_name(db_path: Path) -> None:
    source = _approved_source(db_path)
    with pytest.raises(ValueError):
        db.create_skill_candidate(
            db_path, source_id=source["id"], name="  ", kind="mcp_server", content_hash="h",
        )


def test_create_candidate_rejects_bad_kind(db_path: Path) -> None:
    source = _approved_source(db_path)
    with pytest.raises(ValueError):
        db.create_skill_candidate(
            db_path, source_id=source["id"], name="x", kind="nonsense", content_hash="h",
        )


def test_create_candidate_rejects_empty_content_hash(db_path: Path) -> None:
    source = _approved_source(db_path)
    with pytest.raises(ValueError):
        db.create_skill_candidate(
            db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="  ",
        )


def test_create_candidate_writes_registered_log_line(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    log = db.list_skill_acquisition_log(db_path, item["id"])
    assert len(log) == 1
    assert log[0]["action"] == "registered"
    assert log[0]["to_status"] == "candidate"


def test_list_skill_items_filters_by_every_field_and_pages(db_path: Path) -> None:
    source_a = _approved_source(db_path, origin="a")
    source_b = _approved_source(db_path, origin="b")
    db.create_skill_candidate(
        db_path, source_id=source_a["id"], name="one", kind="mcp_server",
        content_hash="h1", task_class="code_review",
    )
    db.create_skill_candidate(
        db_path, source_id=source_a["id"], name="two", kind="cli_tool",
        content_hash="h2", task_class="code_review",
    )
    db.create_skill_candidate(
        db_path, source_id=source_b["id"], name="three", kind="mcp_server",
        content_hash="h3", task_class="triage",
    )

    assert len(db.list_skill_items(db_path)) == 3
    assert len(db.list_skill_items(db_path, source_id=source_a["id"])) == 2
    assert len(db.list_skill_items(db_path, kind="cli_tool")) == 1
    assert len(db.list_skill_items(db_path, task_class="triage")) == 1
    assert len(db.list_skill_items(db_path, status="candidate")) == 3
    assert len(db.list_skill_items(db_path, status="acquired")) == 0
    page = db.list_skill_items(db_path, limit=1, offset=0)
    assert len(page) == 1
    page2 = db.list_skill_items(db_path, limit=1, offset=1)
    assert len(page2) == 1
    assert page[0]["id"] != page2[0]["id"]


# --- skill_item lifecycle: claim (CAS) before executor, finalize/fail ------


def test_claim_flips_candidate_to_acquiring(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    claimed = db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    assert claimed["status"] == "acquiring"
    assert claimed["lock_version"] == 1


def test_second_concurrent_claim_with_stale_version_loses(db_path: Path) -> None:
    """Two callers racing to claim the same candidate, both starting from the
    version they last read: only the first's compare-and-set may succeed, and
    it must succeed *before* either would invoke an executor — the second's
    write never lands (rejected as a lost update), so it can never reach the
    point of also materialising the skill."""
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    with pytest.raises(db.LostUpdateError):
        db.claim_skill_item(db_path, item["id"], expected_version=0, actor="bob")


def test_second_claim_with_current_version_rejected_by_status(db_path: Path) -> None:
    """A second claim that *does* read the post-claim version still cannot
    win: the row is no longer ``candidate``, so the transition itself is
    disallowed rather than merely version-stale."""
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    claimed = db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    with pytest.raises(InvalidSkillItemTransitionError):
        db.claim_skill_item(
            db_path, item["id"], expected_version=claimed["lock_version"], actor="bob",
        )


def test_finalize_acquisition_flips_to_acquired_and_logs_metadata(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    claimed = db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    finalized = db.finalize_skill_item_acquisition(
        db_path, item["id"], expected_version=claimed["lock_version"], actor="alice",
        detail="no-op", metadata={"mode": "null"},
    )
    assert finalized["status"] == "acquired"
    log = db.list_skill_acquisition_log(db_path, item["id"])
    assert [entry["action"] for entry in log] == ["registered", "acquiring", "acquired"]
    assert log[-1]["metadata"] == {"mode": "null"}


def test_fail_acquisition_reverts_to_candidate_and_logs_failure(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    claimed = db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    reverted = db.fail_skill_item_acquisition(
        db_path, item["id"], expected_version=claimed["lock_version"], actor="alice",
        detail="network denied",
    )
    assert reverted["status"] == "candidate"
    log = db.list_skill_acquisition_log(db_path, item["id"])
    assert log[-1]["action"] == "acquire_failed"
    assert log[-1]["detail"] == "network denied"
    # Retryable: the reverted item can be claimed again.
    db.claim_skill_item(db_path, item["id"], expected_version=reverted["lock_version"], actor="alice")


def test_reject_and_revoke_skill_item(db_path: Path) -> None:
    source = _approved_source(db_path)
    candidate = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    rejected = db.reject_skill_item(
        db_path, candidate["id"], expected_version=0, actor="alice", detail="not useful",
    )
    assert rejected["status"] == "rejected"

    acquired_item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="y", kind="mcp_server", content_hash="h2",
    )
    claimed = db.claim_skill_item(db_path, acquired_item["id"], expected_version=0, actor="alice")
    db.finalize_skill_item_acquisition(
        db_path, acquired_item["id"], expected_version=claimed["lock_version"], actor="alice",
    )
    revoked = db.revoke_skill_item(
        db_path, acquired_item["id"], expected_version=2, actor="alice", detail="bad effect",
    )
    assert revoked["status"] == "revoked"


def test_transition_item_missing_raises_keyerror(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.claim_skill_item(db_path, "nope", expected_version=0, actor="alice")


def test_transition_item_version_mismatch_raises_lost_update(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    with pytest.raises(db.LostUpdateError):
        db.claim_skill_item(db_path, item["id"], expected_version=99, actor="alice")


# --- skill_acquisition_log ---------------------------------------------------


def test_get_missing_item_returns_none(db_path: Path) -> None:
    assert db.get_skill_item(db_path, "nope") is None


def test_log_is_ordered_and_append_only(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    db.claim_skill_item(db_path, item["id"], expected_version=0, actor="alice")
    log = db.list_skill_acquisition_log(db_path, item["id"])
    assert [entry["seq"] for entry in log] == [1, 2]


def test_deleting_skill_item_is_restricted_when_log_rows_exist(db_path: Path) -> None:
    """The audit trail must outlive the row it audits: a hard delete of a
    ``skill_item`` with existing ``skill_acquisition_log``/``skill_outcome``
    rows must fail (``ON DELETE RESTRICT``), never silently cascade the
    evidence away."""
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    db.record_skill_outcome(
        db_path, item["id"], task_id="t-1", used=True, cost_usd=1.0, accepted=True,
    )
    with db.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn):
                conn.execute("DELETE FROM skill_item WHERE id = ?", (item["id"],))
    # Both the item and its audit rows survive the failed delete.
    assert db.get_skill_item(db_path, item["id"]) is not None
    assert len(db.list_skill_acquisition_log(db_path, item["id"])) == 1
    assert len(db.list_skill_outcomes(db_path, item["id"])) == 1


# --- skill_outcome + effect measurement -------------------------------------


def test_record_outcome_missing_item_returns_none(db_path: Path) -> None:
    assert db.record_skill_outcome(
        db_path, "nope", task_id="t-1", used=True, cost_usd=1.0, accepted=True,
    ) is None


def test_record_outcome_rejects_empty_task_id(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    with pytest.raises(ValueError):
        db.record_skill_outcome(
            db_path, item["id"], task_id="  ", used=True, cost_usd=1.0, accepted=True,
        )


def test_list_outcomes_newest_first(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    db.record_skill_outcome(db_path, item["id"], task_id="t-1", used=True, cost_usd=1.0, accepted=True)
    db.record_skill_outcome(db_path, item["id"], task_id="t-2", used=True, cost_usd=2.0, accepted=False)
    outcomes = db.list_skill_outcomes(db_path, item["id"])
    assert [o["task_id"] for o in outcomes] == ["t-2", "t-1"]


def test_get_effect_none_when_no_outcomes(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    effect = db.get_skill_effect(db_path, item["id"])
    assert effect["baseline"]["count"] == 0
    assert effect["baseline"]["avg_cost_usd"] is None
    assert effect["with_skill"]["count"] == 0


def test_get_effect_missing_item_returns_none(db_path: Path) -> None:
    assert db.get_skill_effect(db_path, "nope") is None


def test_get_effect_aggregates_baseline_vs_with_skill(db_path: Path) -> None:
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    # baseline: no skill, higher cost, lower first-pass rate
    db.record_skill_outcome(db_path, item["id"], task_id="b-1", used=False, cost_usd=10.0, accepted=False)
    db.record_skill_outcome(db_path, item["id"], task_id="b-2", used=False, cost_usd=20.0, accepted=True)
    # with-skill: cheaper, always accepted
    db.record_skill_outcome(db_path, item["id"], task_id="w-1", used=True, cost_usd=2.0, accepted=True)
    db.record_skill_outcome(db_path, item["id"], task_id="w-2", used=True, cost_usd=4.0, accepted=True)

    effect = db.get_skill_effect(db_path, item["id"])
    assert effect["baseline"]["count"] == 2
    assert effect["baseline"]["avg_cost_usd"] == pytest.approx(15.0)
    assert effect["baseline"]["first_pass_rate"] == pytest.approx(0.5)
    assert effect["with_skill"]["count"] == 2
    assert effect["with_skill"]["avg_cost_usd"] == pytest.approx(3.0)
    assert effect["with_skill"]["first_pass_rate"] == pytest.approx(1.0)


def test_get_effect_is_not_capped_by_a_fixed_row_limit(db_path: Path) -> None:
    """The aggregates are computed in SQL (``COUNT``/``AVG``), not by fetching
    a fixed-size page of raw rows into Python and averaging that — so the
    measurement stays correct past whatever a list endpoint's page size is."""
    source = _approved_source(db_path)
    item = db.create_skill_candidate(
        db_path, source_id=source["id"], name="x", kind="mcp_server", content_hash="h",
    )
    n = 250  # comfortably past a 100-row default page size
    for i in range(n):
        db.record_skill_outcome(
            db_path, item["id"], task_id=f"w-{i}", used=True, cost_usd=1.0, accepted=True,
        )
    effect = db.get_skill_effect(db_path, item["id"])
    assert effect["with_skill"]["count"] == n
