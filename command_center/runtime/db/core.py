"""Shared core of the runtime store: paths, run-state vocabulary,
exception types, busy/locked retry, connection/transaction management and
migration driver. Split out of the former single-file ``runtime/db.py``
(pure move; the ``db`` package __init__ is the facade).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


import command_center.runtime.db as db  # facade (late-bound; see docstring)


def _new_session_id() -> str:
    """A canonical, dashed UUID string (`8-4-4-4-12`) — unlike `models.new_id`
    (a bare 32-char hex digest, used for `task`/`run` ids which are never
    handed to the `claude` CLI), `session.id` *is* the value passed straight
    to `claude --session-id`/`claude --resume`, and `claude` rejects anything
    that isn't a valid UUID string (verified against the real CLI during
    Sprint 1 end-to-end validation)."""
    return str(uuid.uuid4())

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent


def resolve_db_path(root: Path | None = None) -> Path:
    """`<data dir>/runtime.db`, honoring `AICC_DATA_DIR` like every other module."""
    data_dir = db.storage.resolve_data_dir(root or db.ROOT)
    return data_dir / "runtime.db"


# --------------------------------------------------------------------------
# Run / session states
# --------------------------------------------------------------------------

RUN_STATES: list[str] = [
    "PREPARED",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
    "UNKNOWN",
]

TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "UNKNOWN"})

EXECUTION_CENTER_ACTIVE_STATES: frozenset[str] = frozenset({"PREPARED", "QUEUED", "RUNNING"})

# Explicit allow-list of state transitions. Anything not listed here is refused
# by `update_run_state` before it ever reaches SQL — in particular, no terminal
# state can transition anywhere, so a terminal run can never be silently moved
# back to RUNNING (or anywhere else).
#
# PREPARED/QUEUED both also allow INTERRUPTED/UNKNOWN — the same crash-
# recovery targets RUNNING already allowed — because `Supervisor.reconcile()`
# now inspects every `EXECUTION_CENTER_ACTIVE_STATES` row at startup, not just
# RUNNING ones: a Supervisor process can crash between `create_run` (PREPARED)
# or the QUEUED transition and the process actually being launched, leaving a
# row that would otherwise sit stuck "active" forever (and, since Sprint
# 2's workspace lock, forever blocking that workspace).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PREPARED": frozenset({"QUEUED", "CANCELLED", "FAILED", "INTERRUPTED", "UNKNOWN"}),
    "QUEUED": frozenset({"RUNNING", "CANCELLED", "FAILED", "INTERRUPTED", "UNKNOWN"}),
    "RUNNING": frozenset({"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "UNKNOWN"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
    "INTERRUPTED": frozenset(),
    "UNKNOWN": frozenset(),
}


class DatabaseBusyTimeoutError(Exception):
    """Raised when a genuine SQLITE_BUSY/SQLITE_LOCKED condition did not clear
    within `_BUSY_RETRY_DEADLINE_SECONDS` of retrying (see `_retry_on_busy`).
    Wraps (`raise ... from`) the original `sqlite3.OperationalError`."""


class InvalidTransitionError(Exception):
    """Raised when a run-state transition is not in `ALLOWED_TRANSITIONS`."""


class InvalidCompletionTransitionError(Exception):
    """Raised when an `update_completion` call would move `completion_state`
    along an illegal edge (a backward jump, or any move out of a terminal
    state) — see `runtime.completion.COMPLETION_TRANSITIONS`. Same-state updates
    (retry metadata / evidence enrichment) are always permitted. This is the
    completion-pipeline analogue of `InvalidTransitionError` for `run.state`."""


class InvalidProposalTransitionError(Exception):
    """Raised when an `update_proposal` call would move `state` along an illegal
    edge (a backward jump, or any move out of a terminal state) — see
    `runtime.autonomy.PROPOSAL_TRANSITIONS`. This is the autonomy-proposal
    analogue of `InvalidCompletionTransitionError`."""


class ProposalFieldFrozenError(Exception):
    """Raised when proposal fields are mutated outside the lifecycle states in
    which they are authoritative. In particular, the action, policy, evidence
    digest, eligibility verdict, and execution plan are frozen after assessment."""


class ProposalEvidenceFrozenError(Exception):
    """Raised when evidence is appended after proposal assessment has begun."""


class LostUpdateError(Exception):
    """Raised when a compare-and-set update loses the race (version mismatch)."""


class WorkspaceLockedError(Exception):
    """Raised by `create_run(..., enforce_workspace_lock=True)` when another
    run is already active (`EXECUTION_CENTER_ACTIVE_STATES`) against the same
    `repository_path`. Carries the conflicting run so the caller can report
    it (id, state, task_id, ...) rather than just a message.

    The check-then-insert this guards against is done inside the *same*
    `BEGIN IMMEDIATE` transaction as the new row's `INSERT` (see
    `create_run`), not as a separate query beforehand — `BEGIN IMMEDIATE`
    takes SQLite's write lock up front, so two concurrent callers targeting
    the same workspace serialize here instead of racing: whichever commits
    first makes the row the second one's own conflict check will see."""

    def __init__(self, conflicting_run: dict) -> None:
        self.conflicting_run = conflicting_run
        super().__init__(
            f"Workspace {conflicting_run['repository_path']!r} already has an active run "
            f"({conflicting_run['id']!r}, state={conflicting_run['state']!r})."
        )


