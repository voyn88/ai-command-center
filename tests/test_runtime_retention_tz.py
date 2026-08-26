"""VOYN-W0-AICC-RETENTION-TZ: retention must delete the same *rows*, by id,
regardless of the timezone of the process that happens to run the prune.

`run.completed_at` is a naive local ISO string (`models.iso_now`, deliberately
"local time on the machine that wrote them"). Both retention paths built their
cutoff from a bare `datetime.now()`, i.e. the local zone of *the pruning
process* — a cron job, a container or a service unit started with a different
`TZ` than the app that wrote the rows. The comparison `completed_at < cutoff`
is then shifted by the offset between the two zones, so the same database and
the same wall-clock instant delete a *different set of run_event rows*
depending only on who asked. Deletion is irreversible; this is a data-loss
defect, not a reporting one.

These tests assert on identifiers, never on counts: "the same number of rows
went away" is not evidence that the same rows went away.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from command_center.runtime import db, maintenance

# The zone the *writer* machine is on. Chosen as UTC so the stored naive
# strings are unambiguous in the test's own reasoning.
WRITER_TZ = "UTC"

# Zones a prune process might plausibly be started in. Europe/Moscow is
# permanently UTC+3, America/New_York is UTC-4/-5 — both sides of the writer.
PRUNE_TZS = ["UTC", "Europe/Moscow", "America/New_York"]

RETENTION_DAYS = 30

# Hours relative to the retention boundary. Negative = older than the boundary
# (must be pruned), positive = newer (must survive). The spread is smaller than
# any of the zone offsets under test, so a zone-dependent cutoff necessarily
# moves the boundary across some of these rows.
BOUNDARY_OFFSETS_HOURS = [-4, -1, +1, +4]


@pytest.fixture
def _restore_tz():
    original = os.environ.get("TZ")
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _set_process_tz(name: str) -> None:
    os.environ["TZ"] = name
    time.tzset()


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


def _seed(db_path: Path) -> dict[str, str]:
    """Create one terminal run per boundary offset, written as the writer
    machine (WRITER_TZ) would have written it. Returns label -> run id."""
    _set_process_tz(WRITER_TZ)
    db.migrate(db_path)
    boundary = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    labels: dict[str, str] = {}
    for hours in BOUNDARY_OFFSETS_HOURS:
        label = f"off{hours:+d}h"
        run = _make_run(db_path, label)
        db.append_run_event(db_path, run["id"], "stream_event", {"n": 0})
        stamp = (
            (boundary + timedelta(hours=hours))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
        with db.connect(db_path) as conn:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                    (stamp, run["id"]),
                )
        labels[label] = run["id"]
    return labels


def _snapshot(src: Path, dst: Path) -> None:
    """Copy through the SQLite backup API so WAL content comes along."""
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as destination:
        source.backup(destination)


def _surviving_run_ids(db_path: Path) -> set[str]:
    with db.connect(db_path) as conn:
        return {
            row["run_id"]
            for row in conn.execute("SELECT DISTINCT run_id FROM run_event")
        }


def _pruned_labels(db_path: Path, labels: dict[str, str]) -> set[str]:
    survivors = _surviving_run_ids(db_path)
    return {label for label, run_id in labels.items() if run_id not in survivors}


def test_archive_and_prune_deletes_the_same_rows_in_every_process_tz(
    tmp_path, _restore_tz
):
    seed_db = tmp_path / "seed.db"
    labels = _seed(seed_db)

    pruned_by_tz: dict[str, set[str]] = {}
    for tz in PRUNE_TZS:
        run_db = tmp_path / f"runtime-{tz.replace('/', '_')}.db"
        _snapshot(seed_db, run_db)
        _set_process_tz(tz)
        maintenance.archive_and_prune(
            run_db,
            retention_days=RETENTION_DAYS,
            archive_dir=tmp_path / f"cold-{tz.replace('/', '_')}",
        )
        pruned_by_tz[tz] = _pruned_labels(run_db, labels)

    # The rows older than the boundary, and only those.
    expected = {"off-4h", "off-1h"}
    assert pruned_by_tz == {tz: expected for tz in PRUNE_TZS}


def test_apply_runtime_retention_deletes_the_same_rows_in_every_process_tz(
    tmp_path, _restore_tz
):
    seed_db = tmp_path / "seed.db"
    labels = _seed(seed_db)

    pruned_by_tz: dict[str, set[str]] = {}
    for tz in PRUNE_TZS:
        run_db = tmp_path / f"runtime-{tz.replace('/', '_')}.db"
        _snapshot(seed_db, run_db)
        _set_process_tz(tz)
        db.apply_runtime_retention(run_db, retention_days=RETENTION_DAYS)
        pruned_by_tz[tz] = _pruned_labels(run_db, labels)

    expected = {"off-4h", "off-1h"}
    assert pruned_by_tz == {tz: expected for tz in PRUNE_TZS}


def test_apply_runtime_retention_batches_instead_of_one_unbounded_delete(
    tmp_path, monkeypatch
):
    """A bare `DELETE ... WHERE run_id IN (...)` with no bound holds its write
    lock for as long as the whole unpruned backlog takes to delete -- exactly
    the worst moment for the longest lock hold, since retention only has real
    work to do on a long-neglected database. Force a batch size far smaller
    than the row count and check it actually ran in multiple bounded
    transactions, not one pass that merely produced the right end state."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    task = db.create_task(
        db_path, project="AIOS", title="old", task_type="implementation"
    )
    session = db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/repo"
    )
    run = db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/repo",
        prompt="old",
        is_resume=False,
    )
    for i in range(11):
        db.append_run_event(db_path, run["id"], "stream_event", {"n": i})
    stale = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (stale, run["id"]),
            )

    monkeypatch.setattr(db, "_RETENTION_BATCH_SIZE", 3)
    transaction_calls = {"n": 0}
    real_transaction = db.transaction

    @contextmanager
    def spying_transaction(conn):
        transaction_calls["n"] += 1
        with real_transaction(conn) as c:
            yield c

    monkeypatch.setattr(db, "transaction", spying_transaction)

    removed = db.apply_runtime_retention(db_path, retention_days=30)

    assert removed == 11
    # 11 rows at a batch size of 3: four bounded transactions, not one.
    assert transaction_calls["n"] == 4
    with db.connect(db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM run_event").fetchone()
        assert n == 0


def test_report_records_the_zone_the_cutoff_was_rendered_in(tmp_path, _restore_tz):
    """An irreversible delete must say, in its own report, which clock it
    judged the rows against — otherwise the operator cannot audit the set."""
    seed_db = tmp_path / "seed.db"
    _seed(seed_db)
    _set_process_tz("America/New_York")

    report = maintenance.archive_and_prune(
        seed_db, retention_days=RETENTION_DAYS, archive_dir=tmp_path / "cold"
    )

    assert report["cutoff_timezone"] == WRITER_TZ
    assert report["cutoff_timezone_source"] == "database"
