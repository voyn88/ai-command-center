"""Execution table-family: task, session, run, run_event, report
and the execution-queue mirror (split out of the former single-file
``runtime/db.py``; pure move).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import logging

import json
from pathlib import Path
from typing import Any, Iterable


import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------


def create_task(
    db_path: Path,
    *,
    project: str,
    title: str,
    task_type: str,
    task_id: str | None = None,
    legacy_task_id: str | None = None,
) -> dict:
    now = db.iso_now()
    record = {
        "id": task_id or db.new_id(),
        "project": project,
        "title": title,
        "task_type": task_type,
        "legacy_task_id": legacy_task_id,
        "created_at": now,
        "updated_at": now,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO task (id, project, title, task_type, legacy_task_id, created_at, updated_at)
                   VALUES (:id, :project, :title, :task_type, :legacy_task_id, :created_at, :updated_at)""",
                record,
            )
    _mirror_task(record)
    return record

def _mirror_task(record: dict) -> None:
    """Best-effort dual-write of one task into PostgreSQL (SRV-01B slice 10).

    After the authoritative commit and silent on failure, as every mirror since
    slice 2. Root of the family's foreign keys, so a swallowed failure here
    costs every session, run and event that follows for that task.
    """
    try:
        from command_center.db.execution_store import PostgresTaskMirror

        PostgresTaskMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror task into PostgreSQL", exc_info=True)


def _mirror_task_deletion(task_id: str) -> None:
    """Mirror the deletion of one task, cascade included.

    The authority deletes a single row and lets `ON DELETE CASCADE` remove
    everything hanging off it; the target declares the same cascades, so the
    mirror deletes the same single row. Removing the children explicitly would
    give the mirror its own opinion about what a task's removal implies.
    """
    try:
        from command_center.db.execution_store import PostgresTaskMirror

        PostgresTaskMirror().delete_task(task_id)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror task deletion into PostgreSQL", exc_info=True)


def _mirror_session(record: dict) -> None:
    """Best-effort dual-write of one session into PostgreSQL (slice 10)."""
    try:
        from command_center.db.execution_store import PostgresSessionMirror

        PostgresSessionMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror session into PostgreSQL", exc_info=True)




def delete_task(db_path: Path, task_id: str) -> bool:
    """Delete a task and everything that hangs off it, returning True iff a row
    was removed.

    The FK graph already declares `ON DELETE CASCADE` from session/run/run_event/
    report/completion(+validation/event) to their parents, and proposal task refs
    are `ON DELETE SET NULL`, so deleting the `task` row (with `foreign_keys=ON`,
    set per connection) removes every dependent runtime.db row atomically. This
    closes the AR-1 orphan gap: `tasks_repository.delete_task` only rewrites the
    tasks.json card and previously left these rows behind forever.
    """
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            cur = conn.execute("DELETE FROM task WHERE id = ?", (task_id,))
            removed = cur.rowcount > 0
    # After the commit, and only when a row actually went: mirroring a delete
    # that removed nothing would be a no-op here but a lie in the log.
    if removed:
        _mirror_task_deletion(task_id)
    return removed