class TaskAlreadyActiveError(Exception):
    """Raised by `create_run(..., enforce_workspace_lock=True)` when the same
    `task_id` already has an active run (`EXECUTION_CENTER_ACTIVE_STATES`),
    possibly in a *different* workspace than this launch resolved to.

    The workspace lock alone (`WorkspaceLockedError`) only catches a second
    launch that resolves to the SAME `repository_path`; if a task's configured
    workspace/branch changes between two in-flight launches they can resolve to
    different paths and both slip past it. This check runs inside the same
    `BEGIN IMMEDIATE` transaction as the `INSERT`, so it is a true atomic
    per-task invariant rather than a raceable pre-flight. Carries the
    conflicting run for reporting."""

    def __init__(self, conflicting_run: dict) -> None:
        self.conflicting_run = conflicting_run
        super().__init__(
            f"Task {conflicting_run['task_id']!r} already has an active run "
            f"({conflicting_run['id']!r}, state={conflicting_run['state']!r})."
        )


class GlobalConcurrencyLimitError(Exception):
    """Raised by `create_run(..., max_global_concurrency=N)` when N runs are
    already active (`EXECUTION_CENTER_ACTIVE_STATES`) across *all* workspaces.

    Enforced inside `create_run`'s own `BEGIN IMMEDIATE` transaction (like the
    workspace lock above), so the global cap is a true atomic invariant every
    launch path shares — not an advisory pre-flight count that each entry point
    (the scheduler, the queue's `launch_ready`, portfolio launches, the review
    gate) has to remember to run and that a batch 'launch all READY' would race
    straight past."""

    def __init__(self, active_count: int, limit: int) -> None:
        self.active_count = active_count
        self.limit = limit
        super().__init__(
            f"Global concurrency limit reached ({active_count}/{limit} runs already active)."
        )


class UnknownRunFieldError(Exception):
    """Raised when `update_run_state`/`update_run_fields` is asked to set a
    column that isn't in `_UPDATABLE_RUN_FIELDS`."""


# The only `run` columns `update_run_state`/`update_run_fields` may set via
# their `fields` dict. `id`/`session_id`/`task_id`/`sequence`/`is_resume`/
# `project`/`task_type`/`repository_path`/`prompt`/`command_json`/
# `timeout_seconds`/`created_at` are write-once (set only by `create_run`);
# `state`/`version`/`updated_at` are handled explicitly by the two update
# functions themselves, never via the caller-supplied `fields` dict. This is
# an allowlist checked *before* any SQL is built — every caller today passes
# fixed literal keys, so this is defense in depth (F6), not a fix for a
# reachable injection today, but it turns "a future caller forwards an
# unexpected key" into an immediate, clear exception instead of a dynamically
# constructed `UPDATE ... SET <key> = ...` clause.
_UPDATABLE_RUN_FIELDS: frozenset[str] = frozenset(
    {
        "pid",
        "process_start_identity",
        "pre_run_git_status",
        "post_run_git_status",
        "working_tree_changed",
        "exit_code",
        "cancel_requested",
        "cancel_requested_at",
        "started_at",
        "completed_at",
        "failure_reason",
        # First moment the spawned process produced any output on stdout/
        # stderr — the "Claude startup/handshake" milestone, distinct from
        # `started_at` (the moment `Popen` returned a live PID). Written once,
        # best-effort, by the stdout/stderr reader threads (see
        # `supervisor._record_handshake`); its absence never fails a run.
        "first_output_at",
        # v2 Live Execution Center fields (migration 3) — commit_hash/
        # pull_request_url are the only ones ever set post-create (once, at
        # terminal-state task sync, via `set_run_result_fields`);
        # expected_branch/launch_source/prompt_version are write-once at
        # `create_run` time and never updated afterward, but are still listed
        # here as a defense-in-depth allowlist entry like every other column.
        "commit_hash",
        "pull_request_url",
        # Migration 11: short HEAD captured at launch so the post-run classifier
        # can tell "the agent committed" (HEAD advanced) from "the agent left the
        # tree clean without doing anything" — a committed change leaves the
        # working tree clean, so the porcelain-status diff alone under-counts
        # completed work (see `supervisor._supervise` and `outcome.classify_`).
        "pre_run_head",
    }
)


