"""Schema DDL and the ordered migration list for the runtime store
(split out of the former single-file ``runtime/db.py``; pure move).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable


import command_center.runtime.db as db  # facade (late-bound; see docstring)

# --------------------------------------------------------------------------
# Schema (idempotent — every statement is `IF NOT EXISTS`, so re-running the
# full script after a partially-applied migration is always safe)
# --------------------------------------------------------------------------

SCHEMA_VERSION = 25

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    task_type TEXT NOT NULL,
    legacy_task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    legacy_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_task_id ON session(task_id);

CREATE TABLE IF NOT EXISTS run (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    is_resume INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    project TEXT NOT NULL,
    task_type TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    command_json TEXT,
    timeout_seconds INTEGER,
    pid INTEGER,
    process_start_identity TEXT,
    pre_run_git_status TEXT,
    post_run_git_status TEXT,
    working_tree_changed INTEGER,
    exit_code INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_session_id ON run(session_id);
CREATE INDEX IF NOT EXISTS idx_run_task_id ON run(task_id);
CREATE INDEX IF NOT EXISTS idx_run_state ON run(state);

CREATE TABLE IF NOT EXISTS run_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_run_event_run_id ON run_event(run_id);

CREATE TABLE IF NOT EXISTS report (
    run_id TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _migration_2_add_failure_reason(conn: sqlite3.Connection) -> None:
    """Adds `run.failure_reason` (machine-readable terminal-state detail, e.g.
    `"timeout"` — see `supervisor.py`'s watchdog). `ALTER TABLE ADD COLUMN`
    isn't naturally idempotent like `CREATE TABLE IF NOT EXISTS`, so this
    checks `PRAGMA table_info` first, making it safe to re-run against a db
    where migration 2 was already (fully or partially) applied.

    The check-then-add is wrapped in one `transaction()` (`BEGIN IMMEDIATE`)
    so it is also safe under genuine concurrent execution, not just
    sequential re-runs: `BEGIN IMMEDIATE` takes the write lock for the whole
    check+`ALTER TABLE`, so a second process racing the same migration
    always sees either "not yet added, lock free" (and adds it) or blocks
    until the first process's transaction commits, then sees "already
    added" and correctly skips it — never both processes deciding
    concurrently that the column is missing and both trying to add it
    (which previously surfaced as an unretried `sqlite3.OperationalError:
    duplicate column name: failure_reason`, distinct from — and not fixed
    by — the busy/locked retry in `_retry_on_busy`, since a genuine
    duplicate-column conflict is `SQLITE_ERROR`, not `SQLITE_BUSY`, and must
    never be retried)."""
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        if "failure_reason" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN failure_reason TEXT")


def _migration_3_add_live_execution_center_v2_fields(conn: sqlite3.Connection) -> None:
    """Adds the Live Execution Center v2 columns (see `docs/adr` for the
    Increment 2 brief): `expected_branch` (resolved once at launch time —
    task branch, else project default branch, else NULL — and never
    recomputed afterward, so it can't drift if project config changes mid-
    run), `launch_source` (`"kanban_task"` or `"execution_center_adhoc"`),
    `prompt_version` (the launching task's `prompt_version` at launch time,
    or NULL for an ad-hoc run), and `commit_hash`/`pull_request_url`
    (populated once, at terminal-state task sync, by parsing the run's final
    result text with the existing `report_parser` — see `task_sync.py`).

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape as migration 2
    (`_migration_2_add_failure_reason`) — safe to re-run against a db where
    this migration was already (fully or partially) applied, and safe under
    genuine concurrent execution via the same `BEGIN IMMEDIATE` transaction.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        for column in ("expected_branch", "launch_source", "commit_hash", "pull_request_url"):
            if column not in existing:
                conn.execute(f"ALTER TABLE run ADD COLUMN {column} TEXT")
        if "prompt_version" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN prompt_version INTEGER")


def _migration_4_add_first_output_at(conn: sqlite3.Connection) -> None:
    """Adds `run.first_output_at` (ISO timestamp of the first stdout/stderr
    line the spawned process produced — the "Claude startup/handshake"
    milestone, recorded once, best-effort, by the supervisor's reader
    threads; see `supervisor._record_handshake`).

    This column is the persisted signal that lets the display layer
    distinguish a run that has *spawned but not yet spoken* (a valid PID, no
    output yet — `session_view.STATUS_STARTING`) from one that is genuinely
    streaming output (`STATUS_RUNNING`), so a slow-to-handshake but perfectly
    healthy run is never surfaced as a failure. Its absence is never itself a
    failure — a run that exits cleanly having produced no stdout at all still
    leaves this NULL and is classified purely on its exit facts.

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape as migrations 2
    and 3 — safe to re-run against a db where this migration was already
    (fully or partially) applied, and safe under genuine concurrent execution
    via the same `BEGIN IMMEDIATE` transaction.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        if "first_output_at" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN first_output_at TEXT")


def _migration_9_add_execution_provider_fields(conn: sqlite3.Connection) -> None:
    """Persist the selected provider and its redacted, deterministic launch metadata."""
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        if "provider_id" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'claude_code'")
        if "provider_metadata_json" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN provider_metadata_json TEXT")


def _migration_11_add_pre_run_head(conn: sqlite3.Connection) -> None:
    """Capture the short HEAD at launch so the post-run outcome classifier can
    distinguish "agent committed its work" (HEAD advanced, tree clean) from
    "agent did nothing" (HEAD unchanged, tree clean). Without this, any agent
    that commits — copilot_cli, claude_code — is mis-classified
    `incomplete:working_tree_unchanged` and re-run forever (AICC-DESKTOP-017).

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape as migrations 2,
    3, 4, 9 — safe to re-run and safe under concurrent first-application via
    the `BEGIN IMMEDIATE` transaction in `migrate()`."""
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        if "pre_run_head" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN pre_run_head TEXT")


def _migration_24_add_finalized_at(conn: sqlite3.Connection) -> None:
    """The durable marker that a run's finalization finished, not merely that
    its state went terminal (VOYN-W0-AICC-SRV-09-FINALIZED-AT).

    `_supervise` commits the terminal row first and only then appends
    `process_exited`, auto-commits the agent's work and saves the report, on a
    daemon thread interpreter shutdown does not join. For the width of that
    window — measured over 20 runs at a 6.1 ms median on a clean working tree
    and a 139 ms median (152 ms max) on a changed one, where the real `git
    commit` of the auto-commit costs 133 ms — the run reads COMPLETED while its
    report does not exist and the agent's commit has not been made. The window
    is widest precisely when there is work to lose. `finalized_at` is written
    *after* those, so a reader can tell
    "finished" from "terminal, still finalizing", and a process killed inside
    the window leaves the run visibly unfinalized rather than silently
    reportless.

    Its point is to be readable from *another process*: `Supervisor.wait_for_run`
    answers the same question from an in-memory registry, which the operator
    running a cutover does not have. See `db.count_unfinalized_runs`.

    Nullable and never backfilled — a pre-existing row finalized under a
    supervisor that recorded nothing, and stamping it now would assert durable
    evidence that was never checked.

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape as migrations 2,
    3, 4, 9, 11 — safe to re-run and safe under concurrent first-application via
    the `BEGIN IMMEDIATE` transaction in `migrate()`. The partial index mirrors
    `idx_run_unfinalized` in `0004_run_finalized_at.up.sql`: it holds an entry
    only while a run is unfinalized, so a successful finalization removes its
    own row from it.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        if "finalized_at" not in existing:
            conn.execute("ALTER TABLE run ADD COLUMN finalized_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_unfinalized "
            "ON run (state) WHERE finalized_at IS NULL"
        )


class FinalizationClaimCutoverRequired(RuntimeError):
    """Schema v25 needs an explicit offline recovery of legacy crash rows."""


def _create_finalization_claim_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS run_finalization_claim ("
        "run_id TEXT NOT NULL PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE, "
        "owner_token TEXT NOT NULL CHECK (owner_token <> ''), "
        "owner_pid INTEGER NOT NULL CHECK (owner_pid > 0), "
        "owner_identity TEXT NOT NULL CHECK (owner_identity <> ''), "
        "claimed_at TEXT NOT NULL, completed_at TEXT, "
        "CHECK (completed_at IS NULL OR completed_at >= claimed_at))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_finalization_claim_open "
        "ON run_finalization_claim(completed_at) WHERE completed_at IS NULL"
    )


def _validate_finalization_claim_schema(conn: sqlite3.Connection) -> None:
    expected_columns = {
        "run_id": ("TEXT", 1, 1),
        "owner_token": ("TEXT", 1, 0),
        "owner_pid": ("INTEGER", 1, 0),
        "owner_identity": ("TEXT", 1, 0),
        "claimed_at": ("TEXT", 1, 0),
        "completed_at": ("TEXT", 0, 0),
    }
    columns = {
        row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
        for row in conn.execute(
            "PRAGMA table_info(run_finalization_claim)"
        ).fetchall()
    }
    if columns != expected_columns:
        raise FinalizationClaimCutoverRequired(
            "runtime schema v25 claim fencing has invalid columns or key constraints"
        )
    foreign_keys = conn.execute(
        "PRAGMA foreign_key_list(run_finalization_claim)"
    ).fetchall()
    if not any(
        row["table"] == "run"
        and row["from"] == "run_id"
        and row["to"] == "id"
        and row["on_delete"].upper() == "CASCADE"
        for row in foreign_keys
    ):
        raise FinalizationClaimCutoverRequired(
            "runtime schema v25 claim fencing lacks run_id foreign-key cascade"
        )
    indexes = {
        row["name"]: row
        for row in conn.execute(
            "PRAGMA index_list(run_finalization_claim)"
        ).fetchall()
    }
    open_index = indexes.get("idx_run_finalization_claim_open")
    if open_index is None or open_index["partial"] != 1:
        raise FinalizationClaimCutoverRequired(
            "runtime schema v25 claim fencing lacks the partial open-claim index"
        )
    indexed_columns = [
        row["name"]
        for row in conn.execute(
            "PRAGMA index_info(idx_run_finalization_claim_open)"
        ).fetchall()
    ]
    index_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_run_finalization_claim_open",),
    ).fetchone()
    normalized_index_sql = "".join(
        (index_sql_row["sql"] if index_sql_row else "").lower().split()
    )
    if indexed_columns != ["completed_at"] or "wherecompleted_atisnull" not in normalized_index_sql:
        raise FinalizationClaimCutoverRequired(
            "runtime schema v25 open-claim index has invalid columns or predicate"
        )