def get_task(db_path: Path, task_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return db._row_to_dict(row)


def list_tasks(db_path: Path, *, project: str | None = None) -> list[dict]:
    with db.connect(db_path) as conn:
        if project:
            rows = conn.execute(
                "SELECT * FROM task WHERE project = ? ORDER BY created_at DESC", (project,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM task ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


def create_session(
    db_path: Path,
    *,
    task_id: str,
    project: str,
    repository_path: str,
    session_id: str | None = None,
    legacy_run_id: str | None = None,
) -> dict:
    now = db.iso_now()
    record = {
        "id": session_id or db._new_session_id(),
        "task_id": task_id,
        "project": project,
        "repository_path": repository_path,
        "legacy_run_id": legacy_run_id,
        "created_at": now,
        "updated_at": now,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO session (id, task_id, project, repository_path, legacy_run_id, created_at, updated_at)
                   VALUES (:id, :task_id, :project, :repository_path, :legacy_run_id, :created_at, :updated_at)""",
                record,
            )
    _mirror_session(record)
    return record


def get_session(db_path: Path, session_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        return db._row_to_dict(row)


def list_sessions(db_path: Path, *, task_id: str | None = None) -> list[dict]:
    with db.connect(db_path) as conn:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM session WHERE task_id = ? ORDER BY created_at DESC", (task_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM session ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def create_run(
    db_path: Path,
    *,
    session_id: str,
    task_id: str,
    project: str,
    task_type: str,
    repository_path: str,
    prompt: str,
    is_resume: bool,
    timeout_seconds: int | None = None,
    command: list[str] | None = None,
    run_id: str | None = None,
    expected_branch: str | None = None,
    launch_source: str | None = None,
    prompt_version: int | None = None,
    capability_profile: str | None = None,
    capability_override: str | None = None,
    required_capabilities: str | None = None,
    granted_capabilities: str | None = None,
    capability_preflight: str | None = None,
    command_policy: str | None = None,
    provider_id: str = "claude_code",
    provider_metadata_json: str | None = None,
    provider_route: tuple[str, ...] | None = None,
    max_provider_attempts: int | None = None,
    provider_route_reason: str | None = None,
    provider_policy_version: str | None = None,
    canonical_repository_path: str | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    finalization_owner_token: str | None = None,
    finalization_owner_pid: int | None = None,
    finalization_owner_identity: str | None = None,
    enforce_workspace_lock: bool = False,
    max_global_concurrency: int | None = None,
) -> dict:
    """`expected_branch`/`launch_source`/`prompt_version` are write-once, like
    `project`/`task_type`/`repository_path` above — resolved by the caller
    once at launch time and never recomputed or overwritten afterward (they
    are deliberately absent from `_UPDATABLE_RUN_FIELDS`).

    `enforce_workspace_lock`, default `False`, preserves this function's
    original behavior for every direct/low-level caller (including this
    module's own test suite, which routinely creates several concurrently-
    "active" `run` rows against the same throwaway `repository_path` purely
    to exercise persistence mechanics, with no process ever actually
    running). Only `Supervisor.start_raw` — the one path that actually spawns
    a subprocess against `repository_path` — passes `True`. When it does, the
    conflict check (any other row already in `EXECUTION_CENTER_ACTIVE_STATES`
    for this exact `repository_path`) runs inside this same `BEGIN IMMEDIATE`
    transaction as the `INSERT`, so it cannot lose a race against a second,
    concurrent `create_run(..., enforce_workspace_lock=True)` call for the
    same workspace the way a separate pre-flight query (e.g. `launch_service.
    find_active_run_conflict`) can — raises `WorkspaceLockedError` instead of
    inserting."""
    finalization_owner_values = (
        finalization_owner_token,
        finalization_owner_pid,
        finalization_owner_identity,
    )
    if any(value is not None for value in finalization_owner_values) and not all(
        value is not None for value in finalization_owner_values
    ):
        raise ValueError("finalization owner token, pid and identity must be supplied together")
    if finalization_owner_pid is not None and finalization_owner_pid <= 0:
        raise ValueError("finalization_owner_pid must be positive")
    if finalization_owner_token is not None and not finalization_owner_token:
        raise ValueError("finalization_owner_token must be non-empty")
    if finalization_owner_identity is not None and not finalization_owner_identity:
        raise ValueError("finalization_owner_identity must be non-empty")

    if provider_route is not None:
        if not provider_route or any(not item for item in provider_route):
            raise ValueError("provider_route must contain non-empty provider ids")
        if provider_route[0] != provider_id:
            raise ValueError("provider_id must equal the first provider_route entry")
        if max_provider_attempts is None:
            max_provider_attempts = len(provider_route)
        if not 1 <= max_provider_attempts <= len(provider_route):
            raise ValueError("max_provider_attempts must fit inside provider_route")
        if len(set(provider_route)) != len(provider_route):
            raise ValueError("provider_route may attempt each provider at most once")
        provider_route_reason = provider_route_reason or "explicit_request"
        provider_policy_version = provider_policy_version or "project_policy_v1"
    elif max_provider_attempts is not None:
        raise ValueError("max_provider_attempts requires provider_route")

    with db.connect(db_path) as conn:
        with db.transaction(conn):
            if enforce_workspace_lock:
                active_states = tuple(sorted(db.EXECUTION_CENTER_ACTIVE_STATES))
                terminal_states = tuple(sorted(db.TERMINAL_STATES))
                active_placeholders = ", ".join("?" for _ in active_states)
                terminal_placeholders = ", ".join("?" for _ in terminal_states)
                lock_predicate = (
                    f"(state IN ({active_placeholders}) OR "
                    f"(state IN ({terminal_placeholders}) "
                    "AND finalized_at IS NULL))"
                )
                lock_params = (*active_states, *terminal_states)
                conflict = conn.execute(
                    f"SELECT * FROM run WHERE repository_path = ? AND {lock_predicate}",
                    (repository_path, *lock_params),
                ).fetchone()
                if conflict is not None:
                    raise db.WorkspaceLockedError(db._row_to_dict(conflict))

                # Task-id exclusivity (audit M1): the workspace lock above only
                # catches a double-launch that resolves to the SAME workspace. If
                # a task's configured workspace/branch changes between two
                # in-flight launches they can resolve to *different* paths and
                # slip past it — two agents running for one task. Checked in the
                # same BEGIN IMMEDIATE transaction as the INSERT, so it cannot
                # lose the race a separate pre-flight query can. Ordered after the
                # workspace check so a same-workspace conflict still surfaces as
                # WorkspaceLockedError (unchanged behaviour).
                task_conflict = conn.execute(
                    f"SELECT * FROM run WHERE task_id = ? AND {lock_predicate}",
                    (task_id, *lock_params),
                ).fetchone()
                if task_conflict is not None:
                    raise db.TaskAlreadyActiveError(db._row_to_dict(task_conflict))

            if max_global_concurrency is not None:
                # Global cap as an atomic invariant, in the SAME transaction as
                # the INSERT (like the workspace lock above): count every run
                # currently active across all workspaces and refuse the launch if
                # the cap is already met. Two concurrent launches serialize on the
                # BEGIN IMMEDIATE write lock, so they cannot both slip past the
                # count — the way per-caller pre-flight checks can.
                placeholders = ", ".join("?" for _ in db.EXECUTION_CENTER_ACTIVE_STATES)
                active_count = conn.execute(
                    f"SELECT COUNT(*) AS n FROM run WHERE state IN ({placeholders})",
                    tuple(db.EXECUTION_CENTER_ACTIVE_STATES),
                ).fetchone()["n"]
                if active_count >= max_global_concurrency:
                    raise db.GlobalConcurrencyLimitError(active_count, max_global_concurrency)

            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM run WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sequence = row["next_seq"]
            now = db.iso_now()
            record = {
                "id": run_id or db.new_id(),
                "session_id": session_id,
                "task_id": task_id,
                "sequence": sequence,
                "is_resume": 1 if is_resume else 0,
                "state": "PREPARED",
                "project": project,
                "task_type": task_type,
                "repository_path": repository_path,
                "prompt": prompt,
                "command_json": json.dumps(command, ensure_ascii=False) if command is not None else None,
                "timeout_seconds": timeout_seconds,
                "pid": None,
                "process_start_identity": None,
                "pre_run_git_status": None,
                "post_run_git_status": None,
                "working_tree_changed": None,
                "exit_code": None,
                "cancel_requested": 0,
                "cancel_requested_at": None,
                "started_at": None,
                "completed_at": None,
                "expected_branch": expected_branch,
                "launch_source": launch_source,
                "prompt_version": prompt_version,
                "provider_id": provider_id,
                "provider_metadata_json": provider_metadata_json,
                "commit_hash": None,
                "pull_request_url": None,
                "capability_profile": capability_profile,
                "capability_override": capability_override,
                "required_capabilities": required_capabilities,
                "granted_capabilities": granted_capabilities,
                "capability_preflight": capability_preflight,
                "command_policy": command_policy,
                "version": 0,
                "created_at": now,
                "updated_at": now,
            }
            # Build the column list from the table as it exists rather than
            # from a fixed literal. A database migrated only part-way — which
            # is exactly what the historical-schema migration tests construct —
            # has no `provider_*` columns yet, and naming them unconditionally
            # would make `create_run` unusable against any schema older than
            # the one that introduced them.
            table_columns = {row["name"] for row in conn.execute("PRAGMA table_info(run)")}
            insert_columns = [name for name in record if name in table_columns]
            conn.execute(
                f"""INSERT INTO run ({", ".join(insert_columns)})
                    VALUES ({", ".join(f":{name}" for name in insert_columns)})""",
                record,
            )
            # The stored row, not `record`: the insert names only the columns
            # this database has, so `record` is missing everything the schema
            # defaults. See `_mirror_run`.
            stored_run = dict(
                conn.execute("SELECT * FROM run WHERE id = ?", (record["id"],)).fetchone()
            )
            if finalization_owner_token is not None:
                conn.execute(
                    """INSERT INTO run_finalization_claim (
                           run_id, owner_token, owner_pid, owner_identity,
                           claimed_at, completed_at
                       ) VALUES (?, ?, ?, ?, ?, NULL)""",
                    (
                        record["id"],
                        finalization_owner_token,
                        finalization_owner_pid,
                        finalization_owner_identity,
                        now,
                    ),
                )
            provenance_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_provenance'"
            ).fetchone()
            if provenance_table is not None:
                conn.execute(
                    """INSERT INTO run_provenance (
                           run_id, task_id, repository_path, worktree_path, branch,
                           base_branch, base_sha, head_sha, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["id"],
                        task_id,
                        canonical_repository_path,
                        worktree_path or repository_path,
                        branch,
                        base_branch,
                        base_sha,
                        head_sha,
                        now,
                        now,
                    ),
                )
            provider_route_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_provider_route'"
            ).fetchone()
            if provider_route_table is not None and provider_route is not None:
                conn.execute(
                    """INSERT INTO run_provider_route (
                           run_id, providers_json, max_attempts, selection_reason,
                           policy_version, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        record["id"],
                        json.dumps(provider_route, ensure_ascii=False),
                        max_provider_attempts,
                        provider_route_reason,
                        provider_policy_version,
                        now,
                    ),
                )
            # Read both children back inside the transaction, for the same
            # reason `stored_run` is read back: the row is what the mirror
            # needs, and assembling it from the arguments would make the
            # mirror's correctness a property of this function's parameter
            # list rather than of the table.
            #
            # Guarded by the same `sqlite_master` results as the inserts above,
            # and for the same reason. On a database migrated only part-way —
            # what the historical-schema tests build — these tables do not
            # exist, and an unguarded `SELECT` raises *inside* the
            # authoritative transaction: the run itself is lost to a read the
            # mirror asked for, in the one place the swallow-everything hook
            # cannot help, because the raise happens before any hook is
            # reached. The guards above exist precisely to keep `create_run`
            # working against such a schema; the read-back has to honour them.
            stored_provenance = (
                conn.execute(
                    "SELECT * FROM run_provenance WHERE run_id = ?", (record["id"],)
                ).fetchone()
                if provenance_table is not None
                else None
            )
            stored_route = (
                conn.execute(
                    "SELECT * FROM run_provider_route WHERE run_id = ?", (record["id"],)
                ).fetchone()
                if provider_route_table is not None
                else None
            )
    # Parent first: the target refuses a child whose run is not mirrored.
    _mirror_run(stored_run)
    if stored_provenance is not None:
        _mirror_run_provenance(dict(stored_provenance))
    if stored_route is not None:
        _mirror_run_provider_route(dict(stored_route))
    return record


def _mirror_run_provenance(record: dict) -> None:
    """Best-effort dual-write of one `run_provenance` row (SRV-01B slice 13).

    This hook exists because acceptance found the table had four write sites
    and only two hooks. `update_run_provenance` and `set_run_provenance_once`
    were mirrored; the row's *creation*, here in `create_run`, was not — and
    the family's own suite could not see it, because its first reconciliation
    ran after an update whose whole-row upsert repaired the missing row before
    anything looked at it. A staged check is only as good as its first stage.
    """
    try:
        from command_center.db.provenance_store import PostgresRunProvenanceMirror

        PostgresRunProvenanceMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror run_provenance into PostgreSQL", exc_info=True)


def _mirror_run_provider_route(record: dict) -> None:
    """Best-effort dual-write of one `run_provider_route` row (SRV-01B slice 13).

    Slice 13 declared this mirror, gave it a reconciliation and later a
    stored-shape reader, and never called it from anywhere: the table's only
    writer is `create_run`, which mirrored the run and neither child. A mirror
    nothing writes to is indistinguishable from one that does not exist — the
    slice's own words, landing on one of the four tables it shipped. The
    contract now asserts every declared mirror has a caller, so the next such
    declaration says so at import time rather than at cutover.
    """
    try:
        from command_center.db.provenance_store import PostgresRunProviderRouteMirror

        PostgresRunProviderRouteMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror run_provider_route into PostgreSQL", exc_info=True)


def _mirror_run(record: dict) -> None:
    """Best-effort dual-write of one run into PostgreSQL (SRV-01B slice 11).

    Takes the row as **stored**, never the record a caller assembled:
    `create_run` builds its column list from `PRAGMA table_info(run)`, so its
    record omits whatever it never set — measured as three columns today
    (`failure_reason`, `first_output_at`, `pre_run_head`), all nullable, so
    mirroring the record would happen to work. What it omits depends on the
    caller's optional arguments and on that database's schema, so "complete
    enough" is a property of today's callers, not of this code. The stored row
    is the row.

    After the authoritative commit and silent on failure, as every mirror since
    slice 2.
    """
    try:
        from command_center.db.run_store import PostgresRunMirror

        PostgresRunMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror run into PostgreSQL", exc_info=True)


def get_run(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        return db._row_to_dict(row)


def get_latest_run_for_task(db_path: Path, task_id: str) -> dict | None:
    """Newest run row for `task_id`, or None if the task has no runs.

    Distinct from `list_runs(task_id=..., limit=1)`: that path orders only by
    `created_at DESC`, and `created_at` is second-granularity (`iso_now()`),
    so two runs created in the same second tie and SQLite returns them in
    unspecified rowid order. We add `rowid DESC` as a stable tiebreak — rowid
    is insertion order, so the higher rowid is the newer row — guaranteeing
    the actually-newest run is returned. Used by `task_sync.sync_tasks` to
    self-heal tasks whose `current_run_id` was orphaned by a lost update.
    """
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return db._row_to_dict(row)


def list_runs(
    db_path: Path,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    state: str | None = None,
    states: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """`state` (singular, exact match) and `states` (plural, `IN (...)`) are mutually
    exclusive — passing both raises `ValueError` before any SQL is built, so a caller
    can never end up with an ambiguous, silently-ORed filter. `limit`, if given, is
    applied as a SQL `LIMIT` after the existing `ORDER BY created_at DESC`, bounding
    the result set inside SQLite rather than truncating a full-table fetch in Python;
    a negative `limit` raises `ValueError` before any SQL runs (SQLite's own
    `LIMIT -1` means "unlimited," which would silently defeat the bound this
    parameter exists to provide) — `limit=0` remains a valid request that returns
    `[]`.
    """
    if state is not None and states is not None:
        raise ValueError("Pass either `state` or `states`, not both.")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit!r}")
    clauses = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if states is not None:
        states_list = list(states)
        if states_list:
            placeholders = ", ".join("?" for _ in states_list)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states_list)
        else:
            # A bare `0` is a SQLite-ism: SQLite accepts any non-zero
            # expression in a `WHERE` as "true", but PostgreSQL requires the
            # expression to actually be boolean-typed and raises on `WHERE
            # 0`. `1 = 0` evaluates to `false` in both dialects.
            clauses.append("1 = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = " LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM run {where} ORDER BY created_at DESC{limit_clause}", params
        ).fetchall()
        return [dict(row) for row in rows]


def count_runs(
    db_path: Path,
    *,
    states: Iterable[str] | None = None,
) -> int:
    """Cheap `COUNT(*)` over `run` — the authoritative total run count without
    materializing any rows. Used by the dashboard footer/health so "Всего" and
    success-rate denominators are not silently truncated by the Live Board's
    `limit=200` window (`list_runs(limit=200)` only ever sees the 200 newest
    runs, which under-counts on busy installs). Accepts the same `states`
    filter as `list_runs` for windowed counts (e.g. terminal runs in the last
    sprint); `None` counts every run regardless of state."""
    clauses: list[str] = []
    params: list[Any] = []
    if states is not None:
        states_list = list(states)
        if states_list:
            placeholders = ", ".join("?" for _ in states_list)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states_list)
        else:
            # See the matching comment in `list_runs`: PostgreSQL rejects a
            # bare `0` as a `WHERE` expression, unlike SQLite.
            clauses.append("1 = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect(db_path) as conn:
        (n,) = conn.execute(f"SELECT COUNT(*) FROM run {where}", params).fetchone()
        return int(n)


def update_run_state(
    db_path: Path,
    run_id: str,
    *,
    expected_version: int,
    new_state: str,
    fields: dict | None = None,
) -> dict:
    """Compare-and-set transition of `run.state`.

    Raises `InvalidTransitionError` if `new_state` is not reachable from the
    run's *current* state (fetched fresh, inside the same transaction) —
    this is what keeps a terminal state from ever moving anywhere, including
    back to RUNNING, regardless of what `expected_version` the caller has.
    Raises `LostUpdateError` if `expected_version` no longer matches (someone
    else updated the row first) — the caller must re-read and decide whether
    to retry.
    """
    if new_state not in db.RUN_STATES:
        raise ValueError(f"Unknown run state: {new_state!r}")
    fields = dict(fields or {})
    db._validate_updatable_fields(fields)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"No such run: {run_id!r}")
            current_state = row["state"]
            allowed = db.ALLOWED_TRANSITIONS.get(current_state, frozenset())
            if new_state not in allowed:
                raise db.InvalidTransitionError(
                    f"Run {run_id!r} cannot transition {current_state!r} -> {new_state!r}"
                )
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"Run {run_id!r} version mismatch: expected {expected_version}, actual {row['version']}"
                )
            fields["state"] = new_state
            fields["updated_at"] = db.iso_now()
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["run_id"] = run_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"""UPDATE run SET {set_clause}, version = version + 1
                    WHERE id = :run_id AND version = :expected_version""",
                params,
            )
            if cur.rowcount != 1:
                # Should be unreachable (we just read this row in the same
                # transaction), but never silently succeed if it happens.
                raise db.LostUpdateError(f"Run {run_id!r} update affected {cur.rowcount} rows")
            updated = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            stored_run = dict(updated)
    _mirror_run(stored_run)
    return stored_run


def get_run_finalization_claim(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_finalization_claim WHERE run_id = ?", (run_id,)
        ).fetchone()
        return db._row_to_dict(row)


def claim_run_finalization(
    db_path: Path,
    run_id: str,
    *,
    owner_token: str,
    owner_pid: int,
    owner_identity: str,
    expected_owner_token: str | None,
) -> dict | None:
    """Acquire or take over the durable finalization claim by exact CAS.

    Liveness proof is intentionally outside this persistence primitive: the
    caller must first prove that the prior full process identity is gone.  The
    expected token then makes two simultaneous recoverers serialize here; only
    one can replace the observed owner.
    """
    if not owner_token or not owner_identity or owner_pid <= 0:
        raise ValueError("a non-empty owner token/identity and positive pid are required")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            run = conn.execute("SELECT finalized_at FROM run WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["finalized_at"] is not None:
                return None
            current = conn.execute(
                "SELECT * FROM run_finalization_claim WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                if expected_owner_token is not None:
                    return None
                conn.execute(
                    """INSERT INTO run_finalization_claim (
                           run_id, owner_token, owner_pid, owner_identity,
                           claimed_at, completed_at
                       ) VALUES (?, ?, ?, ?, ?, NULL)""",
                    (run_id, owner_token, owner_pid, owner_identity, now),
                )
            else:
                if (
                    current["completed_at"] is not None
                    or current["owner_token"] != expected_owner_token
                ):
                    return None
                cur = conn.execute(
                    """UPDATE run_finalization_claim
                       SET owner_token = ?, owner_pid = ?, owner_identity = ?,
                           claimed_at = ?, completed_at = NULL
                       WHERE run_id = ? AND owner_token = ? AND completed_at IS NULL""",
                    (
                        owner_token,
                        owner_pid,
                        owner_identity,
                        now,
                        run_id,
                        expected_owner_token,
                    ),
                )
                if cur.rowcount != 1:
                    return None
            row = conn.execute(
                "SELECT * FROM run_finalization_claim WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row)


def mark_run_finalized(
    db_path: Path, run_id: str, *, owner_token: str
) -> dict | None:
    """Stamp `run.finalized_at` — the last write of finalization, never part of
    the terminal-state UPDATE (VOYN-W0-AICC-SRV-09-FINALIZED-AT).

    The ordering *is* the feature. `update_run_state` publishes the terminal
    state, and the report, the auto-commit and the `process_exited` event are
    all written after it, on a daemon thread interpreter shutdown does not join.
    Calling this at the end of that sequence makes the marker a consequence of
    those durable writes rather than an announcement of them: a reader that sees
    `finalized_at` knows the report exists, and a process killed anywhere in the
    window leaves the run terminal-but-unfinalized, which is recoverable,
    instead of terminal-and-silently-reportless, which is not detectable at all.
    Folding this into the `fields` dict of `update_run_state` would restore
    exactly the bug this task exists to remove.

    Write-once and idempotent: `WHERE finalized_at IS NULL` means a retry, a
    duplicate finalization or a recovery pass cannot move a marker that is
    already set, so the recorded moment stays the first one at which the run's
    evidence was durable.

    Deliberately does **not** bump `version`, and deliberately does not touch
    `updated_at`. Those belong to the optimistic-concurrency protocol for
    *domain* mutations, and this is a durability watermark, not a domain change.
    Bumping the version would make an unrelated CAS holder — `task_sync` writing
    `commit_hash` at terminal state is the live example — lose its update
    because a bookkeeping write landed between its read and its write, turning
    the marker into precisely the kind of race it was added to close.

    Returns the stored row, or `None` if the run is gone or was already marked.
    """
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            now = db.iso_now()
            claim = conn.execute(
                """SELECT owner_token, completed_at
                   FROM run_finalization_claim WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            if (
                claim is None
                or claim["owner_token"] != owner_token
                or claim["completed_at"] is not None
            ):
                return None
            cur = conn.execute(
                "UPDATE run SET finalized_at = ? WHERE id = ? AND finalized_at IS NULL",
                (now, run_id),
            )
            if cur.rowcount != 1:
                return None
            completed = conn.execute(
                """UPDATE run_finalization_claim SET completed_at = ?
                   WHERE run_id = ? AND owner_token = ? AND completed_at IS NULL""",
                (now, run_id, owner_token),
            )
            if completed.rowcount != 1:
                raise db.LostUpdateError(
                    f"Run {run_id!r} finalization claim changed before completion"
                )
            stored_run = dict(conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone())
    _mirror_run(stored_run)
    return stored_run


def count_unfinalized_runs(db_path: Path) -> int:
    """How many runs are terminal but have not finished finalizing.

    The cross-process predicate that did not exist. `Supervisor.wait_for_run`
    answers a similar question by watching an in-memory registry, which is
    private to the process that launched the run — so the operator draining a
    cutover, the backward mirror deciding whether the seam is quiet, and a
    readiness probe all had nothing to read. This is a plain query against the
    stored marker, so any process that can open the database can evaluate it.

    Zero means every terminal run's report and auto-commit are durable. Nonzero
    means at least one run is either still inside its finalization window or was
    killed inside it; the two are distinguished by whether the owning process is
    still alive, not by this count.

    Served by the partial index `idx_run_unfinalized`, which holds an entry only
    while a run is unfinalized.
    """
    placeholders = ",".join("?" for _ in db.TERMINAL_STATES)
    with db.connect(db_path) as conn:
        (n,) = conn.execute(
            f"SELECT COUNT(*) FROM run WHERE finalized_at IS NULL AND state IN ({placeholders})",
            tuple(db.TERMINAL_STATES),
        ).fetchone()
        return int(n)


def list_unfinalized_runs(db_path: Path, *, limit: int = 100) -> list[dict]:
    """The rows behind `count_unfinalized_runs`, oldest completion first.

    An operator who sees a nonzero count needs to know *which* runs, to decide
    whether to wait or to recover them; a bare number turns a drainable
    condition into an opaque one.
    """
    placeholders = ",".join("?" for _ in db.TERMINAL_STATES)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT * FROM run
                WHERE finalized_at IS NULL AND state IN ({placeholders})
                ORDER BY completed_at ASC, id ASC
                LIMIT ?""",
            (*db.TERMINAL_STATES, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def set_run_result_fields(
    db_path: Path,
    run_id: str,
    *,
    expected_version: int,
    commit_hash: str | None = None,
    pull_request_url: str | None = None,
) -> dict:
    """Thin `update_run_fields` wrapper for the one terminal-sync write site
    (`task_sync.sync_task_from_run`) — populates the two fields deterministic
    report parsing can extract, once, at terminal state."""
    return db.update_run_fields(
        db_path,
        run_id,
        expected_version=expected_version,
        fields={"commit_hash": commit_hash, "pull_request_url": pull_request_url},
    )


def update_run_fields(db_path: Path, run_id: str, *, expected_version: int, fields: dict) -> dict:
    """Compare-and-set update of non-state fields (e.g. recording a PID right
    after Popen succeeds, before any state transition). Does not touch `state`."""
    fields = dict(fields)
    fields.pop("state", None)
    fields.pop("version", None)
    db._validate_updatable_fields(fields)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute("SELECT version FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"No such run: {run_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"Run {run_id!r} version mismatch: expected {expected_version}, actual {row['version']}"
                )
            fields["updated_at"] = db.iso_now()
            set_clause = ", ".join(f"{key} = :{key}" for key in fields)
            params = dict(fields)
            params["run_id"] = run_id
            params["expected_version"] = expected_version
            cur = conn.execute(
                f"""UPDATE run SET {set_clause}, version = version + 1
                    WHERE id = :run_id AND version = :expected_version""",
                params,
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(f"Run {run_id!r} update affected {cur.rowcount} rows")
            updated = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            stored_run = dict(updated)
    _mirror_run(stored_run)
    return stored_run


# --------------------------------------------------------------------------
# RunEvent (append-only)
# --------------------------------------------------------------------------


def append_run_event(db_path: Path, run_id: str, event_type: str, payload: dict) -> int:
    """Append one event and return its per-run sequence number.

    The next `seq` is computed and inserted inside one `BEGIN IMMEDIATE`
    transaction, so concurrent writers (the stdout reader thread and the
    stderr reader thread for the same run) serialize on SQLite's write lock
    rather than racing on the sequence number in application memory.
    """
    payload_json = json.dumps(payload, ensure_ascii=False)
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_event WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = row["next_seq"]
            cur = conn.execute(
                """INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, seq, event_type, payload_json, now),
            )
            stored_event = {
                "id": cur.lastrowid,
                "run_id": run_id,
                "seq": seq,
                "event_type": event_type,
                "payload_json": payload_json,
                "created_at": now,
            }
    _mirror_run_event(stored_event)
    return seq

def _mirror_run_event(record: dict) -> None:
    """Best-effort dual-write of one journal row into PostgreSQL (slice 12).

    Takes the stored record, which carries the id SQLite minted: the target's
    `run_event.id` is `GENERATED ALWAYS AS IDENTITY` and refuses a non-DEFAULT
    value without `OVERRIDING SYSTEM VALUE`, and reconciliation pairs rows by
    id.
    """
    try:
        from command_center.db.run_children_store import PostgresRunEventMirror

        PostgresRunEventMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror run_event into PostgreSQL", exc_info=True)


def _mirror_report(record: dict) -> None:
    """Best-effort dual-write of one report row into PostgreSQL (slice 12)."""
    try:
        from command_center.db.run_children_store import PostgresReportMirror

        PostgresReportMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror report into PostgreSQL", exc_info=True)




def list_run_events_stored(db_path: Path, run_id: str) -> list[dict]:
    """Every journal row for one run in the shape SQLite **stores**.

    :func:`list_run_events` selects an explicit column list that omits `id` —
    reasonably, since callers address the journal by `(run_id, seq)`. But
    reconciliation pairs rows by the table's key, so fed those rows it sees
    `None` on every one of them and reports the whole journal divergent.

    That is a third variant of the same family: slice 4 found readers that
    *decode* a column away, and this one *projects* it away. The fitness gate
    was extended in the same slice to catch the second variant, because the
    first one taught that memory is not a mechanism.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_event WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def list_run_events(
    db_path: Path, run_id: str, *, after_seq: int = 0, limit: int = 1000, event_type: str | None = None
) -> list[dict]:
    """`event_type`, if given, filters in SQL (e.g. `"lifecycle"` for
    `log_tail.session_timeline`) — bounded by `limit` the same way an
    unfiltered call is, never a full-table read followed by an in-Python
    filter."""
    with db.connect(db_path) as conn:
        if event_type is not None:
            rows = conn.execute(
                """SELECT run_id, seq, event_type, payload_json, created_at FROM run_event
                   WHERE run_id = ? AND seq > ? AND event_type = ? ORDER BY seq ASC LIMIT ?""",
                (run_id, after_seq, event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT run_id, seq, event_type, payload_json, created_at FROM run_event
                   WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?""",
                (run_id, after_seq, limit),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        return events


def tail_run_events(db_path: Path, run_id: str, *, limit: int = 200) -> list[dict]:
    """Bounded log tail: the *last* `limit` events for a run, oldest-first —
    never the whole table. Orders `DESC` (so SQLite can stop after `limit`
    rows without scanning every event this run has ever produced) and
    reverses in Python before returning, so callers see them in the same
    chronological order `list_run_events` already returns."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT run_id, seq, event_type, payload_json, created_at FROM run_event
               WHERE run_id = ? ORDER BY seq DESC LIMIT ?""",
            (run_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        events.reverse()
        return events


def latest_events_for_runs(db_path: Path, run_ids: list[str]) -> dict[str, dict]:
    """The single most recent event per run, keyed by run_id — one query for a
    whole board instead of a `sqlite3.connect()` per run (audit H5 N+1). Same
    shaping as `tail_run_events` (payload_json decoded into `payload`). Uses a
    MAX(seq) join rather than a window function so it works on any SQLite the
    rest of the module targets."""
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT e.run_id, e.seq, e.event_type, e.payload_json, e.created_at
                FROM run_event e
                JOIN (
                    SELECT run_id, MAX(seq) AS mx FROM run_event
                    WHERE run_id IN ({placeholders}) GROUP BY run_id
                ) m ON e.run_id = m.run_id AND e.seq = m.mx""",
            tuple(run_ids),
        ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        result[event["run_id"]] = event
    return result


# --------------------------------------------------------------------------
# Report (immutable, at most one per run)
# --------------------------------------------------------------------------


def create_report(db_path: Path, run_id: str, path: str) -> dict:
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO report (run_id, path, created_at) VALUES (?, ?, ?)",
                (run_id, path, now),
            )
    record = {"run_id": run_id, "path": path, "created_at": now}
    _mirror_report(record)
    return record


def get_report(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM report WHERE run_id = ?", (run_id,)).fetchone()
        return db._row_to_dict(row)


def get_reports_for_runs(db_path: Path, run_ids: list[str]) -> dict[str, dict]:
    """Batch of `get_report` keyed by run_id — one query for a whole board
    instead of a `sqlite3.connect()` per run (audit H5 N+1)."""
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM report WHERE run_id IN ({placeholders})", tuple(run_ids)
        ).fetchall()
    return {row["run_id"]: db._row_to_dict(row) for row in rows}


# --------------------------------------------------------------------------
# Execution queue (ADR 0007) — the SQLite home of `execution_queue.json`
#
# These are storage primitives only. Every rule about *what* a queue entry may
# contain, when it becomes ready, and what launching it means stays in
# `command_center.execution_queue`; this layer just stores and returns rows.
# During the dual-write phases the JSON file remains authoritative, so nothing
# here may raise on data the JSON store would have accepted.
# --------------------------------------------------------------------------

_QUEUE_ENTRY_COLUMNS: tuple[str, ...] = (
    "id",
    "task_id",
    "project",
    "state",
    "reason",
    "run_id",
    "added_at",
    "evaluated_at",
    "launched_at",
)


def replace_queue_entries(db_path: Path, entries: list[dict]) -> None:
    """Persist `entries` as the complete queue, in the given order.

    Whole-list replacement rather than per-entry upsert, deliberately: that is
    exactly the semantics `execution_queue.save_queue` has today, so during
    dual-write the two stores cannot drift through a difference in *how* they
    are written. `position` preserves list order, which the queue's display and
    planning both depend on.

    Unknown keys on an entry are ignored rather than rejected — the JSON store
    accepts them, and a dual-write phase where SQLite is stricter than the
    authoritative store would fail on data that is, by definition, valid."""
    rows = [
        {
            **{column: entry.get(column) for column in db._QUEUE_ENTRY_COLUMNS},
            "position": index,
        }
        for index, entry in enumerate(entries)
    ]
    columns = ", ".join((*db._QUEUE_ENTRY_COLUMNS, "position"))
    placeholders = ", ".join(f":{name}" for name in (*db._QUEUE_ENTRY_COLUMNS, "position"))
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute("DELETE FROM queue_entry")
            if rows:
                conn.executemany(
                    f"INSERT INTO queue_entry ({columns}) VALUES ({placeholders})", rows
                )


def list_queue_entries(db_path: Path) -> list[dict]:
    """Every queue entry in stored order, shaped exactly like a JSON entry so a
    divergence check can compare the two directly, with no translation step of
    its own to be wrong about."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(db._QUEUE_ENTRY_COLUMNS)} FROM queue_entry ORDER BY position ASC"
        ).fetchall()
        return [dict(row) for row in rows]
