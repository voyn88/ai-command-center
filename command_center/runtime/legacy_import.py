"""One-way, non-destructive import of v1.2 `data/runs.jsonl` records into the
v2 SQLite runtime store.

This module only ever *reads* `data/runs.jsonl` (via `agent_runner.load_runs`,
the same fold-to-latest-by-id used by the v1.2 Runs page) — it never deletes,
rewrites, or truncates that file, and the v1.2 JSONL runtime keeps working
completely unmodified alongside this store. Import is idempotent: each legacy
run is linked to at most one v2 `session` via `session.legacy_run_id`, so
running the import again only picks up runs that weren't there last time.
"""

from __future__ import annotations

from pathlib import Path

from command_center import agent_runner
from command_center.models import iso_now
from command_center.runtime import db

# v1.2 `RUN_STATUSES` -> v2 run states. v1.2's runner was synchronous
# (README/ARCHITECTURE §11.4), so a legacy row still sitting at "queued" or
# "running" today did not silently keep running for however long this system
# has existed since — its process is certainly gone, and nothing recorded how
# it ended, which is exactly what `INTERRUPTED` means in v2.
_LEGACY_STATUS_TO_STATE: dict[str, str] = {
    "queued": "INTERRUPTED",
    "running": "INTERRUPTED",
    "completed": "COMPLETED",
    "failed": "FAILED",
    "timed_out": "FAILED",
    "cancelled": "CANCELLED",
}


def _map_state(legacy_status: str | None) -> str:
    return _LEGACY_STATUS_TO_STATE.get(legacy_status or "", "UNKNOWN")


def _already_imported(db_path: Path, legacy_run_id: str) -> bool:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM session WHERE legacy_run_id = ?", (legacy_run_id,)).fetchone()
        return row is not None


def _find_or_create_task_for_legacy(
    db_path: Path, *, project: str, task_type: str, legacy_task_id: str | None, title: str
) -> dict:
    if legacy_task_id:
        with db.connect(db_path) as conn:
            row = conn.execute("SELECT * FROM task WHERE legacy_task_id = ?", (legacy_task_id,)).fetchone()
            if row is not None:
                return dict(row)
    return db.create_task(
        db_path,
        project=project,
        title=title or "(imported from v1.2)",
        task_type=task_type,
        legacy_task_id=legacy_task_id,
    )


def import_legacy_runs(db_path: Path, *, legacy_runs: list[dict] | None = None) -> list[str]:
    """Import every legacy run not yet represented in the v2 store.

    `legacy_runs` defaults to the real `data/runs.jsonl` (via
    `agent_runner.load_runs()`) but accepts an explicit list so tests never
    need to touch real runtime data. Returns the v2 run ids created by *this*
    call (empty if every legacy run was already imported).
    """
    if legacy_runs is None:
        legacy_runs = agent_runner.load_runs()

    db.migrate(db_path)
    created_run_ids: list[str] = []

    for legacy in legacy_runs:
        legacy_id = legacy.get("id")
        if not legacy_id or _already_imported(db_path, legacy_id):
            continue

        project = legacy.get("project") or "UNKNOWN"
        task_type = legacy.get("task_type") or "implementation"
        legacy_task_id = legacy.get("task_id")
        repository_path = legacy.get("repository_path") or ""

        task = _find_or_create_task_for_legacy(
            db_path,
            project=project,
            task_type=task_type,
            legacy_task_id=legacy_task_id,
            title=(legacy.get("prompt") or "")[:120],
        )

        session = db.create_session(
            db_path,
            task_id=task["id"],
            project=project,
            repository_path=repository_path,
            legacy_run_id=legacy_id,
        )

        run = db.create_run(
            db_path,
            session_id=session["id"],
            task_id=task["id"],
            project=project,
            task_type=task_type,
            repository_path=repository_path,
            prompt=legacy.get("prompt") or "",
            is_resume=False,
            timeout_seconds=legacy.get("timeout_seconds"),
            command=None,
        )

        state = _map_state(legacy.get("status"))
        legacy_pre = legacy.get("pre_run") or {}
        legacy_post = legacy.get("post_run") or {}

        # Every legacy row is routed PREPARED -> QUEUED -> RUNNING -> <state>,
        # exactly the transitions a live run would take, since `RUNNING` is
        # the only state every one of v2's terminal states is reachable from.
        run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
        run = db.update_run_state(
            db_path,
            run["id"],
            expected_version=run["version"],
            new_state="RUNNING",
            fields={"started_at": legacy.get("started_at") or iso_now()},
        )
        run = db.update_run_state(
            db_path,
            run["id"],
            expected_version=run["version"],
            new_state=state,
            fields={
                "completed_at": legacy.get("completed_at"),
                "exit_code": legacy.get("exit_code"),
                "pre_run_git_status": legacy_pre.get("status_summary"),
                "post_run_git_status": legacy_post.get("status_summary"),
            },
        )

        db.append_run_event(
            db_path, run["id"], "lifecycle", {"lifecycle": "legacy_import", "legacy_run_id": legacy_id}
        )
        if legacy.get("stdout"):
            db.append_run_event(db_path, run["id"], "legacy_stdout", {"stdout": legacy["stdout"]})
        if legacy.get("stderr"):
            db.append_run_event(db_path, run["id"], "legacy_stderr", {"stderr": legacy["stderr"]})

        report_path = legacy.get("report_path")
        if report_path:
            db.create_report(db_path, run["id"], report_path)

        # Last, after the events and the report, exactly as a live run's
        # finalization does it. An imported row has no finalization pending —
        # the import is the whole of it — so leaving the marker empty would
        # park every historical run in the "still finalizing" set permanently
        # and make the cutover predicate unusable on any install that imported
        # its v1 history.
        db.mark_run_finalized(db_path, run["id"])

        created_run_ids.append(run["id"])

    return created_run_ids