def _migration_25_add_finalization_claim(conn: sqlite3.Connection) -> None:
    """Install claim fencing only after an offline, fully-finalized drain.

    A pre-v25 process cannot write claims.  Creating the table while such a
    process is active would let new code mistake a missing claim for abandoned
    ownership.  Refuse that rolling-upgrade shape; deployment must stop intake
    and the old supervisor, prove the drain, then migrate and restart.
    """
    with db.transaction(conn):
        if conn.execute(
            "SELECT 1 FROM schema_version WHERE version = 25"
        ).fetchone() is not None:
            return
        terminal_states = tuple(sorted(db.TERMINAL_STATES))
        terminal_placeholders = ",".join("?" for _ in terminal_states)
        preexisting = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_finalization_claim'"
        ).fetchone()
        if preexisting is not None:
            raise FinalizationClaimCutoverRequired(
                "runtime schema v25 found an unversioned finalization claim table"
            )
        # Fail closed: only a known terminal state with durable finalization
        # evidence is safe.  There is no SQL CHECK on run.state, so testing
        # only today's active/terminal allowlists would let a corrupt or future
        # state silently cross the non-rolling cutover.
        unsafe = conn.execute(
            "SELECT id, state FROM run WHERE NOT "
            f"(state IN ({terminal_placeholders}) AND finalized_at IS NOT NULL) LIMIT 1",
            terminal_states,
        ).fetchone()
        if unsafe is not None:
            raise FinalizationClaimCutoverRequired(
                "runtime schema v25 requires an offline zero-active, "
                "zero-unfinalized drain before finalization claims are enabled; "
                "use the explicit offline finalization cutover for legacy crash rows"
            )
        _create_finalization_claim_schema(conn)
        # The fencing table and its ledger record are one atomic fact.  The
        # generic migration loop's following INSERT is deliberately idempotent
        # and will observe this row as already present.
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (25, db.iso_now()),
        )


