"""VOYN-W0-AICC-RETENTION-TZ: retention must delete the same *rows*, by id,
regardless of the timezone of the process that happens to run the prune.

`run.completed_at` is a naive ISO string with no offset (`models.iso_now`).
Both retention paths built their cutoff from a bare `datetime.now()`, i.e. the
local zone of *the pruning process* — a cron job, a container or a service unit
started with a different `TZ` than the app that wrote the rows. The comparison
`completed_at < cutoff` is then shifted by the offset between the two zones, so
the same database and the same wall-clock instant delete a *different set of
run_event rows* depending only on who asked. Deletion is irreversible; this is
a data-loss defect, not a reporting one.

Since `VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL` those strings are naive **UTC**, and a
database that was already in use holds rows on two clocks. The second half of
this file covers that case; the tests below it are the original ones, where the
writer and the file agree on one clock.

These tests assert on identifiers, never on counts: "the same number of rows
went away" is not evidence that the same rows went away.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------------------
# The two-clock database (VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL)
#
# `models.iso_now()` now writes naive **UTC**. A file created since then holds
# one clock and the tests above cover it. A file that was *already in use* holds
# two: naive local rows from before the switchover, in the zone its ledger
# recorded, and naive UTC rows after it. Nothing tells them apart by inspection
# — same column, same format — so the cutoff has to satisfy the earlier of the
# two readings. Getting this wrong deletes pre-upgrade rows up to the legacy
# zone's offset early, and deletion is irreversible.
#
# Every test here declares a legacy zone on both sides of UTC, because the
# conservative bound comes from a different candidate in each direction.
# ---------------------------------------------------------------------------

LEGACY_WEST = "America/Los_Angeles"  # UTC-7/-8: its wall clock lags UTC
LEGACY_EAST = "Europe/Moscow"        # UTC+3 permanently: its wall clock leads


def _declare_legacy_zone(db_path: Path, zone: str) -> None:
    """Make a migrated database look like one that was in use before the UTC
    switchover: its ledger names the local zone of the host that wrote its
    rows rather than `"UTC"`."""
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"UPDATE schema_version SET {db.LEDGER_TIMESTAMP_TZ_COLUMN} = ?",
                (zone,),
            )


def _clear_declared_zone(db_path: Path) -> None:
    """A database that declares nothing — one that predates the column, or a
    host whose zone could not be named."""
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"UPDATE schema_version SET {db.LEDGER_TIMESTAMP_TZ_COLUMN} = NULL"
            )


def _naive_in(instant: datetime, zone: str | None) -> str:
    """`instant` as the naive wall-clock string a writer on `zone` would have
    recorded for it (`None` = UTC, i.e. what this app writes today)."""
    there = instant if zone is None else instant.astimezone(ZoneInfo(zone))
    return there.replace(tzinfo=None).isoformat(timespec="seconds")


def _add_terminal_run(db_path: Path, label: str, completed_at: str) -> str:
    """One terminal run with one event, completed at the given stored string."""
    run = _make_run(db_path, label)
    db.append_run_event(db_path, run["id"], "stream_event", {"n": 0})
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (completed_at, run["id"]),
            )
    return run["id"]


@pytest.fixture
def _no_tz_override(monkeypatch):
    """`AICC_RUNTIME_TZ` outranks the ledger; these tests are about the ledger."""
    monkeypatch.delenv(db.RETENTION_TZ_ENV, raising=False)


@pytest.mark.parametrize("prune_tz", PRUNE_TZS)
@pytest.mark.parametrize("legacy_tz", [LEGACY_WEST, LEGACY_EAST])
def test_a_pre_switchover_row_survives_its_full_window(
    tmp_path, _restore_tz, _no_tz_override, legacy_tz, prune_tz
):
    """The regression this file exists for, in its post-UTC form.

    A row written before the switchover is local time. Judging it against a
    UTC-rendered cutoff moves the boundary by the writer's offset, so a row
    that is four hours *inside* its retention window is deleted anyway. Only
    the truly-older row may go.
    """
    db_path = tmp_path / f"legacy-{legacy_tz}-{prune_tz}.db".replace("/", "_")
    _set_process_tz(WRITER_TZ)
    db.migrate(db_path)
    _declare_legacy_zone(db_path, legacy_tz)

    boundary = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    inside = _add_terminal_run(
        db_path, "legacy-inside", _naive_in(boundary + timedelta(hours=4), legacy_tz)
    )
    outside = _add_terminal_run(
        db_path, "legacy-outside", _naive_in(boundary - timedelta(days=2), legacy_tz)
    )

    _set_process_tz(prune_tz)
    db.apply_runtime_retention(db_path, retention_days=RETENTION_DAYS)

    survivors = _surviving_run_ids(db_path)
    assert inside in survivors
    assert outside not in survivors


@pytest.mark.parametrize("prune_tz", PRUNE_TZS)
def test_a_post_switchover_row_survives_its_full_window_on_the_same_file(
    tmp_path, _restore_tz, _no_tz_override, prune_tz
):
    """The other half of the same file: rows written *after* the switchover are
    UTC, and a cutoff rendered only in the legacy zone would prune those early
    instead. Both readings have to be satisfied at once."""
    db_path = tmp_path / f"mixed-{prune_tz}.db".replace("/", "_")
    _set_process_tz(WRITER_TZ)
    db.migrate(db_path)
    _declare_legacy_zone(db_path, LEGACY_EAST)  # the zone that leads UTC

    boundary = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    inside = _add_terminal_run(
        db_path, "utc-inside", _naive_in(boundary + timedelta(hours=4), None)
    )
    outside = _add_terminal_run(
        db_path, "utc-outside", _naive_in(boundary - timedelta(days=2), None)
    )

    _set_process_tz(prune_tz)
    db.apply_runtime_retention(db_path, retention_days=RETENTION_DAYS)

    survivors = _surviving_run_ids(db_path)
    assert inside in survivors
    assert outside not in survivors


def test_the_cost_of_the_conservative_bound_is_bounded_over_retention(
    tmp_path, _restore_tz, _no_tz_override
):
    """Stated as a test because it is the price of the fix, and it is the safe
    direction: on a file with a legacy zone, a row can outlive its window by up
    to that zone's offset. Nothing outlives it by more."""
    db_path = tmp_path / "over-retained.db"
    _set_process_tz(WRITER_TZ)
    db.migrate(db_path)
    _declare_legacy_zone(db_path, LEGACY_WEST)  # UTC-7 in September

    boundary = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    spared = _add_terminal_run(
        db_path, "utc-just-past", _naive_in(boundary - timedelta(hours=4), None)
    )
    pruned = _add_terminal_run(
        db_path, "utc-well-past", _naive_in(boundary - timedelta(hours=12), None)
    )

    db.apply_runtime_retention(db_path, retention_days=RETENTION_DAYS)

    survivors = _surviving_run_ids(db_path)
    assert spared in survivors      # within the offset: kept, deliberately
    assert pruned not in survivors  # past it: the bound holds