def _validate_updatable_fields(fields: dict) -> None:
    unknown = set(fields) - db._UPDATABLE_RUN_FIELDS
    if unknown:
        raise db.UnknownRunFieldError(f"Not an updatable run field: {sorted(unknown)}")


# --------------------------------------------------------------------------
# Busy/locked retry — for the narrow class of statements that can return
# SQLITE_BUSY as a single immediate failure *without* looping through the
# connection's own busy handler (see `_retry_on_busy`'s docstring).
# --------------------------------------------------------------------------

_BUSY_RETRY_DEADLINE_SECONDS = 30.0
_BUSY_RETRY_INITIAL_SLEEP_SECONDS = 0.01
_BUSY_RETRY_MAX_SLEEP_SECONDS = 0.5

_T = TypeVar("_T")


def _is_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    """True only for a genuine SQLITE_BUSY/SQLITE_LOCKED (or an extended code
    in either family, e.g. SQLITE_BUSY_SNAPSHOT) condition — never for an
    unrelated `OperationalError` (bad SQL, missing table, unreadable file,
    ...), which must always propagate immediately, unretried."""
    name = getattr(exc, "sqlite_errorname", None)
    if name is not None:
        return name.startswith("SQLITE_BUSY") or name.startswith("SQLITE_LOCKED")
    # `sqlite_errorname`/`sqlite_errorcode` are only populated on Python's
    # sqlite3 module for 3.11+; fall back to matching the two known
    # busy/locked message shapes for older interpreters.
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


def _retry_on_busy(fn: Callable[[], _T], *, deadline_seconds: float = _BUSY_RETRY_DEADLINE_SECONDS) -> _T:
    """Call `fn()` until it succeeds, the deadline elapses, or it raises an
    `OperationalError` that is not a busy/locked condition (re-raised
    immediately, never retried).

    Exists because a handful of statements — chief among them `PRAGMA
    journal_mode=WAL` when multiple processes race to initialize WAL mode on
    a database file that does not yet exist — can return SQLITE_BUSY as one
    immediate failure without the connection's own `busy_timeout` ever
    retrying it. This was confirmed empirically, not assumed: reading back
    `PRAGMA busy_timeout` immediately before the failing call shows it
    already correctly configured (30000ms) at the moment `PRAGMA
    journal_mode=WAL` still raises `sqlite3.OperationalError: database is
    locked` with `sqlite_errorcode == sqlite3.SQLITE_BUSY`. Ordinary reads/
    writes made inside a `BEGIN IMMEDIATE` transaction (see `transaction()`)
    do not need this wrapper — busy_timeout already retries those reliably,
    as proven by the multi-thread/multi-process writer tests in
    `tests/test_runtime_db.py`, which never exercise this path and never
    flake. This wrapper is applied only to the non-transactional, DDL-shaped
    statements executed during connection setup and first-time migration —
    the earliest first-open/first-DDL race, not later INSERT conflicts
    (those are already handled by `migrate()`'s
    `except sqlite3.IntegrityError` on the `schema_version` insert).
    """
    deadline = time.monotonic() + deadline_seconds
    sleep_seconds = db._BUSY_RETRY_INITIAL_SLEEP_SECONDS
    last_exc: sqlite3.OperationalError | None = None
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not db._is_busy_or_locked(exc):
                raise
            last_exc = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(sleep_seconds, max(deadline - time.monotonic(), 0)))
            sleep_seconds = min(sleep_seconds * 2, db._BUSY_RETRY_MAX_SLEEP_SECONDS)
    raise db.DatabaseBusyTimeoutError(
        f"Gave up waiting for a SQLite busy/locked condition to clear after {deadline_seconds}s"
    ) from last_exc


