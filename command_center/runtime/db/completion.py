"""Completion table-family: the completion pipeline rows, events
and validation results (AICC-AUTONOMY-001) (split out of the former
single-file ``runtime/db.py``; pure move).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable


import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Completion pipeline (AICC-AUTONOMY-001)
#
# One `completion` row per run. Like `run`, it is a mutable current-state row
# updated only through a compare-and-set (`update_completion`) that bumps
# `version`; the set of columns that update may touch is the allowlist below
# (write-once identity columns — run_id/task_id/project/created_at — are
# deliberately absent). The completion *state* string is validated by the
# domain layer (`runtime.completion`), not here, so this module stays agnostic
# to the lifecycle vocabulary.
# --------------------------------------------------------------------------

_UPDATABLE_COMPLETION_FIELDS: frozenset[str] = frozenset(
    {
        "session_id",
        "branch",
        "base_branch",
        "head_commit",
        "remote",
        "remote_branch",
        "pull_request_number",
        "pull_request_url",
        "pull_request_state",
        "replaced_pull_request_number",
        "replaced_pull_request_url",
        "merge_commit",
        "merge_mode",
        "merge_method",
        "completion_state",
        "last_reason_code",
        "requires_human",
        "is_recoverable",
        "recommended_action",
        "validation_summary",
        "review_verdict",
        "review_run_id",
        "review_summary",
        "policy_json",
        "last_checked_at",
        "next_retry_at",
        "retry_count",
        "recovery_count",
    }
)


def _validate_updatable_completion_fields(fields: dict) -> None:
    unknown = set(fields) - db._UPDATABLE_COMPLETION_FIELDS
    if unknown:
        raise db.UnknownRunFieldError(f"Not an updatable completion field: {sorted(unknown)}")


_COMPLETION_INSERT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "task_id",
    "session_id",
    "project",
    "repository_path",
    "branch",
    "base_branch",
    "head_commit",
    "remote",
    "remote_branch",
    "pull_request_number",
    "pull_request_url",
    "pull_request_state",
    "replaced_pull_request_number",
    "replaced_pull_request_url",
    "merge_commit",
    "merge_mode",
    "merge_method",
    "completion_state",
    "last_reason_code",
    "requires_human",
    "is_recoverable",
    "recommended_action",
    "validation_summary",
    "policy_json",
    "last_checked_at",
    "next_retry_at",
    "retry_count",
    "recovery_count",
    "version",
    "created_at",
    "updated_at",
)


def create_completion(
    db_path: Path,
    *,
    run_id: str,
    task_id: str,
    project: str,
    repository_path: str,
    completion_state: str,
    session_id: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    head_commit: str | None = None,
    remote: str | None = None,
    remote_branch: str | None = None,
    merge_mode: str | None = None,
    merge_method: str | None = None,
    policy_json: str | None = None,
    last_reason_code: str | None = None,
) -> dict:
    """Create the single `completion` row for a run.

    Raises `sqlite3.IntegrityError` if a completion row already exists for the
    run (the PRIMARY KEY on `run_id`) — this is the pipeline's restart-safe
    idempotency guard: callers (`runtime.completion_service.begin_completion`)
    check `get_completion` first, so a re-processed terminal run never gets a
    second completion row (and therefore never a duplicate PR)."""
    now = db.iso_now()
    record = {name: None for name in db._COMPLETION_INSERT_COLUMNS}
    record.update(
        {
            "run_id": run_id,
            "task_id": task_id,
            "session_id": session_id,
            "project": project,
            "repository_path": repository_path,
            "branch": branch,
            "base_branch": base_branch,
            "head_commit": head_commit,
            "remote": remote,
            "remote_branch": remote_branch,
            "merge_mode": merge_mode,
            "merge_method": merge_method,
            "completion_state": completion_state,
            "last_reason_code": last_reason_code,
            "requires_human": 0,
            "is_recoverable": 0,
            "policy_json": policy_json,
            "retry_count": 0,
            "recovery_count": 0,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(db._COMPLETION_INSERT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in db._COMPLETION_INSERT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(f"INSERT INTO completion ({columns}) VALUES ({placeholders})", record)
            # The stored row, not `record`: the insert names only
            # `_COMPLETION_INSERT_COLUMNS`, so the record is missing whatever
            # the schema defaults — the shape trap slice 11 measured on `run`.
            stored = dict(
                conn.execute("SELECT * FROM completion WHERE run_id = ?", (record["run_id"],)).fetchone()
            )
    _mirror("PostgresCompletionMirror", stored, "completion")
    return record


def _mirror(mirror_name: str, record: dict, table: str) -> None:
    """Best-effort dual-write of one completion-family row (SRV-01B slice 14).

    One helper for three tables: the rule is identical for all of them — after
    the authoritative commit, silent on failure, lazily imported so the desktop
    and CLI entry points keep working without a driver.
    """
    try:
        from command_center.db import completion_store

        getattr(completion_store, mirror_name)().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror %s into PostgreSQL", table, exc_info=True)


def get_completion(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM completion WHERE run_id = ?", (run_id,)).fetchone()
        return db._row_to_dict(row)


def get_completions_for_runs(db_path: Path, run_ids: list[str]) -> dict[str, dict]:
    """Batch of `get_completion` keyed by run_id — one query for a whole board
    of runs instead of one `sqlite3.connect()` per run (audit H5 N+1). Runs with
    no completion row are simply absent from the result."""
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM completion WHERE run_id IN ({placeholders})", tuple(run_ids)
        ).fetchall()
    return {row["run_id"]: db._row_to_dict(row) for row in rows}


def get_completion_by_task(db_path: Path, task_id: str) -> dict | None:
    """The most recently created completion row for a task (there is one per
    run, and a task may have several runs over time).

    `created_at` is an ISO timestamp at *second* resolution, so two completions
    for the same task created within the same second tie, and a bare
    `ORDER BY created_at DESC` would return an arbitrary one of them. That is
    not hypothetical: an automatic rework relaunches a task as soon as its
    failure is observed, so several completions per task is the normal case, and
    a caller reading the stale row would act on a failure that has already been
    superseded. `rowid` — monotonic per insert — breaks the tie in true
    insertion order."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM completion WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return db._row_to_dict(row)