@pytest.mark.parametrize("prune_tz", PRUNE_TZS)
def test_the_report_names_the_clock_that_judged_the_rows(
    tmp_path, _restore_tz, _no_tz_override, prune_tz
):
    """An irreversible delete has to say which reading it applied, and say the
    same thing in every pruning process's timezone."""
    _set_process_tz(WRITER_TZ)
    west = tmp_path / "west.db"
    db.migrate(west)
    _declare_legacy_zone(west, LEGACY_WEST)
    east = tmp_path / "east.db"
    db.migrate(east)
    _declare_legacy_zone(east, LEGACY_EAST)
    utc = tmp_path / "utc.db"
    db.migrate(utc)

    _set_process_tz(prune_tz)
    west_cutoff, west_zone, west_source = db.retention_cutoff(
        west, retention_days=RETENTION_DAYS
    )
    east_cutoff, east_zone, east_source = db.retention_cutoff(
        east, retention_days=RETENTION_DAYS
    )
    utc_cutoff, utc_zone, utc_source = db.retention_cutoff(
        utc, retention_days=RETENTION_DAYS
    )

    # West of UTC: the legacy wall clock is the earlier bound, and it is named.
    assert (west_zone, west_source) == (LEGACY_WEST, "database")
    # East of UTC: UTC itself is the earlier bound. The source says so rather
    # than claiming the declared zone rendered a string it did not.
    assert (east_zone, east_source) == ("UTC", db.RETENTION_ZONE_SOURCE_UTC_FLOOR)
    assert east_cutoff == utc_cutoff
    assert west_cutoff < east_cutoff
    # A file with nothing but UTC rows reports its own declaration: the two
    # candidate renderings are the same string, so there is no floor to name.
    assert (utc_zone, utc_source) == ("UTC", "database")


