"""VOYN-W0-AICC-RUNTIME-DB-BLOAT: a live `runtime.db` can hold a handful of
rows across hundreds of megabytes on disk, because `DELETE` (event retention,
or an ordinary task-delete cascade) frees pages inside the file without ever
returning them to the filesystem — only `VACUUM` does that, and it was
previously all-or-nothing opt-in, gated behind retention being configured at
all. These tests cover the new threshold: `AICC_RUNTIME_VACUUM_FREE_RATIO`
triggers an automatic `VACUUM` once the freelist crosses a declared fraction
of the file, independent of whether event retention itself is enabled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center.runtime import db


def _make_run(db_path: Path, name: str) -> dict:
    task = db.create_task(
        db_path, project="AIOS", title=name, task_type="implementation"
    )
    session = db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/repo"
    )
    return db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/repo",
        prompt=name,
        is_resume=False,
    )


def _bloat_and_prune(db_path: Path, *, n_events: int = 4000) -> None:
    """Write a pile of `run_event` rows for a terminal run, then delete them
    with a raw `DELETE` (not `VACUUM`) so the file is left holding freelist
    pages — the exact shape `PRAGMA freelist_count` is meant to detect.

    Bulk-inserted in one transaction (unlike `append_run_event`, which opens
    one transaction per call) purely so the test suite stays fast; the
    resulting rows and the delete path below are otherwise identical to the
    real thing.
    """
    db.migrate(db_path)
    run = _make_run(db_path, "bloat")
    payload_json = json.dumps({"blob": "x" * 512})
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.executemany(
                """INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (run["id"], i, "stream_event", payload_json, now)
                    for i in range(1, n_events + 1)
                ],
            )
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute("DELETE FROM run_event WHERE run_id = ?", (run["id"],))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AICC_RUNTIME_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("AICC_RUNTIME_VACUUM_ON_START", raising=False)
    monkeypatch.delenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, raising=False)


def test_free_page_ratio_is_near_zero_on_a_fresh_db(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    ratio = db.free_page_ratio(db_path)
    assert ratio is not None
    assert ratio < 0.1


def test_free_page_ratio_rises_after_delete_and_falls_after_vacuum(tmp_path):
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)

    bloated_ratio = db.free_page_ratio(db_path)
    assert bloated_ratio is not None
    assert bloated_ratio > 0.5

    with db.connect(db_path) as conn:
        conn.execute("VACUUM")
    assert db.free_page_ratio(db_path) < 0.1


def test_maybe_apply_runtime_retention_is_a_pure_noop_without_any_env_var(tmp_path):
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)
    before = db.free_page_ratio(db_path)

    db.maybe_apply_runtime_retention(db_path)

    assert db.free_page_ratio(db_path) == before


def test_vacuum_free_ratio_threshold_fires_without_retention_days(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)
    assert db.free_page_ratio(db_path) > 0.5

    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, "0.3")

    db.maybe_apply_runtime_retention(db_path)

    assert db.free_page_ratio(db_path) < 0.1


def test_vacuum_free_ratio_threshold_does_not_fire_below_bloat(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)
    before = db.free_page_ratio(db_path)
    assert before is not None

    # A threshold higher than the actual bloat must not trigger VACUUM.
    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, str(min(before + 0.3, 1.0)))

    db.maybe_apply_runtime_retention(db_path)

    assert db.free_page_ratio(db_path) == before


def test_vacuum_free_ratio_threshold_ignores_unusable_values(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)
    before = db.free_page_ratio(db_path)

    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, "not-a-number")
    db.maybe_apply_runtime_retention(db_path)
    assert db.free_page_ratio(db_path) == before

    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, "0")
    db.maybe_apply_runtime_retention(db_path)
    assert db.free_page_ratio(db_path) == before

    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, "1.5")
    db.maybe_apply_runtime_retention(db_path)
    assert db.free_page_ratio(db_path) == before


def test_vacuum_on_start_still_fires_without_retention_days(tmp_path, monkeypatch):
    """`AICC_RUNTIME_VACUUM_ON_START=1` used to be reachable only after a
    successful prune, which itself required `AICC_RUNTIME_RETENTION_DAYS`.
    Bloat from an ordinary task-delete cascade has nothing to do with event
    retention, so the unconditional flag has to work on its own too."""
    db_path = tmp_path / "runtime.db"
    _bloat_and_prune(db_path)
    assert db.free_page_ratio(db_path) > 0.5

    monkeypatch.setenv("AICC_RUNTIME_VACUUM_ON_START", "1")

    db.maybe_apply_runtime_retention(db_path)

    assert db.free_page_ratio(db_path) < 0.1


def test_retention_prune_and_bloat_threshold_compose(tmp_path, monkeypatch):
    """Both knobs set: retention prunes eligible events, then the threshold
    decides whether the resulting freelist is worth a VACUUM."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_run(db_path, "old")
    payload_json = json.dumps({"blob": "x" * 512})
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.executemany(
                """INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (run["id"], i, "stream_event", payload_json, now)
                    for i in range(1, 4001)
                ],
            )
    stale = "2000-01-01T00:00:00"
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (stale, run["id"]),
            )

    monkeypatch.setenv("AICC_RUNTIME_RETENTION_DAYS", "1")
    monkeypatch.setenv(db.RUNTIME_VACUUM_FREE_RATIO_ENV, "0.3")

    db.maybe_apply_runtime_retention(db_path)

    with db.connect(db_path) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM run_event WHERE run_id=?", (run["id"],)
        ).fetchone()["c"]
    assert remaining == 0
    assert db.free_page_ratio(db_path) < 0.1