def bootstrap_finalization_claim_cutover(
    db_path: Path,
    *,
    owner_token: str,
    owner_pid: int,
    owner_identity: str,
    offline_confirmed: bool,
) -> int:
    """Serialize the explicit cutover against every ordinary migration."""
    with db._migration_file_lock(db_path):
        return _bootstrap_finalization_claim_cutover_unlocked(
            db_path,
            owner_token=owner_token,
            owner_pid=owner_pid,
            owner_identity=owner_identity,
            offline_confirmed=offline_confirmed,
        )


def _bootstrap_finalization_claim_cutover_unlocked(
    db_path: Path,
    *,
    owner_token: str,
    owner_pid: int,
    owner_identity: str,
    offline_confirmed: bool,
) -> int:
    """Atomically fence legacy terminal crash rows during an offline cutover.

    Version-24 writers cannot honor the claim table, so their absence cannot
    be inferred from SQLite. The caller must first stop intake and every old
    Supervisor, then opt in explicitly. Active/corrupt/future-state rows remain
    unchanged and refused. If this process dies after the atomic schema+claim
    commit, a later v25 Supervisor can take the dead owner's exact-token claim.
    """
    if not offline_confirmed:
        raise FinalizationClaimCutoverRequired(
            "offline finalization cutover requires explicit confirmation that "
            "intake and every pre-v25 Supervisor are stopped"
        )
    if not owner_token or not owner_identity or owner_pid <= 0:
        raise ValueError("a non-empty owner token/identity and positive pid are required")

    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > db.SCHEMA_VERSION:
                raise FinalizationClaimCutoverRequired(
                    "offline finalization cutover refuses runtime schema "
                    f"v{current}; this binary only understands v{db.SCHEMA_VERSION}"
                )
            if current == 25:
                _validate_finalization_claim_schema(conn)
                return 0
            if current != 24:
                raise FinalizationClaimCutoverRequired(
                    f"offline finalization cutover requires schema v24, found v{current}"
                )
            if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'run_finalization_claim'"
            ).fetchone() is not None:
                raise FinalizationClaimCutoverRequired(
                    "runtime schema v25 found an unversioned finalization claim table"
                )

            terminal_states = tuple(sorted(db.TERMINAL_STATES))
            placeholders = ",".join("?" for _ in terminal_states)
            unsafe = conn.execute(
                "SELECT id, state FROM run WHERE "
                f"state NOT IN ({placeholders}) LIMIT 1",
                terminal_states,
            ).fetchone()
            if unsafe is not None:
                raise FinalizationClaimCutoverRequired(
                    "offline finalization cutover refuses active, corrupt, or "
                    f"future-state run {unsafe['id']!r} ({unsafe['state']!r})"
                )

            _create_finalization_claim_schema(conn)
            claimed_at = db.iso_now()
            pending = conn.execute(
                "SELECT id FROM run WHERE finalized_at IS NULL "
                f"AND state IN ({placeholders}) ORDER BY id",
                terminal_states,
            ).fetchall()
            for pending_run in pending:
                conn.execute(
                    "INSERT INTO run_finalization_claim ("
                    "run_id, owner_token, owner_pid, owner_identity, "
                    "claimed_at, completed_at) VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        pending_run["id"],
                        owner_token,
                        owner_pid,
                        owner_identity,
                        claimed_at,
                    ),
                )
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (25, claimed_at),
            )
            return len(pending)


