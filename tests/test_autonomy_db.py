"""Persistence-layer tests for autonomy proposals (migrations 6 and 7).

Covers create/get/list, compare-and-set updates, the structural transition
guard, lifecycle-scoped field freezing, immutable action parameters/evidence,
and append-only event ordering — plus real upgrade/preservation paths."""

from __future__ import annotations

import pytest

from command_center.runtime import autonomy as A
from command_center.runtime import db as runtime_db


@pytest.fixture
def db_path():
    path = runtime_db.resolve_db_path()
    runtime_db.migrate(path)
    return path


def _proposal(db_path, **overrides):
    kwargs = dict(
        kind=A.ProposalKind.TASK_CREATION,
        project="AICC",
        title="Add tests",
        rationale="Coverage gap detected",
        state=A.ProposalState.DRAFT,
        risk_level=A.RiskLevel.LOW,
    )
    kwargs.update(overrides)
    return runtime_db.create_proposal(db_path, **kwargs)


def _migrations_through(version):
    return [item for item in runtime_db.MIGRATIONS if item[0] <= version]


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_schema_migrated_to_9(db_path):
    assert runtime_db.current_schema_version(db_path) == runtime_db.SCHEMA_VERSION


def test_migrate_is_idempotent(db_path):
    runtime_db.migrate(db_path)
    assert runtime_db.current_schema_version(db_path) == runtime_db.SCHEMA_VERSION


def test_v5_to_v6_migration_preserves_runtime_and_completion_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime-v5.db"
    all_migrations = list(runtime_db.MIGRATIONS)
    monkeypatch.setattr(runtime_db, "MIGRATIONS", _migrations_through(5))
    runtime_db.migrate(db_path)
    assert runtime_db.current_schema_version(db_path) == 5

    task = runtime_db.create_task(
        db_path, project="AICC", title="preserve", task_type="implementation"
    )
    session = runtime_db.create_session(
        db_path,
        task_id=task["id"],
        project="AICC",
        repository_path="/tmp/preserve",
    )
    run = runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AICC",
        task_type="implementation",
        repository_path="/tmp/preserve",
        prompt="preserve me",
        is_resume=False,
    )
    completion = runtime_db.create_completion(
        db_path,
        run_id=run["id"],
        task_id=task["id"],
        session_id=session["id"],
        project="AICC",
        repository_path="/tmp/preserve",
        completion_state="EXECUTION_FINISHED",
    )

    monkeypatch.setattr(
        runtime_db,
        "MIGRATIONS",
        [item for item in all_migrations if item[0] <= 6],
    )
    runtime_db.migrate(db_path)

    assert runtime_db.current_schema_version(db_path) == 6
    assert runtime_db.get_task(db_path, task["id"])["title"] == "preserve"
    assert runtime_db.get_session(db_path, session["id"])["repository_path"] == "/tmp/preserve"
    assert runtime_db.get_run(db_path, run["id"])["prompt"] == "preserve me"
    assert runtime_db.get_completion(db_path, completion["run_id"])["completion_state"] == (
        "EXECUTION_FINISHED"
    )
    assert runtime_db.list_proposals(db_path) == []