def list_completions(
    db_path: Path,
    *,
    states: Iterable[str] | None = None,
    due_before: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List completion rows, optionally restricted to a set of
    `completion_state` values and to rows whose `next_retry_at` is due
    (NULL, i.e. never scheduled, or `<= due_before`). Used by the bounded
    completion poller to find work without scanning terminal rows."""
    clauses: list[str] = []
    params: list[Any] = []
    states = list(states) if states is not None else None
    if states is not None:
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        clauses.append(f"completion_state IN ({placeholders})")
        params.extend(states)
    if due_before is not None:
        clauses.append("(next_retry_at IS NULL OR next_retry_at <= ?)")
        params.append(due_before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    params.append(limit)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM completion{where} ORDER BY created_at ASC LIMIT ?", params
        ).fetchall()
        return [dict(row) for row in rows]


def update_completion(db_path: Path, run_id: str, *, expected_version: int, fields: dict) -> dict:
    """Compare-and-set update of a `completion` row, mirroring
    `update_run_fields`: validates `fields` against
    `_UPDATABLE_COMPLETION_FIELDS`, bumps `version`, sets `updated_at`, and
    raises `LostUpdateError` if `expected_version` no longer matches."""
    fields = dict(fields)
    fields.pop("version", None)
    fields.pop("created_at", None)
    db._validate_updatable_completion_fields(fields)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT completion_state, version FROM completion WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such completion: {run_id!r}")
            # Compare-and-set FIRST: a caller whose `expected_version` no longer
            # matches has read a stale row, so it must lose with `LostUpdateError`
            # *before* we judge its intended transition against a state it never
            # saw. Evaluating the transition guard first would let a stale loser
            # be reported as an `InvalidCompletionTransitionError` (its stale
            # target measured against the winner's newer state) — misclassifying
            # benign concurrency as a hard state-machine violation. See
            # AICC-AUTONOMY-002.
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"Completion {run_id!r} version mismatch: expected {expected_version}, actual {row['version']}"
                )
            # Structural transition guard (mirrors `update_run_state`): once the
            # caller is confirmed to be operating on the current row version,
            # reject an illegal completion-state move (a backward jump or a move
            # out of a terminal state). A same-state / metadata-only update (no
            # `completion_state` in `fields`) is always allowed.
            new_state = fields.get("completion_state")
            if new_state is not None and not db.completion_domain.is_valid_completion_transition(
                row["completion_state"], new_state
            ):
                raise db.InvalidCompletionTransitionError(
                    f"Completion {run_id!r} cannot transition {row['completion_state']!r} -> {new_state!r}"
                )
            fields["updated_at"] = db.iso_now()
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["run_id"] = run_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"""UPDATE completion SET {set_clause}, version = version + 1
                    WHERE run_id = :run_id AND version = :expected_version""",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(f"Completion {run_id!r} update affected {cur.rowcount} rows")
            updated = conn.execute("SELECT * FROM completion WHERE run_id = ?", (run_id,)).fetchone()
            record = dict(updated)
    _mirror("PostgresCompletionMirror", record, "completion")
    return record


def append_completion_event(
    db_path: Path,
    run_id: str,
    event_type: str,
    *,
    reason_code: str | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Append one completion audit event and return its per-run sequence
    number. `metadata` is JSON-encoded; callers must never place credentials,
    tokens, or environment dumps in it (see `runtime.completion_service`)."""
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM completion_event WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = row["next_seq"]
            cur = conn.execute(
                """INSERT INTO completion_event
                       (run_id, seq, event_type, reason_code, message, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, seq, event_type, reason_code, message, metadata_json, now),
            )
            stored_event = {
                "id": cur.lastrowid,
                "run_id": run_id,
                "seq": seq,
                "event_type": event_type,
                "reason_code": reason_code,
                "message": message,
                "metadata_json": metadata_json,
                "created_at": now,
            }
    _mirror("PostgresCompletionEventMirror", stored_event, "completion_event")
    return seq


def list_completion_events_stored(db_path: Path, run_id: str) -> list[dict]:
    """Every completion event for one run in the shape SQLite **stores**.

    :func:`list_completion_events` does both things that hide a column from
    reconciliation: it selects an explicit column list without `id`, and it
    pops `metadata_json` in favour of a decoded `metadata`. Fed those rows the
    reconciliation pairs on `None` and compares a column that is not there.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM completion_event WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def list_completion_events(db_path: Path, run_id: str, *, limit: int = 500) -> list[dict]:
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT run_id, seq, event_type, reason_code, message, metadata_json, created_at
               FROM completion_event WHERE run_id = ? ORDER BY seq ASC LIMIT ?""",
            (run_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            raw = event.pop("metadata_json")
            event["metadata"] = json.loads(raw) if raw else None
            events.append(event)
        return events


def record_validation_result(
    db_path: Path,
    run_id: str,
    *,
    attempt: int,
    command: str,
    exit_code: int | None,
    started_at: str | None,
    finished_at: str | None,
    stdout_summary: str | None,
    stderr_summary: str | None,
) -> dict:
    """Record one validation-command result. `stdout_summary`/`stderr_summary`
    must already be bounded by the caller (`runtime.validation`) — this table
    never stores unlimited logs."""
    now = db.iso_now()
    record = {
        "run_id": run_id,
        "attempt": attempt,
        "command": command,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "created_at": now,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO completion_validation
                       (run_id, attempt, command, exit_code, started_at, finished_at,
                        stdout_summary, stderr_summary, created_at)
                   VALUES (:run_id, :attempt, :command, :exit_code, :started_at, :finished_at,
                           :stdout_summary, :stderr_summary, :created_at)""",
                record,
            )
            stored_validation = dict(
                conn.execute(
                    "SELECT * FROM completion_validation WHERE rowid = last_insert_rowid()"
                ).fetchone()
            )
    _mirror("PostgresCompletionValidationMirror", stored_validation, "completion_validation")
    return record


def list_validation_results(db_path: Path, run_id: str, *, attempt: int | None = None) -> list[dict]:
    with db.connect(db_path) as conn:
        if attempt is not None:
            rows = conn.execute(
                "SELECT * FROM completion_validation WHERE run_id = ? AND attempt = ? ORDER BY id ASC",
                (run_id, attempt),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM completion_validation WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
