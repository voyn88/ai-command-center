"""VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23: timed upgrade window with backup-first rehearsal."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from command_center.runtime import db, schema_upgrade


def _pin_schema(path: Path, monkeypatch, version: int) -> None:
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pinned:
        pinned.setattr(
            db, "MIGRATIONS", [m for m in current_migrations if m[0] <= version]
        )
        pinned.setattr(db, "SCHEMA_VERSION", version)
        db.migrate(path)
    assert db.current_schema_version(path) == version


def _seed_v16_population(path: Path, *, owners: int, digests: int) -> None:
    """Fill the two tables migration 17 indexes — the only size-scaling step in 17..23."""
    now = db.iso_now()
    owner_rows = [
        (f"owner-{i}", f"title-{i}", None, None, 0, None, 0, now, now)
        for i in range(owners)
    ]
    digest_rows = [
        (f"digest-{i}", f"title-{i}", "", "cat", "[]", now, "2026-01-01", i)
        for i in range(digests)
    ]
    with db.connect(path) as conn:
        with db.transaction(conn):
            conn.executemany(
                "INSERT INTO owner_item "
                "(id, title, detail, due, done, source_ref, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                owner_rows,
            )
            conn.executemany(
                "INSERT INTO digest_item "
                "(id, title, body, category, refs_json, created_at, day, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                digest_rows,
            )


def test_apply_pending_migrations_times_each_step_from_v16_to_v23(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-v16.db"
    _pin_schema(path, monkeypatch, 16)
    _seed_v16_population(path, owners=20_000, digests=20_000)

    report = schema_upgrade.apply_pending_migrations(
        path, stop_after_version=23, run_backfill=False
    )

    assert report["initial_version"] == 16
    assert report["final_version"] == 23
    assert report["backfill_run"] is False
    assert report["backfill_inserted"] == 0
    assert report["integrity_check"] == "ok"
    applied = [step["version"] for step in report["steps"]]
    assert applied == [17, 18, 19, 20, 21, 22, 23]
    assert report["total_elapsed_seconds"] < 30.0
    for step in report["steps"]:
        assert step["elapsed_seconds"] < 30.0, (
            f"migration {step['version']} took {step['elapsed_seconds']:.2f}s "
            "over a populated v16 stand-in — investigate before the live window"
        )
    with db.connect(path) as conn:
        owner_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(owner_item)")
        }
        digest_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(digest_item)")
        }
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "project_ref" in owner_cols
    assert "project_ref" in digest_cols
    assert {
        "conflict",
        "audit_run",
        "model_entry",
        "market_item",
        "motion",
        "contact",
    } <= tables


def test_apply_pending_migrations_does_not_backfill_unless_opted_in(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-v16-no-backfill.db"
    _pin_schema(path, monkeypatch, 16)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(
        path, task_id=task["id"], project="AIOS", repository_path="/tmp/x"
    )
    now = db.iso_now()
    with db.connect(path) as conn:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO run (id, session_id, task_id, sequence, state, project, "
                "task_type, repository_path, prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "run-legacy",
                    session["id"],
                    task["id"],
                    1,
                    "COMPLETED",
                    "AIOS",
                    "implementation",
                    "/tmp/x",
                    "prompt",
                    now,
                    now,
                ),
            )
            conn.execute("DELETE FROM run_provenance")

    report = schema_upgrade.apply_pending_migrations(
        path, stop_after_version=23, run_backfill=False
    )
    assert report["backfill_inserted"] == 0
    with db.connect(path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM run_provenance").fetchone()["c"]
    assert count == 0

    report = schema_upgrade.apply_pending_migrations(
        path, stop_after_version=23, run_backfill=True, backfill_limit=10
    )
    assert report["steps"] == []  # already at 23
    assert report["backfill_inserted"] == 1


def test_rehearse_schema_upgrade_backs_up_and_leaves_original_untouched(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-live.db"
    _pin_schema(path, monkeypatch, 16)
    _seed_v16_population(path, owners=500, digests=500)
    before = path.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()

    report = schema_upgrade.rehearse_schema_upgrade(
        path,
        archive_dir=tmp_path / "cold",
        stop_after_version=23,
        run_backfill=False,
    )

    assert report["mode"] == "rehearsal"
    assert report["original_untouched"] is True
    assert report["original_sha256"] == before_sha
    assert path.read_bytes() == before
    assert db.current_schema_version(path) == 16
    assert Path(report["backup_path"]).is_file()
    assert Path(report["rehearsal_db"]).is_file()
    assert db.current_schema_version(Path(report["rehearsal_db"])) == 23
    assert [step["version"] for step in report["steps"]] == list(range(17, 24))


def test_rehearse_refuses_missing_database(tmp_path):
    with pytest.raises(schema_upgrade.SchemaUpgradeError, match="does not exist"):
        schema_upgrade.rehearse_schema_upgrade(
            tmp_path / "missing.db", archive_dir=tmp_path / "cold"
        )


def test_apply_still_gates_v25_without_offline_cutover(tmp_path, monkeypatch):
    path = tmp_path / "runtime-v24.db"
    _pin_schema(path, monkeypatch, 24)
    with pytest.raises(db.FinalizationClaimCutoverRequired, match="explicit offline"):
        schema_upgrade.apply_pending_migrations(path, stop_after_version=None)
    assert db.current_schema_version(path) == 24
