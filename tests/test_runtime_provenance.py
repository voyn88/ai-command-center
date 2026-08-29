from __future__ import annotations

import pytest

from command_center import run_lineage as provenance
from command_center.runtime import db


def _run(db_path, **identity):
    task = db.create_task(
        db_path,
        project="AICC",
        title="provenance",
        task_type="implementation",
    )
    session = db.create_session(
        db_path,
        task_id=task["id"],
        project="AICC",
        repository_path="/worktrees/aicc/task",
    )
    return db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AICC",
        task_type="implementation",
        repository_path="/worktrees/aicc/task",
        prompt="implement",
        is_resume=False,
        canonical_repository_path=identity.get("repository_path"),
        worktree_path=identity.get("worktree_path", "/worktrees/aicc/task"),
        branch=identity.get("branch"),
        base_branch=identity.get("base_branch"),
        base_sha=identity.get("base_sha"),
        head_sha=identity.get("head_sha"),
    )


def test_new_run_persists_identity_and_explicit_unknowns(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _run(
        db_path,
        repository_path="/repos/aicc",
        branch="feature/provenance",
        base_branch="main",
        base_sha="a" * 40,
        head_sha="a" * 40,
    )

    view = provenance.get_view(db_path, run["id"])

    assert view["task_id"] == run["task_id"]
    assert view["repository"] == "/repos/aicc"
    assert view["worktree"] == "/worktrees/aicc/task"
    assert view["branch"] == "feature/provenance"
    assert view["base_sha"] == "a" * 40
    assert view["head_sha"] == "a" * 40
    assert {"pr", "ci", "accepted_sha", "deployed_sha"} <= set(view["unknown_fields"])


def test_legacy_backfill_is_bounded_idempotent_and_honest(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    first = _run(db_path)
    second = _run(db_path)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute("DELETE FROM run_provenance")

    assert db.backfill_run_provenance(db_path, limit=1) == 1
    assert db.backfill_run_provenance(db_path, limit=1) == 1
    assert db.backfill_run_provenance(db_path, limit=1) == 0

    for run in (first, second):
        view = provenance.get_view(db_path, run["id"])
        assert view["worktree"] == "/worktrees/aicc/task"
        assert view["repository"] is None
        assert "repository" in view["unknown_fields"]
        assert "base_sha" in view["unknown_fields"]


def test_pr_and_completed_ci_are_bound_to_exact_run_and_head(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _run(db_path, head_sha="b" * 40)

    provenance.observe_pull_request(
        db_path,
        run["id"],
        number=165,
        url="https://github.example/pr/165",
        head_sha="c" * 40,
        checks=[
            {"name": "linux", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "windows", "status": "IN_PROGRESS", "conclusion": None},
            {"name": "boundary", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
        observed_at="2026-08-08T16:00:00Z",
    )

    view = provenance.get_view(db_path, run["id"])
    assert view["pr"] == {
        "number": 165,
        "url": "https://github.example/pr/165",
        "head_sha": "c" * 40,
    }
    assert view["ci"] == [
        {"name": "boundary", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "linux", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert "pr" not in view["unknown_fields"]
    assert "ci" not in view["unknown_fields"]


def test_acceptance_and_deployment_evidence_are_immutable_and_verified(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _run(db_path, head_sha="d" * 40)

    with pytest.raises(provenance.UnverifiedEvidenceError):
        provenance.record_acceptance(
            db_path, run["id"], accepted_sha="d" * 40, target_verified=False
        )
    accepted = provenance.record_acceptance(
        db_path,
        run["id"],
        accepted_sha="d" * 40,
        target_verified=True,
        observed_at="2026-08-08T17:00:00Z",
    )
    assert accepted["accepted_sha"] == "d" * 40
    assert provenance.record_acceptance(
        db_path, run["id"], accepted_sha="d" * 40, target_verified=True
    )["accepted_sha"] == "d" * 40
    with pytest.raises(provenance.ImmutableEvidenceError):
        provenance.record_acceptance(
            db_path, run["id"], accepted_sha="e" * 40, target_verified=True
        )

    with pytest.raises(provenance.UnverifiedEvidenceError):
        provenance.record_deployment(
            db_path,
            run["id"],
            deployed_sha="d" * 40,
            environment="production",
            target_verified=False,
        )
    deployed = provenance.record_deployment(
        db_path,
        run["id"],
        deployed_sha="d" * 40,
        environment="production",
        target_verified=True,
        observed_at="2026-08-08T18:00:00Z",
    )
    assert deployed["deployed_sha"] == "d" * 40
    assert deployed["deployment_environment"] == "production"
    with pytest.raises(provenance.ImmutableEvidenceError):
        provenance.record_deployment(
            db_path,
            run["id"],
            deployed_sha="f" * 40,
            environment="production",
            target_verified=True,
        )


def test_migration_preserves_run_history_and_backfills_missing_rows(tmp_path):
    db_path = tmp_path / "runtime.db"
    original_migrations = db.MIGRATIONS
    original_version = db.SCHEMA_VERSION
    try:
        db.MIGRATIONS = [step for step in original_migrations if step[0] < original_version]
        db.SCHEMA_VERSION = original_version - 1
        db.migrate(db_path)
        legacy = _run(db_path)
        with db.connect(db_path) as conn:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE run SET state = 'COMPLETED', completed_at = ?, "
                    "finalized_at = ? WHERE id = ?",
                    (db.iso_now(), db.iso_now(), legacy["id"]),
                )
    finally:
        db.MIGRATIONS = original_migrations
        db.SCHEMA_VERSION = original_version

    db.migrate(db_path)
    db.migrate(db_path)

    assert db.get_run(db_path, legacy["id"])["prompt"] == "implement"
    assert provenance.get_view(db_path, legacy["id"])["worktree"] == "/worktrees/aicc/task"
    with db.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_provenance WHERE run_id = ?", (legacy["id"],)
        ).fetchone()[0]
    assert count == 1


def test_github_evidence_rejects_green_checks_for_the_wrong_sha_and_drafts():
    adapter = provenance.GitHubEvidenceAdapter()
    wrong = adapter.normalize(
        candidate_sha="a" * 40,
        payload={
            "draft": False,
            "accepted": True,
            "head_sha": "b" * 40,
            "checks": [{"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    )
    assert wrong["accepted_sha"] is None
    assert wrong["status"] == "rejected_sha_mismatch"

    draft = adapter.normalize(
        candidate_sha="a" * 40,
        payload={
            "draft": True,
            "accepted": True,
            "head_sha": "a" * 40,
            "checks": [{"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    )
    assert draft["status"] == "unaccepted"
    assert draft["accepted_sha"] is None


def test_github_acceptance_gate_binds_the_exact_candidate_sha():
    event = provenance.GitHubEvidenceAdapter().normalize(
        candidate_sha="a" * 40,
        payload={
            "draft": False,
            "accepted": True,
            "head_sha": "a" * 40,
            "checks": [
                {"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "boundary", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        },
    )
    assert event["status"] == "accepted"
    assert event["accepted_sha"] == "a" * 40
    assert event["reported_sha"] == "a" * 40


def test_runtime_probe_never_promotes_health_or_journey_to_sha_evidence():
    adapter = provenance.RuntimeProbeAdapter()
    health = adapter.normalize(
        candidate_sha="a" * 40,
        payload={"health": "ok", "journey": "passed"},
    )
    assert health["reported_sha"] is None
    assert health["deployment_status"] == "unknown"
    assert health["journey_verified"] is True

    mismatch = adapter.normalize(
        candidate_sha="a" * 40,
        payload={"health": "ok", "immutable_sha": "b" * 40},
    )
    assert mismatch["status"] == "runtime_sha_mismatch"
    assert mismatch["deployment_status"] == "unverified"


@pytest.mark.parametrize("source", ["main", "ci", "package_checksum", "db_checksum", "health"])
def test_deployment_adapter_rejects_non_target_evidence(source):
    event = provenance.DeploymentEvidenceAdapter().normalize(
        candidate_sha="a" * 40,
        payload={"source": source, "sha": "a" * 40, "status": "success", "target_verified": True},
    )
    assert event["deployment_status"] == "unverified"
    assert event["deployed_sha"] is None


def test_deployment_adapter_requires_target_verification_and_exact_sha():
    adapter = provenance.DeploymentEvidenceAdapter()
    no_target = adapter.normalize(
        candidate_sha="a" * 40,
        payload={"source": "github_deployment", "sha": "a" * 40, "status": "success"},
    )
    assert no_target["deployment_status"] == "unverified"

    verified = adapter.normalize(
        candidate_sha="a" * 40,
        payload={
            "source": "signed_target_manifest",
            "sha": "a" * 40,
            "signed": True,
            "target_verified": True,
        },
    )
    assert verified["deployment_status"] == "verified"
    assert verified["deployed_sha"] == "a" * 40


def test_native_evidence_upsert_is_idempotent_and_roundtrips_integrity_id(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _run(db_path)
    native = {"event_id": "esf-event-1", "health": "ok", "journey": "passed"}
    event = provenance.DomainEvidenceAdapter("esf_runtime").normalize(
        candidate_sha="a" * 40, payload=native
    )

    first = provenance.upsert_native_event(db_path, run["id"], event)
    second = provenance.upsert_native_event(db_path, run["id"], event)

    assert first == second
    assert first["integrity_id"] == "esf-event-1"
    assert first["native_payload"] == native


def test_canonical_view_projects_safe_runtime_evidence_without_native_payload(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _run(db_path, head_sha="a" * 40)
    event = provenance.RuntimeProbeAdapter().normalize(
        candidate_sha="a" * 40,
        payload={
            "health": "ok",
            "immutable_sha": "b" * 40,
            "prompt": "must-not-reach-dashboard",
        },
    )
    provenance.upsert_native_event(db_path, run["id"], event)

    view = provenance.get_view(db_path, run["id"])

    assert view["evidence"] == [
        {
            "integrity_id": event["integrity_id"],
            "adapter": "runtime_probe",
            "status": "runtime_sha_mismatch",
            "candidate_sha": "a" * 40,
            "reported_sha": "b" * 40,
            "observed_at": event["observed_at"],
        }
    ]
    assert "must-not-reach-dashboard" not in repr(view)
