"""Legacy task helpers extracted from ``app.py`` (NIGHT-W9-AICC-ARCH slice 1).

These are the thin task-persistence wrappers that used to live inline in
``app.py`` (its "Task persistence (data/tasks.json)" section). Every function
here is pure delegation to ``command_center.tasks_repository`` /
``command_center.models`` — there is deliberately **no second task engine**
(the acceptance criterion of NIGHT-W9-AICC-ARCH): the single writer of
``data/tasks.json`` remains ``tasks_repository.py``, exactly as documented in
``docs/AUTHORITY_MAP.md``, and a fitness test
(``tests/architecture/test_tasks_json_single_writer_fitness.py``) keeps both
``app.py`` and this module free of direct writes to that store.

``app.py`` re-exports every name below unchanged (``load_tasks =
legacy_task_helpers.load_tasks`` …), so every existing call site — including
tests that introspect ``app.create_task``'s signature — keeps working.

No ``save_tasks(tasks)`` bulk-overwrite helper exists here (deliberately, as
in ``app.py`` before the move): writing back whatever ``tasks`` a Streamlit
script run loaded at the top would silently discard any concurrent writer's
change made since that load (see ``tasks_repository``'s module docstring).
Every write path goes through ``create_task``/``update_task_status``/
``delete_task``/``upsert_tasks``/``tasks_repository.upsert_task``/
``tasks_repository.mutate_tasks`` instead, each of which locks and reloads
fresh immediately before writing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from command_center import models, tasks_repository

# Same repository root `app.py` binds its own ROOT to (this file lives at
# command_center/ui/legacy_task_helpers.py, two levels below it). Test
# isolation does not depend on this constant: `tasks_repository.get_repository`
# resolves the actual store location through `storage.resolve_data_dir`, which
# honours the `AICC_DATA_DIR` environment override (see tests/conftest.py).
ROOT = Path(__file__).resolve().parents[2]


def normalize_task(task: dict) -> dict:
    return tasks_repository.normalize_task(task)


def load_tasks() -> list[dict]:
    tasks = tasks_repository.get_repository(ROOT).load_all()
    if tasks:
        return tasks
    # Deployed-console fallback (VOYN-W0-AICC-WIRE-BACKLOG-API): an empty
    # local repository with a configured master projection renders the
    # fleet's real board read-only instead of zeros. A VIEW, not a store:
    # every record is read_only and upsert_tasks refuses them, so the local
    # repository can never silently fork the master backlog.
    from command_center import backlog_client

    return backlog_client.projection_board_tasks()


def _refuse_master_records(tasks: list[dict]) -> list[dict]:
    """Write-path guard for the read-only master view."""
    writable = [task for task in tasks if task.get("source") != "master"]
    if len(writable) != len(tasks):
        raise PermissionError(
            "master-backlog records are a read-only projection: change them "
            "through the backlog store (console edits would fork the truth)"
        )
    return writable


def upsert_tasks(tasks: list[dict]) -> None:
    """The `save_tasks_fn` callback handed to `recommendations_panel`/
    `queue_panel`: both mutate a subset of `tasks_by_id`'s dicts in place
    (via `execution_queue.launch_ready`, exactly like `launch_service`) and
    need to commit exactly those changes. Locked bulk upsert, not a blind
    overwrite of this script run's entire (possibly-stale) `tasks` snapshot."""
    tasks_repository.get_repository(ROOT).upsert_all(_refuse_master_records(tasks))


def new_task_record(
    project: str,
    title: str,
    task_type: str,
    status: str,
    *,
    goal: str | None = None,
    notes: str = "",
    priority: str = "Medium",
    owner: str = "",
    estimate_hours: float = 0.0,
    depends_on: list[str] | None = None,
    parent_task_id: str | None = None,
    prior_run_id: str | None = None,
    workflow_stage: str = "Draft",
    workspace_path: str | None = None,
    branch: str | None = None,
    executor: str | None = None,
    prompt: str | None = None,
    untrusted_import: bool = False,
) -> dict:
    return tasks_repository.new_task_record(
        project,
        title,
        task_type,
        status,
        goal=goal,
        notes=notes,
        priority=priority,
        owner=owner,
        estimate_hours=estimate_hours,
        depends_on=depends_on,
        parent_task_id=parent_task_id,
        prior_run_id=prior_run_id,
        workflow_stage=workflow_stage,
        workspace_path=workspace_path,
        branch=branch,
        executor=executor,
        prompt=prompt,
        untrusted_import=untrusted_import,
    )


def create_task(
    project: str,
    title: str,
    task_type: str,
    status: str,
    *,
    goal: str | None = None,
    notes: str = "",
    priority: str = "Medium",
    owner: str = "",
    estimate_hours: float = 0.0,
    depends_on: list[str] | None = None,
    parent_task_id: str | None = None,
    prior_run_id: str | None = None,
    workflow_stage: str = "Draft",
    workspace_path: str | None = None,
    branch: str | None = None,
    executor: str | None = None,
    prompt: str | None = None,
    untrusted_import: bool = False,
) -> dict:
    """Locked create — every page that adds a task to the Kanban board must
    call this (never `tasks.append(new_task_record(...)); save_tasks(tasks)`
    against its own possibly-stale in-memory `tasks` list, which is exactly
    the pattern that silently drops a concurrent writer's task). See
    `tasks_repository.create_task`/module docstring.

    `untrusted_import` stamps the provenance flag `agent_runner.is_untrusted_task`
    gates on, so a task synthesized from untrusted report content (the "create
    next task" widget, chat "convert message to task") is not laundered into a
    trusted run — audit SEC-D-02, mirroring `backlog_proposals.apply_candidate`."""
    _repo = tasks_repository.get_repository(ROOT)
    return _repo.create(new_task_record(
        project,
        title,
        task_type,
        status,
        goal=goal,
        notes=notes,
        priority=priority,
        owner=owner,
        estimate_hours=estimate_hours,
        depends_on=depends_on,
        parent_task_id=parent_task_id,
        prior_run_id=prior_run_id,
        workflow_stage=workflow_stage,
        workspace_path=workspace_path,
        branch=branch,
        executor=executor,
        prompt=prompt,
        untrusted_import=untrusted_import,
    ))


def update_task_status(task_id: str, new_status: str) -> dict | None:
    return tasks_repository.get_repository(ROOT).update_status(task_id, new_status)


def delete_task(task_id: str, *, on_deleted: Callable[[str], None] | None = None) -> None:
    """Locked delete of the Kanban record.

    `on_deleted` is the seam for the runtime.db cascade: `app.py`'s shim passes
    `get_execution_center_api().delete_task` here so a deleted Kanban card also
    leaves no orphan session/run/event/report/completion rows in the unified
    Runs/Timeline/metrics views (audit AR-1). This module stays Streamlit-free
    and never constructs the `st.cache_resource` API singleton itself — the
    callback keeps the interface testable with plain pytest."""
    tasks_repository.get_repository(ROOT).delete(task_id)
    if on_deleted is not None:
        on_deleted(task_id)


def task_label(task: dict) -> str:
    return tasks_repository.task_label(task)


def unmet_dependencies(task: dict, tasks_by_id: dict[str, dict]) -> list[str]:
    return models.unmet_dependencies(task, tasks_by_id)


def is_blocked(task: dict, tasks_by_id: dict[str, dict]) -> bool:
    return models.is_blocked(task, tasks_by_id)