# --------------------------------------------------------------------------
# Connection / transaction management
# --------------------------------------------------------------------------


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """One connection for one operation. Sets WAL, busy_timeout, foreign_keys
    on every connection (WAL persists in the file after the first time, but the
    other two are per-connection settings, so they must be reapplied here).

    The three PRAGMAs are each wrapped in `_retry_on_busy`: `journal_mode=WAL`
    in particular is the statement proven to race unretried under concurrent
    first-time database creation (see `_retry_on_busy`'s docstring) — this is
    what makes opening a connection safe even when several independent
    processes call `connect()` against the same not-yet-existent db file at
    the same instant.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        db._retry_on_busy(lambda: conn.execute("PRAGMA journal_mode=WAL"))
        db._retry_on_busy(lambda: conn.execute("PRAGMA busy_timeout=30000"))
        db._retry_on_busy(lambda: conn.execute("PRAGMA foreign_keys=ON"))
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A short, explicit BEGIN IMMEDIATE / COMMIT (or ROLLBACK on error).

    BEGIN IMMEDIATE takes the write lock up front, so concurrent writers
    serialize here (retrying under `busy_timeout` instead of racing) rather
    than failing with SQLITE_BUSY the way a deferred transaction could.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


_SCHEMA_VERSION_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def migrate(db_path: Path) -> None:
    """Apply every migration newer than the recorded schema version.

    Idempotent in two senses: every SQL-script migration statement is `IF NOT
    EXISTS` (safe to re-run a partially-applied migration), and
    `schema_version.version` is itself a `PRIMARY KEY` — if two processes
    both start up against a brand-new db file at once and both decide
    migration N still needs applying, the loser's `INSERT` raises
    `sqlite3.IntegrityError`, which is caught and treated as "someone else
    already recorded this migration", not an error (see F5/F8: this is a
    real scenario, not hypothetical — two separate CLI invocations against
    the same fresh db file race here in practice).

    The `CREATE TABLE IF NOT EXISTS schema_version` and each migration's own
    DDL (`executescript`/callable `step`) run outside any explicit
    transaction (autocommit, like the PRAGMAs in `connect()`) and are
    therefore wrapped in `_retry_on_busy` too — this is the "earliest
    first-DDL race" (as opposed to the `schema_version` INSERT race just
    above, which was already handled): two processes racing to create
    `schema_version` or apply migration 1's `CREATE TABLE`s for the very
    first time on a brand-new file can hit the same unretried-SQLITE_BUSY
    behavior `connect()`'s docstring describes for `journal_mode=WAL`.
    """
    with db.connect(db_path) as conn:
        db._retry_on_busy(lambda: conn.execute(db._SCHEMA_VERSION_TABLE_SQL))
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        for version, step in db.MIGRATIONS:
            if version <= current:
                continue
            if callable(step):
                db._retry_on_busy(lambda step=step: step(conn))
            else:
                db._retry_on_busy(lambda step=step: conn.executescript(step))
            try:
                with db.transaction(conn):
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, db.iso_now()),
                    )
            except sqlite3.IntegrityError:
                pass  # another process already recorded this migration version
            current = version
        # Stamp the zone this file's naive timestamps are on, from a process
        # that also writes them. Retention reads it back instead of trusting
        # its own `TZ` (VOYN-W0-AICC-RETENTION-TZ).
        db._stamp_timestamp_zone(conn)
    if current >= 13:
        db.backfill_run_provenance(db_path, limit=500)
    # Optional, operator-opted-in retention: run after the migration connection
    # above has closed so VACUUM (if enabled) does not contend with it. See
    # `apply_runtime_retention`.
    db.maybe_apply_runtime_retention(db_path)


#: Where a database records the zone its naive timestamps are on.
#:
#: Deliberately a column on the `schema_version` bookkeeping ledger rather than
#: a table of its own. It is a fact *about the store*, in the same category as
#: `applied_at` (itself an `iso_now` value, and the row this annotates): it says
#: which clock that timestamp — and every other naive timestamp in the file —
#: was written on. A separate domain table would also have to acquire a
#: PostgreSQL counterpart in the store-migration lane before it could exist,
#: which is a coupling this fix has no reason to create.
LEDGER_TIMESTAMP_TZ_COLUMN = "timestamp_tz"
RETENTION_TZ_ENV = "AICC_RUNTIME_TZ"


def _machine_timestamp_zone() -> str | None:
    """The IANA zone name this machine writes `models.iso_now` timestamps in,
    or `None` when it cannot be identified as an IANA key.

    Two sources, in order: an explicit `TZ` (what a container, a service unit
    or a cron entry sets), then the `/etc/localtime` symlink (macOS and Linux
    both point it into the zoneinfo tree). A `TZ` in POSIX rule form
    (`EST5EDT`), or a Windows host with no symlink, yields `None` — a name we
    cannot resolve is worse than no name, because it would be recorded as
    authoritative and then silently mis-resolve later.
    """
    declared = os.environ.get("TZ")
    if declared:
        try:
            ZoneInfo(declared)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return declared
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in link:
        return None
    name = link.split(marker, 1)[1]
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _read_timestamp_zone(conn: sqlite3.Connection) -> str | None:
    """The zone recorded on the ledger, or `None` — including on a database
    written before this column existed, where the `SELECT` itself fails."""
    try:
        row = conn.execute(
            f"SELECT {db.LEDGER_TIMESTAMP_TZ_COLUMN} AS zone FROM schema_version"
            f" WHERE {db.LEDGER_TIMESTAMP_TZ_COLUMN} IS NOT NULL"
            " ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["zone"] if row else None


def _stamp_timestamp_zone(conn: sqlite3.Connection) -> None:
    """Record, once, the zone this database's naive timestamps are on.

    Written by `migrate()`, i.e. by an application process that also writes
    those timestamps, and never overwritten: the recorded zone describes the
    history already in the file. Moving a database to a machine in another zone
    therefore keeps the old (correct) reading of the old rows; new rows written
    there are on a different clock, which no retention cutoff can reconcile —
    that is the `iso_now` convention's own limit, not this function's (see
    `VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL`).

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape the run-table
    migrations use, under the same `BEGIN IMMEDIATE`, so two processes racing
    a brand-new database cannot both decide the column is missing.
    """
    zone = db._machine_timestamp_zone()
    if zone is None:
        return
    try:
        with db.transaction(conn):
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(schema_version)").fetchall()
            }
            if db.LEDGER_TIMESTAMP_TZ_COLUMN not in existing:
                conn.execute(
                    "ALTER TABLE schema_version"
                    f" ADD COLUMN {db.LEDGER_TIMESTAMP_TZ_COLUMN} TEXT"
                )
            already = conn.execute(
                f"SELECT 1 FROM schema_version"
                f" WHERE {db.LEDGER_TIMESTAMP_TZ_COLUMN} IS NOT NULL LIMIT 1"
            ).fetchone()
            if already:
                return
            conn.execute(
                f"UPDATE schema_version SET {db.LEDGER_TIMESTAMP_TZ_COLUMN} = ?"
                " WHERE version = (SELECT MAX(version) FROM schema_version)",
                (zone,),
            )
    except sqlite3.OperationalError:
        # A concurrent writer got there first, or the ledger is not readable
        # here. The marker is not this call's to force; retention says
        # "process-local" rather than deleting against a guessed clock.
        pass


def resolve_timestamp_zone(db_path: Path) -> tuple[str | None, str]:
    """Which zone `completed_at`/`created_at` strings in `db_path` are on, and
    where that answer came from (`"env"`, `"database"`, `"process-local"`).

    `AICC_RUNTIME_TZ` wins so an operator can state the truth for a database
    stamped on the wrong machine; an unusable value raises rather than falling
    back, because a wrong zone here silently changes which rows get deleted.
    """
    override = os.environ.get(RETENTION_TZ_ENV)
    if override:
        try:
            ZoneInfo(override)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{RETENTION_TZ_ENV}={override!r} is not a usable IANA timezone"
            ) from exc
        return override, "env"
    with db.connect(db_path) as conn:
        recorded = db._read_timestamp_zone(conn)
    if recorded:
        try:
            ZoneInfo(recorded)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return recorded, "database"
    return None, "process-local"


def retention_cutoff(db_path: Path, *, retention_days: int) -> tuple[str, str, str]:
    """The `completed_at < ?` bound for `retention_days`, as
    `(cutoff, zone_name, zone_source)`.

    The single reason this exists: `completed_at` is a naive local string, so
    the bound has to be rendered on the *same* clock the rows were written on
    — not on whatever clock the pruning process happens to be started with.
    Anchoring to `datetime.now(timezone.utc)` and converting into the
    database's declared zone makes the returned string identical in every
    process timezone, which is what makes the deleted row *set* deterministic
    (`VOYN-W0-AICC-RETENTION-TZ`).

    With no declared zone (a database that predates migration 24 and has not
    been migrated since) the process clock is all there is; the third element
    says so, so a caller can record that the answer was not pinned.
    """
    zone_name, source = db.resolve_timestamp_zone(db_path)
    if zone_name is None:
        now_local = datetime.now()
        zone_name = datetime.now().astimezone().tzname() or ""
    else:
        now_local = (
            datetime.now(timezone.utc)
            .astimezone(ZoneInfo(zone_name))
            .replace(tzinfo=None)
        )
    cutoff = (now_local - timedelta(days=retention_days)).isoformat(timespec="seconds")
    return cutoff, zone_name, source


def apply_runtime_retention(db_path: Path, *, retention_days: int) -> int:
    """Delete `run_event` rows (and orphaned `report` rows) for runs that have
    been terminal for longer than `retention_days`, returning the number of
    `run_event` rows removed.

    Bounded and conservative:
      * only *terminal* runs are eligible (their events are historical audit
        trail, not live state);
      * the cutoff comes from `retention_cutoff`, which renders it in the zone
        the database declares its naive timestamps are on — so the deleted row
        *set* is the same whichever timezone the pruning process runs in. It
        used to be a bare `datetime.now()`, i.e. the pruning process's own
        zone, which deleted a different set of rows from the same database at
        the same instant (`VOYN-W0-AICC-RETENTION-TZ`);
      * the run row itself is kept (its `state`/`completed_at` remain visible
        in the Execution Center and to reconciliation); only the bulky
        per-output-event history is pruned;
      * runs with a NULL `completed_at` are left untouched.

    Does not VACUUM here — reclaiming disk is a separate, heavier, lock-holding
    operation the operator should run deliberately (see `maybe_apply_runtime_retention`).
    """
    if retention_days <= 0:
        return 0
    cutoff, _zone, _zone_source = db.retention_cutoff(
        db_path, retention_days=retention_days
    )
    placeholders = ",".join("?" for _ in db.TERMINAL_STATES)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            cur = conn.execute(
                f"""
                DELETE FROM run_event
                 WHERE run_id IN (
                    SELECT id FROM run
                     WHERE state IN ({placeholders})
                       AND completed_at IS NOT NULL
                       AND completed_at < ?
                 )
                """,
                (*db.TERMINAL_STATES, cutoff),
            )
            removed = cur.rowcount
        # `report` rows cascade-delete with `run` via FK, but a terminal run's
        # report file on disk is also historical; leave the DB row (the path is
        # small) — only events are bulky.
    return removed


def maybe_apply_runtime_retention(db_path: Path) -> None:
    """Apply retention iff the operator set `AICC_RUNTIME_RETENTION_DAYS` to a
    positive integer. Default (unset / <= 0) is a no-op, so this never changes
    behavior for existing installs or the test suite.

    A companion `AICC_RUNTIME_VACUUM_ON_START=1` runs `VACUUM` after pruning to
    reclaim disk. VACUUM rewrites the whole database under an exclusive lock, so
    it is opt-in and should only be enabled on a single-host install that can
    pause other writers briefly.
    """
    raw = os.environ.get("AICC_RUNTIME_RETENTION_DAYS")
    if not raw:
        return
    try:
        retention_days = int(raw)
    except ValueError:
        return
    if retention_days <= 0:
        return
    try:
        db.apply_runtime_retention(db_path, retention_days=retention_days)
    except ValueError:
        # An unusable `AICC_RUNTIME_TZ`. This path runs inside `migrate()`, on
        # every service construction, so it must not take the app down — but it
        # must not delete rows against a guessed clock either. Skipping is the
        # safe half of that trade; the operator's next deliberate
        # `apply_runtime_retention` call raises and says why.
        return
    if os.environ.get("AICC_RUNTIME_VACUUM_ON_START") == "1":
        with db.connect(db_path) as conn:
            conn.execute("VACUUM")


def current_schema_version(db_path: Path) -> int:
    with db.connect(db_path) as conn:
        db._retry_on_busy(lambda: conn.execute(db._SCHEMA_VERSION_TABLE_SQL))
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] if row and row["v"] is not None else 0


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