def test_v6_to_v7_migration_backfills_parameters_and_preserves_proposal_children(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime-v6.db"
    all_migrations = list(runtime_db.MIGRATIONS)
    monkeypatch.setattr(runtime_db, "MIGRATIONS", _migrations_through(6))
    runtime_db.migrate(db_path)
    assert runtime_db.current_schema_version(db_path) == 6

    task = runtime_db.create_task(
        db_path, project="AICC", title="legacy proposal task", task_type="implementation"
    )
    now = "2026-07-23T12:00:00"
    with runtime_db.connect(db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute(
                """INSERT INTO proposal
                       (id, kind, project, task_id, title, rationale, state,
                        risk_level, requires_human, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "proposal-v6",
                    A.ProposalKind.TASK_CREATION,
                    "AICC",
                    task["id"],
                    "legacy",
                    "created before parameters_json",
                    A.ProposalState.DRAFT,
                    A.RiskLevel.LOW,
                    1,
                    0,
                    now,
                    now,
                ),
            )
    runtime_db.append_proposal_evidence(
        db_path,
        "proposal-v6",
        kind="gap",
        source="migration-test",
        summary="preserve evidence",
        observed_at=now,
    )
    runtime_db.append_proposal_event(
        db_path,
        "proposal-v6",
        A.EventType.CREATED,
        to_state=A.ProposalState.DRAFT,
    )

    # This test proves the v6 -> v7 data migration only.  Crossing the later
    # v24 -> v25 boundary deliberately requires the explicit offline cutover,
    # so do not accidentally turn this historical migration test into an
    # unconfirmed production cutover.
    monkeypatch.setattr(
        runtime_db,
        "MIGRATIONS",
        [migration for migration in all_migrations if migration[0] <= 7],
    )
    runtime_db.migrate(db_path)

    assert runtime_db.current_schema_version(db_path) == 7
    proposal = runtime_db.get_proposal(db_path, "proposal-v6")
    assert proposal["parameters_json"] == "{}"
    assert proposal["task_id"] == task["id"]
    assert len(runtime_db.list_proposal_evidence(db_path, proposal["id"])) == 1
    assert len(runtime_db.list_proposal_events(db_path, proposal["id"])) == 1
    with runtime_db.connect(db_path) as conn:
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(proposal)")}
    assert columns["parameters_json"]["notnull"] == 1
    assert columns["parameters_json"]["dflt_value"] == "'{}'"


# --------------------------------------------------------------------------
# Create / get / list
# --------------------------------------------------------------------------


def test_create_and_get_proposal(db_path):
    row = _proposal(
        db_path,
        parameters_json='{"task_type": "implementation", "repository_path": "/tmp/r"}',
    )
    assert row["version"] == 0
    assert row["requires_human"] == 1
    assert row["parameters_json"] == (
        '{"repository_path":"/tmp/r","task_type":"implementation"}'
    )
    fetched = runtime_db.get_proposal(db_path, row["id"])
    assert fetched["state"] == A.ProposalState.DRAFT
    assert fetched["rationale"] == "Coverage gap detected"
    assert fetched["risk_level"] == A.RiskLevel.LOW


@pytest.mark.parametrize("parameters_json", ["[]", '"scalar"', "not-json"])
def test_create_proposal_rejects_non_object_parameters(db_path, parameters_json):
    with pytest.raises(ValueError):
        _proposal(db_path, parameters_json=parameters_json)


def test_create_proposal_rejects_blank_rationale(db_path):
    with pytest.raises(ValueError):
        _proposal(db_path, rationale="")
    with pytest.raises(ValueError):
        _proposal(db_path, rationale="   ")


def test_get_missing_proposal_returns_none(db_path):
    assert runtime_db.get_proposal(db_path, "nope") is None


def test_list_proposals_filters(db_path):
    a = _proposal(db_path, project="AICC", kind=A.ProposalKind.TASK_CREATION)
    b = _proposal(db_path, project="AIOS", kind=A.ProposalKind.MERGE, risk_level=A.RiskLevel.CRITICAL)
    ids = {r["id"] for r in runtime_db.list_proposals(db_path)}
    assert ids == {a["id"], b["id"]}
    assert [r["id"] for r in runtime_db.list_proposals(db_path, project="AIOS")] == [b["id"]]
    assert [r["id"] for r in runtime_db.list_proposals(db_path, kind=A.ProposalKind.MERGE)] == [b["id"]]
    drafts = {r["id"] for r in runtime_db.list_proposals(db_path, states=[A.ProposalState.DRAFT])}
    assert drafts == {a["id"], b["id"]}


def test_list_proposals_empty_states_returns_empty(db_path):
    _proposal(db_path)
    assert runtime_db.list_proposals(db_path, states=[]) == []


def test_list_proposals_negative_limit_raises(db_path):
    with pytest.raises(ValueError):
        runtime_db.list_proposals(db_path, limit=-1)


# --------------------------------------------------------------------------
# Compare-and-set update + guards
# --------------------------------------------------------------------------


def test_update_proposal_bumps_version(db_path):
    row = _proposal(db_path)
    updated = runtime_db.update_proposal(
        db_path, row["id"], expected_version=0, fields={"state": A.ProposalState.PROPOSED}
    )
    assert updated["version"] == 1
    assert updated["state"] == A.ProposalState.PROPOSED


def test_update_proposal_lost_update_raises(db_path):
    row = _proposal(db_path)
    runtime_db.update_proposal(db_path, row["id"], expected_version=0,
                               fields={"state": A.ProposalState.PROPOSED})
    with pytest.raises(runtime_db.LostUpdateError):
        runtime_db.update_proposal(db_path, row["id"], expected_version=0,
                                   fields={"title": "stale writer"})


def test_update_proposal_illegal_transition_rejected(db_path):
    row = _proposal(db_path)  # DRAFT
    with pytest.raises(runtime_db.InvalidProposalTransitionError):
        runtime_db.update_proposal(db_path, row["id"], expected_version=0,
                                   fields={"state": A.ProposalState.EXECUTED})


def test_update_proposal_cas_precedes_transition_guard(db_path):
    row = _proposal(db_path)
    runtime_db.update_proposal(
        db_path,
        row["id"],
        expected_version=0,
        fields={"state": A.ProposalState.PROPOSED},
    )
    # A stale caller always loses as stale, even when its target is illegal
    # against the winner's newer state.
    with pytest.raises(runtime_db.LostUpdateError):
        runtime_db.update_proposal(db_path, row["id"], expected_version=999,
                                   fields={"state": A.ProposalState.EXECUTED})
    # A current caller still gets the structural state-machine error.
    with pytest.raises(runtime_db.InvalidProposalTransitionError):
        runtime_db.update_proposal(
            db_path,
            row["id"],
            expected_version=1,
            fields={"state": A.ProposalState.EXECUTED},
        )


def test_update_proposal_unknown_field_rejected(db_path):
    row = _proposal(db_path)
    with pytest.raises(runtime_db.UnknownRunFieldError):
        runtime_db.update_proposal(db_path, row["id"], expected_version=0,
                                   fields={"kind": "hacked"})


def test_persisted_assessment_marker_freezes_authority_before_state_routing(db_path):
    row = _proposal(db_path)
    assessed = runtime_db.update_proposal(
        db_path,
        row["id"],
        expected_version=0,
        fields={"eligibility_json": '{"decision":"ELIGIBLE"}'},
    )

    with pytest.raises(runtime_db.ProposalFieldFrozenError):
        runtime_db.update_proposal(
            db_path,
            row["id"],
            expected_version=assessed["version"],
            fields={"parameters_json": '{"task_type":"different"}'},
        )


def test_update_missing_proposal_raises_keyerror(db_path):
    with pytest.raises(KeyError):
        runtime_db.update_proposal(db_path, "nope", expected_version=0,
                                   fields={"title": "x"})


def test_same_state_metadata_update_allowed(db_path):
    row = _proposal(db_path)
    updated = runtime_db.update_proposal(db_path, row["id"], expected_version=0,
                                         fields={"state": A.ProposalState.DRAFT, "last_reason_code": "X"})
    assert updated["version"] == 1
    assert updated["last_reason_code"] == "X"


# --------------------------------------------------------------------------
# Evidence — append-only, immutable, ordered
# --------------------------------------------------------------------------


def test_evidence_is_appended_and_ordered(db_path):
    row = _proposal(db_path)
    s1 = runtime_db.append_proposal_evidence(db_path, row["id"], kind="git", source="git_info",
                                             summary="clean", observed_at="2026-07-23T12:00:00",
                                             data={"dirty": False})
    s2 = runtime_db.append_proposal_evidence(db_path, row["id"], kind="gap", source="pi",
                                             summary="missing", observed_at="2026-07-23T12:00:01",
                                             is_blocker=True)
    assert (s1, s2) == (1, 2)
    items = runtime_db.list_proposal_evidence(db_path, row["id"])
    assert [i["seq"] for i in items] == [1, 2]
    assert items[0]["data"] == {"dirty": False}
    assert items[0]["is_blocker"] is False
    assert items[1]["is_blocker"] is True


# --------------------------------------------------------------------------
# Events — append-only audit trail
# --------------------------------------------------------------------------


def test_events_are_appended_and_ordered(db_path):
    row = _proposal(db_path)
    runtime_db.append_proposal_event(db_path, row["id"], A.EventType.CREATED,
                                     to_state=A.ProposalState.DRAFT, message="created")
    runtime_db.append_proposal_event(db_path, row["id"], A.EventType.TRANSITION,
                                     from_state=A.ProposalState.DRAFT, to_state=A.ProposalState.PROPOSED,
                                     actor="engine", reason_code="X", metadata={"k": "v"})
    events = runtime_db.list_proposal_events(db_path, row["id"])
    assert [e["seq"] for e in events] == [1, 2]
    assert events[1]["actor"] == "engine"
    assert events[1]["metadata"] == {"k": "v"}
    assert events[0]["metadata"] is None


def test_task_delete_sets_null_and_proposal_delete_cascades_children(db_path):
    task = runtime_db.create_task(db_path, project="AICC", title="t", task_type="implementation")
    row = _proposal(db_path, task_id=task["id"])
    runtime_db.append_proposal_evidence(
        db_path,
        row["id"],
        kind="gap",
        source="test",
        summary="preserve until proposal deletion",
        observed_at="2026-07-23T12:00:00",
    )
    runtime_db.append_proposal_event(db_path, row["id"], A.EventType.CREATED)
    with runtime_db.connect(db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute("DELETE FROM task WHERE id = ?", (task["id"],))

    fetched = runtime_db.get_proposal(db_path, row["id"])
    assert fetched["task_id"] is None
    assert len(runtime_db.list_proposal_evidence(db_path, row["id"])) == 1
    assert len(runtime_db.list_proposal_events(db_path, row["id"])) == 1

    with runtime_db.connect(db_path) as conn:
        with runtime_db.transaction(conn):
            conn.execute("DELETE FROM proposal WHERE id = ?", (row["id"],))
        evidence_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proposal_evidence WHERE proposal_id = ?",
            (row["id"],),
        ).fetchone()["n"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proposal_event WHERE proposal_id = ?",
            (row["id"],),
        ).fetchone()["n"]
    assert runtime_db.get_proposal(db_path, row["id"]) is None
    assert evidence_count == 0
    assert event_count == 0