def test_a_new_database_declares_utc_whichever_host_created_it(
    tmp_path, _restore_tz, _no_tz_override
):
    """A file this code created holds only UTC rows, so the zone of the machine
    that happened to create it is not a fact about the file."""
    for tz in ["UTC", "America/New_York", "Europe/Moscow"]:
        _set_process_tz(tz)
        db_path = tmp_path / f"fresh-{tz.replace('/', '_')}.db"
        db.migrate(db_path)

        assert db.resolve_timestamp_zone(db_path) == ("UTC", "database")


def test_a_database_that_predates_the_switchover_declares_its_hosts_zone(
    tmp_path, _restore_tz, _no_tz_override
):
    """The inverse, and the one that protects old rows: an *existing* file may
    already hold naive local ones, so stamping it `"UTC"` would licence exactly
    the early deletion this fix prevents."""
    _set_process_tz("America/New_York")
    db_path = tmp_path / "in-use-before-the-switchover.db"
    db.migrate(db_path)
    _clear_declared_zone(db_path)  # a v24+ file that was never stamped

    db.migrate(db_path)  # schema already current: this file is not new

    assert db.resolve_timestamp_zone(db_path) == ("America/New_York", "database")


def test_an_operator_override_still_names_the_legacy_zone(
    tmp_path, _restore_tz, monkeypatch
):
    """`AICC_RUNTIME_TZ` is how an operator corrects a file stamped on the
    wrong machine, and it goes through the same two-clock rule."""
    _set_process_tz("UTC")
    db_path = tmp_path / "overridden.db"
    db.migrate(db_path)  # declares UTC
    monkeypatch.setenv(db.RETENTION_TZ_ENV, LEGACY_WEST)

    cutoff, zone, source = db.retention_cutoff(db_path, retention_days=RETENTION_DAYS)

    assert (zone, source) == (LEGACY_WEST, "env")
    assert cutoff < _naive_in(
        datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS), None
    )


@pytest.mark.parametrize("prune_tz", PRUNE_TZS)
def test_an_undeclared_database_is_never_pruned_past_its_old_bound(
    tmp_path, _restore_tz, _no_tz_override, prune_tz
):
    """A file that declares nothing cannot be judged exactly — nobody wrote
    down the zone of its legacy rows. The process clock stays the only guess
    available for them, so the guarantee here is narrower: the bound is never
    *later* than what that guess alone would have produced, i.e. this can only
    delete a subset of what the previous behaviour deleted."""
    db_path = tmp_path / f"undeclared-{prune_tz}.db".replace("/", "_")
    _set_process_tz(WRITER_TZ)
    db.migrate(db_path)
    _clear_declared_zone(db_path)

    _set_process_tz(prune_tz)
    cutoff, _zone, source = db.retention_cutoff(db_path, retention_days=RETENTION_DAYS)
    process_clock_only = (
        datetime.now() - timedelta(days=RETENTION_DAYS)
    ).isoformat(timespec="seconds")

    assert source in ("process-local", db.RETENTION_ZONE_SOURCE_UTC_FLOOR)
    assert cutoff <= process_clock_only