# Autonomous completion pipeline (AICC-AUTONOMY-001). One `completion` row per
# run drives a *separate* state machine — the post-execution "is the engineering
# task actually merged into the target branch" lifecycle — distinct from and
# additive to the `run` row's execution state machine (which stays terminal once
# a process exits). It is a mutable current-state row, guarded by the same
# `version` compare-and-set column pattern as `run`. `completion_validation`
# records one row per validation command per attempt (bounded stdout/stderr
# summaries, never unbounded logs). `completion_event` is an append-only audit
# trail of the completion lifecycle (PR created, closed-unmerged, merged,
# target-branch verified, ...), ordered by a per-run monotonic `seq`, mirroring
# `run_event`.
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS completion (
    run_id TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES session(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    branch TEXT,
    base_branch TEXT,
    head_commit TEXT,
    remote TEXT,
    remote_branch TEXT,
    pull_request_number INTEGER,
    pull_request_url TEXT,
    pull_request_state TEXT,
    replaced_pull_request_number INTEGER,
    replaced_pull_request_url TEXT,
    merge_commit TEXT,
    merge_mode TEXT,
    merge_method TEXT,
    completion_state TEXT NOT NULL,
    last_reason_code TEXT,
    requires_human INTEGER NOT NULL DEFAULT 0,
    is_recoverable INTEGER NOT NULL DEFAULT 0,
    recommended_action TEXT,
    validation_summary TEXT,
    policy_json TEXT,
    last_checked_at TEXT,
    next_retry_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    recovery_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_completion_task_id ON completion(task_id);
CREATE INDEX IF NOT EXISTS idx_completion_state ON completion(completion_state);

CREATE TABLE IF NOT EXISTS completion_validation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    command TEXT NOT NULL,
    exit_code INTEGER,
    started_at TEXT,
    finished_at TEXT,
    stdout_summary TEXT,
    stderr_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_completion_validation_run_id ON completion_validation(run_id);

CREATE TABLE IF NOT EXISTS completion_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reason_code TEXT,
    message TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_completion_event_run_id ON completion_event(run_id);
"""


# Autonomy proposal foundation (AICC-AUTONOMY-002). The pre-execution decision
# layer: a proposal is an evidence-backed, risk-classified suggestion that moves
# through the `runtime.autonomy` state machine under an explicit policy. It never
# executes anything itself — `dispatched_run_id`/`dispatched_task_id` only ever
# *record* an execution the caller performed through the existing routes.
#
#   proposal          -- one mutable current-state row per proposal, guarded by
#                        a `version` column (compare-and-set) and the
#                        `autonomy.is_valid_proposal_transition` structural guard.
#   proposal_evidence -- append-only, immutable. The observations a decision was
#                        made on; never updated, so the audit trail cannot be
#                        rewritten after the fact.
#   proposal_event    -- append-only audit trail, ordered by per-proposal `seq`.
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS proposal (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    state TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    policy_json TEXT,
    eligibility_json TEXT,
    plan_json TEXT,
    evidence_digest TEXT,
    requires_human INTEGER NOT NULL DEFAULT 1,
    last_reason_code TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    dispatched_run_id TEXT REFERENCES run(id) ON DELETE SET NULL,
    dispatched_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposal_state ON proposal(state);
CREATE INDEX IF NOT EXISTS idx_proposal_project ON proposal(project);
CREATE INDEX IF NOT EXISTS idx_proposal_task_id ON proposal(task_id);

CREATE TABLE IF NOT EXISTS proposal_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT,
    observed_at TEXT NOT NULL,
    is_blocker INTEGER NOT NULL DEFAULT 0,
    data_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_proposal_evidence_proposal_id ON proposal_evidence(proposal_id);

CREATE TABLE IF NOT EXISTS proposal_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT,
    reason_code TEXT,
    message TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_proposal_event_proposal_id ON proposal_event(proposal_id);
"""


def _migration_7_add_proposal_parameters_json(conn: sqlite3.Connection) -> None:
    """Add the immutable, canonical action payload approved by a proposal.

    Existing schema-6 databases contain proposals without a structured action
    payload. Backfill those rows with the empty-object sentinel rather than
    NULL so every caller can safely parse `parameters_json` after migration.
    The check-and-add runs under the same write lock used by earlier callable
    migrations, making concurrent and repeated migration attempts safe.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(proposal)").fetchall()}
        if "parameters_json" not in existing:
            conn.execute(
                "ALTER TABLE proposal "
                "ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}'"
            )


def _migration_8_add_independent_review_fields(conn: sqlite3.Connection) -> None:
    """Add the independent-review verdict to `completion`.

    The blocking review gate needs three facts per completion: which run
    produced the verdict, what the verdict was, and the reviewer's reasoning.
    They live on the completion row rather than in a side table because there is
    exactly one review outcome per completion and it is read on the same access
    path as every other completion field.

    Existing schema-7 databases keep NULLs, which read as "no verdict yet" — the
    same thing a brand-new row means. That is the safe direction: with the gate
    enabled, no verdict means *wait*, never *proceed*. The check-and-add runs
    under the same write lock as the earlier callable migrations, so concurrent
    and repeated migration attempts are safe.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(completion)").fetchall()}
        for column in ("review_verdict", "review_run_id", "review_summary"):
            if column not in existing:
                conn.execute(f"ALTER TABLE completion ADD COLUMN {column} TEXT")


_SCHEMA_V10 = """
-- ADR 0007 step 1: the execution queue's home in the execution-state store.
--
-- Mirrors `data/execution_queue.json`'s entry shape exactly, field for field,
-- because during the dual-write phases the two must be comparable without any
-- translation — a divergence check that had to normalise shapes first would be
-- checking its own translation as much as the data.
--
-- `task_id` is deliberately NOT a foreign key. The task lives in tasks.json,
-- which stays a human-editable file (see the ADR); the queue's reference to it
-- is advisory, and an entry whose task has vanished resolves to `cancelled`
-- with a reason, exactly as `evaluate_readiness` already does today.
CREATE TABLE IF NOT EXISTS queue_entry (
    id            TEXT PRIMARY KEY,
    task_id       TEXT,
    project       TEXT,
    state         TEXT NOT NULL,
    reason        TEXT,
    run_id        TEXT,
    added_at      TEXT,
    evaluated_at  TEXT,
    launched_at   TEXT,
    -- Preserves the JSON file's list order, which is load-bearing: the queue is
    -- displayed and planned in insertion order, and a set-shaped table would
    -- silently reorder it.
    position      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_queue_entry_state ON queue_entry(state);
CREATE INDEX IF NOT EXISTS idx_queue_entry_task ON queue_entry(task_id);
"""

def _migration_12_add_executor_capability_fields(conn: sqlite3.Connection) -> None:
    """Adds the executor-capability columns (see the executor-capabilities
    brief): `capability_profile` (the granted profile — `READ_ONLY` /
    `WORKSPACE_WRITE`), `capability_override` (the normalized per-task override,
    or NULL), `required_capabilities` / `granted_capabilities` (comma-joined
    canonical tool lists), `capability_preflight` (`ok` / `mismatch`, the
    pre-spawn decision), and `command_policy` (a secret-free identity of the
    tool-permission policy the command encodes — profile + permission-mode +
    tool flag, never the prompt).

    All are write-once, resolved by the launcher at run-creation time. A run
    row that predates this migration keeps NULL for every one of them; readers
    (`session_view`, `reports`) treat NULL as "legacy / unknown" and fall back
    to deriving the profile from `task_type` deterministically, so legacy rows
    render a stable, safe default rather than crashing.

    Same idempotent check-then-`ALTER TABLE ADD COLUMN` shape as migrations 2
    and 3, wrapped in one `BEGIN IMMEDIATE` transaction — safe to re-run and
    safe under genuine concurrent execution.
    """
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(run)").fetchall()}
        for column in (
            "capability_profile",
            "capability_override",
            "required_capabilities",
            "granted_capabilities",
            "capability_preflight",
            "command_policy",
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE run ADD COLUMN {column} TEXT")


_SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS run_provenance (
    run_id TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    repository_path TEXT,
    worktree_path TEXT,
    branch TEXT,
    base_branch TEXT,
    base_sha TEXT,
    head_sha TEXT,
    pull_request_number INTEGER,
    pull_request_url TEXT,
    pull_request_head_sha TEXT,
    ci_conclusions_json TEXT,
    ci_observed_at TEXT,
    accepted_sha TEXT,
    accepted_at TEXT,
    deployed_sha TEXT,
    deployment_environment TEXT,
    deployed_at TEXT,
    deployment_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_provenance_task_id ON run_provenance(task_id);
CREATE INDEX IF NOT EXISTS idx_run_provenance_pr ON run_provenance(pull_request_number);

CREATE TABLE IF NOT EXISTS provenance_evidence (
    integrity_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_sha TEXT,
    reported_sha TEXT,
    native_payload_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provenance_evidence_run_id
    ON provenance_evidence(run_id);
"""


_SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS run_provider_route (
    run_id TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    providers_json TEXT NOT NULL,
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    selection_reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_attempt (
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    provider_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    classification TEXT,
    disposition TEXT,
    error_code TEXT,
    parent_attempt_number INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (run_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_provider_attempt_run_id
    ON provider_attempt(run_id, attempt_number);
"""


# Wave 1 "new engine" persistence (W1-DATA-EVENTS). Three additive, standalone
# table families that back the Wave-1 product surfaces and are wholly distinct
# from the autonomy `proposal` family above:
#
#   advisor_proposal -- Советник (the advisor inbox). One mutable current-state
#                       row per suggestion, guarded by a `version` compare-and-
#                       set column and an explicit status-transition allowlist
#                       (`wave1.ADVISOR_PROPOSAL_TRANSITIONS`). `promoted_task_id`
#                       only ever *records* a task the caller created through the
#                       existing tasks path — this layer executes nothing itself.
#   owner_item       -- «Мой день» (the owner's action list). `done` is 0/1.
#   digest_item      -- Дайджест (a periodic rollup entry). `refs_json` is a
#                       JSON array of opaque string references; the row is
#                       write-once (no update path).
#
# Statuses/kinds are stored as their stable string *values* (never a Python
# enum's member name), so a column round-trips to exactly the Literal the API
# contract (`api/models.py`) declares — the enum-name lesson from the earlier
# migration renumbering.
_SCHEMA_V15 = """
CREATE TABLE IF NOT EXISTS advisor_proposal (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    expected_gain TEXT,
    effort TEXT,
    project_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    promoted_task_id TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_advisor_proposal_status ON advisor_proposal(status);
CREATE INDEX IF NOT EXISTS idx_advisor_proposal_project ON advisor_proposal(project_ref);

CREATE TABLE IF NOT EXISTS owner_item (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    detail TEXT,
    due TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    source_ref TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_owner_item_done ON owner_item(done);

CREATE TABLE IF NOT EXISTS digest_item (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    category TEXT,
    refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digest_item_category ON digest_item(category);
CREATE INDEX IF NOT EXISTS idx_digest_item_created ON digest_item(created_at);
"""


def _migration_16_add_digest_day_and_position(conn: sqlite3.Connection) -> None:
    """Give ``digest_item`` an explicit build ``day`` and intra-build
    ``position`` so the morning-digest engine (``command_center/digest``) can
    build one deterministic, ordered rollup per calendar day and *rebuild* it
    idempotently (delete the day, re-insert) without duplicating rows or leaning
    on second-precision ``created_at`` (which ties within a build) for order.

    ``day`` is the ``YYYY-MM-DD`` the entry belongs to; ``position`` is its rank
    inside that day's digest (0-based, assembly order). Both are additive and
    optional: a row written through the pre-existing ad-hoc ``POST /digest``
    path keeps ``day = NULL`` / ``position = 0`` and is unaffected. Rows that
    predate this migration read the same way.

    Same idempotent check-then-``ALTER TABLE ADD COLUMN`` shape as the earlier
    callable migrations, wrapped in one ``BEGIN IMMEDIATE`` transaction — safe to
    re-run and safe under genuine concurrent first-application."""
    with db.transaction(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(digest_item)").fetchall()}
        if "day" not in existing:
            conn.execute("ALTER TABLE digest_item ADD COLUMN day TEXT")
        if "position" not in existing:
            conn.execute("ALTER TABLE digest_item ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_digest_item_day ON digest_item(day)")


def _migration_17_add_owner_digest_project_ref(conn: sqlite3.Connection) -> None:
    """Give ``owner_item`` and ``digest_item`` an explicit, nullable
    ``project_ref`` so the redaction policy (drop BANK/LEGAL rows —
    ``project_config.is_sensitive``) can be enforced *in the SQL query* the same
    way it already is for ``advisor_proposal.project_ref``.

    Before this column the two surfaces carried no project binding, so a
    sensitive row could only be filtered after the fact — which under-returns a
    limit/offset page (audit MED-1/MED-2). Keying the exclusion on a real column
    lets ``list_owner_items``/``list_digest_items`` page over *visible* rows
    only, and lets an ad-hoc/auto-filled row record which project it belongs to.

    Additive and optional: ``project_ref`` defaults to ``NULL`` (an un-attributed
    item), so every pre-existing row and every write that does not name a
    project reads exactly as before. Same idempotent check-then-``ALTER TABLE
    ADD COLUMN`` shape as the earlier callable migrations, in one
    ``BEGIN IMMEDIATE`` transaction — safe to re-run and safe under concurrent
    first-application."""
    with db.transaction(conn):
        owner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(owner_item)").fetchall()}
        if "project_ref" not in owner_cols:
            conn.execute("ALTER TABLE owner_item ADD COLUMN project_ref TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_owner_item_project ON owner_item(project_ref)")
        digest_cols = {row["name"] for row in conn.execute("PRAGMA table_info(digest_item)").fetchall()}
        if "project_ref" not in digest_cols:
            conn.execute("ALTER TABLE digest_item ADD COLUMN project_ref TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_digest_item_project ON digest_item(project_ref)")


# Wave-2 Conflicts/Incidents engine (VOYN-W2-CONFLICT). One additive, standalone
# table family, wholly distinct from every family above:
#
#   conflict -- one mutable current-state row per tracked conflict, guarded by a
#               `version` compare-and-set column and an explicit status-transition
#               allowlist (`conflict.CONFLICT_TRANSITIONS`, open → mitigating →
#               resolved). `source_ref` records the opaque origin (e.g.
#               `incident:<id>`); `owner` and `mitigation` are the two facts the
#               *service* requires before a row may reach `resolved`. `project_ref`
#               (nullable) is the redaction key: a BANK/LEGAL row is excluded in
#               the SQL query so its `source_ref` never leaves the read surface.
#
# Statuses/kinds/severities are stored as their stable string *values* (never a
# Python enum member name), so a column round-trips to exactly the Literal the
# API contract (`api/models.py Conflict`) declares — the enum-name lesson carried
# forward from the earlier migration renumbering.
_SCHEMA_V18 = """
CREATE TABLE IF NOT EXISTS conflict (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'sev3',
    status TEXT NOT NULL DEFAULT 'open',
    owner TEXT,
    mitigation TEXT,
    project_ref TEXT,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conflict_status ON conflict(status);
CREATE INDEX IF NOT EXISTS idx_conflict_kind ON conflict(kind);
CREATE INDEX IF NOT EXISTS idx_conflict_owner ON conflict(owner);
CREATE INDEX IF NOT EXISTS idx_conflict_project ON conflict(project_ref);
CREATE INDEX IF NOT EXISTS idx_conflict_source_ref ON conflict(source_ref);
"""


# Wave 2 "new engine" persistence (VOYN-W2-AUD): the Audit engine's two
# table-families, standalone and additive, backing the automated-audit surface
# (`command_center/audit` checks → `api/audit_service.py` → this repository).
# Renumbered to v19 on integration: the sibling Wave-2 Conflicts engine landed
# first and took v18, so this family moves to v19 — its content is unchanged and
# version-agnostic, and the two families never collide.
#
#   audit_run     -- one mutable current-state row per audit pass over a project,
#                    guarded by a `version` compare-and-set column and an explicit
#                    status-transition allowlist (`audit.AUDIT_RUN_TRANSITIONS`).
#                    `project_ref` is NOT NULL — a run always targets one project
#                    so a sensitive (BANK/LEGAL) run is redacted in the SQL query.
#   audit_finding -- one row per finding. `status` (open/ack/fixed) and `owner`
#                    are BOTH NOT NULL: every finding always carries the two
#                    triage axes, enforced at the persistence boundary so a
#                    finding with no owner can never reach a stored row (the
#                    acceptance invariant). `project_ref` mirrors the run's so
#                    redaction pages over visible findings directly.
#                    `promoted_task_id` only *records* a task the caller created
#                    through `tasks_repository` — this layer executes nothing.
#
# Statuses/severities/categories are stored as their stable string *values*
# (never a Python enum's member name), so a column round-trips to exactly the
# Literal the API contract (`api/models.py`) declares — the enum-name lesson
# carried forward from the earlier migration renumbering.
_SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS audit_run (
    id TEXT PRIMARY KEY,
    project_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    checks_json TEXT NOT NULL DEFAULT '[]',
    finding_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_run_project ON audit_run(project_ref);
CREATE INDEX IF NOT EXISTS idx_audit_run_status ON audit_run(status);

CREATE TABLE IF NOT EXISTS audit_finding (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES audit_run(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    summary TEXT NOT NULL DEFAULT '',
    file_path TEXT,
    loc TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    owner TEXT NOT NULL,
    project_ref TEXT,
    promoted_task_id TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_finding_run ON audit_finding(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_finding_status ON audit_finding(status);
CREATE INDEX IF NOT EXISTS idx_audit_finding_owner ON audit_finding(owner);
CREATE INDEX IF NOT EXISTS idx_audit_finding_project ON audit_finding(project_ref);
"""


# Wave-3 model registry (VOYN-W3-MODELS): two additive, standalone table
# families backing the AI-model catalog surface (`api/model_registry_routes.py` →
# `model_registry_service.py` → this repository), wholly distinct from every
# family above.
#
#   model_entry -- one mutable current-state row per registered model, external
#                  (a hosted API provider) or local. Guarded by a `version`
#                  compare-and-set column and an explicit status-transition
#                  allowlist (`model_registry.MODEL_STATUS_TRANSITIONS`). A local
#                  model's download is a real status lifecycle — available →
#                  downloading → installed — with a 0..100 `download_progress`;
#                  the actual byte transfer is an injectable downloader in the
#                  service, but this row's lifecycle is real, not a placeholder.
#                  `cost`/`quality`/`latency_ms` are the auto-select signals;
#                  `provenance` records where the model came from.
#   model_event -- append-only governance log, one row per model action
#                  (register, download-request, download-progress, assign, use,
#                  status-change), ordered by a per-model monotonic `seq`. This
#                  is what makes a model's history fully traceable (the VOYN-W3
#                  acceptance): every action a model takes part in is recorded
#                  here with its `provenance`, and the log is never rewritten.
#
# Statuses/kinds/actions are stored as their stable string *values* (never a
# Python enum's member name), so a column round-trips to exactly the Literal the
# API contract (`api/models.py`) declares — the enum-name lesson carried forward
# from the earlier migration renumbering.
_SCHEMA_V20 = """
CREATE TABLE IF NOT EXISTS model_entry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'external',
    provider TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    cost REAL,
    quality REAL,
    latency_ms INTEGER,
    provenance TEXT,
    download_progress INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_entry_kind ON model_entry(kind);
CREATE INDEX IF NOT EXISTS idx_model_entry_status ON model_entry(status);

CREATE TABLE IF NOT EXISTS model_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL REFERENCES model_entry(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    target_ref TEXT,
    provenance TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(model_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_model_event_model_id ON model_event(model_id);
"""


# Wave-3 Marketplace baseline (VOYN-W3-MARKET): the catalogue of installable
# modules/add-ons and its append-only install trail. Two additive tables,
# wholly separate from every family above; the names never collide.
#
# Pre-assigned v21 (the sibling Wave-3 model-registry family took v20, the
# council family v22, the networking family v23) so the parallel Wave-3 branches
# integrate without renumbering each other; v20 and v21 now sit contiguously.
#
#   market_item        -- one mutable current-state row per listing, guarded by
#                         a `lock_version` compare-and-set column and an explicit
#                         status allowlist (`marketplace.MARKET_ITEM_TRANSITIONS`,
#                         listed → installed; installed is terminal). The
#                         package `version`, `publisher` and `provenance`
#                         (where the listing came from) are plain descriptive
#                         columns carried verbatim onto every install-log line.
#   market_install_log -- append-only audit trail: one immutable row per install,
#                         recording *who* (`actor`), *when* (`installed_at`) and
#                         *what version* (`version`) of *which* listing was
#                         installed, plus the `installer` implementation that did
#                         it. This is the acceptance artefact of the install path
#                         — a real, queryable record, never a placeholder.
#
# The CAS column is named `lock_version` (not `version`) precisely because
# `version` is already the listing's semver-ish package string on this family —
# the two must not share a column. Kinds/statuses are stored as their stable
# string *values* (never a Python enum member name) so a column round-trips to
# exactly the Literal the API contract (`api/models.py`) declares — the
# enum-name lesson carried forward from the earlier migration renumbering.
_SCHEMA_V21 = """
CREATE TABLE IF NOT EXISTS market_item (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'listed',
    provenance TEXT NOT NULL DEFAULT '',
    lock_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_item_kind ON market_item(kind);
CREATE INDEX IF NOT EXISTS idx_market_item_status ON market_item(status);
CREATE INDEX IF NOT EXISTS idx_market_item_publisher ON market_item(publisher);

CREATE TABLE IF NOT EXISTS market_install_log (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES market_item(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT '',
    installer TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    installed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_install_log_item ON market_install_log(item_id);
"""


# Wave-3 Council / Board-of-Directors engine (VOYN-W3-COUNCIL): the persistence
# tier behind the collective-decision surface (routes → service → repository →
# db). Four additive, standalone tables, wholly distinct from every family above;
# the names never collide.
#
#   motion          -- one mutable current-state row per motion put to the Board,
#                      guarded by a `version` compare-and-set column and an
#                      explicit status-transition allowlist
#                      (`council.MOTION_TRANSITIONS`, open → decided / withdrawn).
#                      `quorum` is the vote count required before it may close;
#                      `proposed_by` names who raised it; `source_ref` records the
#                      opaque origin an event-raised motion came from (e.g.
#                      `proposal:<id>`, `incident:<id>`) and dedups redeliveries.
#                      `project_ref` (nullable) is the redaction key.
#   council_vote    -- one row per voter per motion. `UNIQUE(motion_id, voter_id)`
#                      makes a second vote by the same voter a structural error
#                      (surfaced as HTTP 409), so "one vote per voter" is enforced
#                      at the persistence boundary, not merely in the service.
#                      `role` records the voter's Board role at the moment of the
#                      vote (roles-recorded invariant); `voter_kind` is ai|human.
#                      Votes are append-only — no update path — so the record of
#                      how each member voted cannot be rewritten after the fact.
#   council_decision -- at most one immutable row per motion (PRIMARY KEY
#                      `motion_id`), written once when quorum is met and the motion
#                      closes. Carries the ADR-style record: `outcome`, the frozen
#                      `tally_json`, the `roles_json` snapshot of every voter's
#                      role + choice, and the `rationale` explaining the outcome.
#                      There is no update path in the repository — once recorded a
#                      decision is source of truth and cannot be edited.
#   council_event   -- append-only journal / audit trail, ordered by a per-motion
#                      monotonic `seq` (mirrors `run_event`/`proposal_event`):
#                      every critical action (motion opened, vote cast, decision
#                      recorded, motion withdrawn) is one row here, so a decision
#                      always carries a full, replayable journal.
#
# Statuses/kinds/choices are stored as their stable string *values* (never a
# Python enum's member name), so a column round-trips to exactly the Literal the
# API contract (`api/models.py`) declares — the enum-name lesson carried forward
# from the earlier migration renumbering.
_SCHEMA_V22 = """
CREATE TABLE IF NOT EXISTS motion (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    proposed_by TEXT NOT NULL,
    quorum INTEGER NOT NULL DEFAULT 1,
    project_ref TEXT,
    proposal_ref TEXT,
    source_ref TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    decided_at TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_motion_status ON motion(status);
CREATE INDEX IF NOT EXISTS idx_motion_project ON motion(project_ref);
CREATE INDEX IF NOT EXISTS idx_motion_source_ref ON motion(source_ref);

CREATE TABLE IF NOT EXISTS council_vote (
    id TEXT PRIMARY KEY,
    motion_id TEXT NOT NULL REFERENCES motion(id) ON DELETE CASCADE,
    voter_id TEXT NOT NULL,
    voter_kind TEXT NOT NULL DEFAULT 'ai',
    role TEXT NOT NULL,
    choice TEXT NOT NULL,
    rationale TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(motion_id, voter_id)
);

CREATE INDEX IF NOT EXISTS idx_council_vote_motion ON council_vote(motion_id);

CREATE TABLE IF NOT EXISTS council_decision (
    motion_id TEXT PRIMARY KEY REFERENCES motion(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    tally_json TEXT NOT NULL DEFAULT '{}',
    roles_json TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL DEFAULT '',
    quorum INTEGER NOT NULL DEFAULT 1,
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_council_decision_outcome ON council_decision(outcome);
CREATE INDEX IF NOT EXISTS idx_council_decision_created ON council_decision(created_at);

CREATE TABLE IF NOT EXISTS council_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    motion_id TEXT NOT NULL REFERENCES motion(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    role TEXT,
    message TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(motion_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_council_event_motion ON council_event(motion_id);
"""


# Wave-3 Networking engine (VOYN-W3-NET): the networking feedback/invitation
# loop's three additive, standalone tables, wholly distinct from every family
# above (routes → service → repository → db).
#
#   contact               -- one row per person you network with. `project_ref`
#                            (nullable) is the redaction key: a BANK/LEGAL contact
#                            is excluded in the SQL query so its handle/name never
#                            leaves the read surface (the Wave-1 exclude-in-SQL
#                            pattern). Mutable row guarded by a `version`
#                            compare-and-set column.
#   message               -- one row per message exchanged with a contact.
#                            `direction` is `inbound`/`outbound`; an inbound
#                            `feedback`-kind message is the intake that the
#                            service turns into an actionable board task.
#                            Write-once (no update path). `project_ref` mirrors the
#                            contact's for the same in-SQL redaction.
#   networking_invitation -- one row per invitation of a contact to the Council.
#                            `council_ref` is the stable seam the Council engine
#                            consumes (no external identity/auth is wired here —
#                            this is the boundary only). Mutable row moving through
#                            an explicit status allowlist
#                            (`networking.INVITATION_TRANSITIONS`, pending →
#                            accepted/declined), guarded by a `version` column.
#
# Schema version is **23** — the last of the Wave-3 db chain, sitting directly
# above the sibling models/market/council families (20/21/22) which landed first.
# The migration driver applies any migration whose version exceeds the recorded
# one in list order, so a fresh db applies 1..23 in sequence and an existing db
# only runs 23.
#
# Statuses/directions are stored as their stable string *values* (never a Python
# enum's member name), so a column round-trips to exactly the Literal the API
# contract (`api/models.py`) declares — the enum-name lesson carried forward from
# the earlier migration renumbering.
_SCHEMA_V23 = """
CREATE TABLE IF NOT EXISTS contact (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    handle TEXT NOT NULL DEFAULT '',
    org TEXT,
    note TEXT,
    project_ref TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_project ON contact(project_ref);
CREATE INDEX IF NOT EXISTS idx_contact_handle ON contact(handle);

CREATE TABLE IF NOT EXISTS message (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    direction TEXT NOT NULL DEFAULT 'inbound',
    kind TEXT NOT NULL DEFAULT 'note',
    body TEXT NOT NULL DEFAULT '',
    project_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_contact ON message(contact_id);
CREATE INDEX IF NOT EXISTS idx_message_project ON message(project_ref);
CREATE INDEX IF NOT EXISTS idx_message_kind ON message(kind);

CREATE TABLE IF NOT EXISTS networking_invitation (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    council_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    project_ref TEXT,
    invited_at TEXT NOT NULL,
    responded_at TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_networking_invitation_contact ON networking_invitation(contact_id);
CREATE INDEX IF NOT EXISTS idx_networking_invitation_status ON networking_invitation(status);
CREATE INDEX IF NOT EXISTS idx_networking_invitation_council_ref ON networking_invitation(council_ref);
CREATE INDEX IF NOT EXISTS idx_networking_invitation_project ON networking_invitation(project_ref);
"""


# Each migration is either a raw SQL script (applied via `executescript`, every
# statement `IF NOT EXISTS`) or a callable(conn) for changes — like `ALTER
# TABLE ADD COLUMN` — that need their own idempotency check.
MIGRATIONS: list[tuple[int, str | Callable[[sqlite3.Connection], None]]] = [
    (1, _SCHEMA_V1),
    (2, _migration_2_add_failure_reason),
    (3, _migration_3_add_live_execution_center_v2_fields),
    (4, _migration_4_add_first_output_at),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
    (7, _migration_7_add_proposal_parameters_json),
    (8, _migration_8_add_independent_review_fields),
    # Renumbered from 6 on integration: the execution-provider branch and the
    # autonomy/review work both grew migrations from a shared base of 5, so this
    # one moves to the end of the sequence. Its content is unchanged and it is
    # idempotent, so a database that already ran it under the old number simply
    # finds the columns present.
    (9, _migration_9_add_execution_provider_fields),
    (10, _SCHEMA_V10),
    (11, _migration_11_add_pre_run_head),
    (12, _migration_12_add_executor_capability_fields),
    (13, _SCHEMA_V13),
    (14, _SCHEMA_V14),
    (15, _SCHEMA_V15),
    (16, _migration_16_add_digest_day_and_position),
    (17, _migration_17_add_owner_digest_project_ref),
    (18, _SCHEMA_V18),
    (19, _SCHEMA_V19),
    (20, _SCHEMA_V20),
    (21, _SCHEMA_V21),
    (22, _SCHEMA_V22),
    (23, _SCHEMA_V23),
    (24, _migration_24_add_finalized_at),
    (25, _migration_25_add_finalization_claim),
]
