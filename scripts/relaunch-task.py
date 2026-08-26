#!/usr/bin/env python3
"""Relaunch an existing task through the Command Center launch pipeline.

Usage: scripts/relaunch-task.py <task_id> [--no-wait]

Mirrors `_launch_task_from_board` in app.py: marks the task ready, enqueues it,
re-evaluates readiness, and calls `execution_queue.launch_ready` for that one
entry. The actual agent process is spawned by the same v2 launcher the UI uses.

By default the script stays alive after launch, periodically reconciling the
supervisor and syncing task state until the run reaches a terminal state. This
is critical: the supervisor's reader threads are daemon threads that die when
the launching process exits — if the script exits immediately, the Streamlit
app's supervisor sees the run as orphaned and no output is captured. Use
``--no-wait`` to launch and exit immediately (legacy behaviour; the run will be
orphaned from this supervisor instance).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center import execution_queue, project_config, tasks_repository  # noqa: E402
from command_center.runtime import api as runtime_api, db as run_db  # noqa: E402
from command_center.runtime import task_sync  # noqa: E402

TASK_ID = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "e5f7fb697e6a4245bdb10a43a654a706"
WAIT = "--no-wait" not in sys.argv

SUPERVISE_INTERVAL = 15  # seconds between reconcile+sync ticks
RUN_DB = ROOT / "data" / "runtime.db"


def _launch() -> tuple[runtime_api.ExecutionCenterAPI, str | None]:
    # 1. Mark the task ready for relaunch (same as the "К перезапуску" button).
    tasks_repository.set_manual_launch_status(
        ROOT, TASK_ID, "Ready", "Отмечено для перезапуска (скрипт relaunch-task)."
    )

    # 2. Fresh load — the mutation above wrote to disk.
    tasks = tasks_repository.load_tasks(ROOT)
    tasks_by_id = {t["id"]: t for t in tasks}
    task = tasks_by_id.get(TASK_ID)
    if task is None:
        print(f"Задача {TASK_ID} не найдена.")
        return (None, None)
    print(f"Задача: {task.get('title') or TASK_ID}  [{task.get('launch_status')}]")

    # 3. Enqueue + re-evaluate (idempotent per task).
    execution_queue.enqueue_and_persist(ROOT, task, tasks_by_id)
    entries = execution_queue.reevaluate_and_persist(ROOT, tasks_by_id)
    entry = next(
        (
            e
            for e in entries
            if e.get("task_id") == TASK_ID and e.get("state") in execution_queue.OPEN_STATES
        ),
        None,
    )
    if entry is None:
        print("Задача не попала в очередь запуска (возможно, не READY).")
        return (None, None)
    print(f"Запись очереди: {entry['id']}  state={entry['state']}")

    # 4. Launch that one entry — same locked path the board uses.
    project_configs = project_config.load_project_configs()
    api = runtime_api.ExecutionCenterAPI()
    _, results = execution_queue.launch_ready(
        ROOT,
        entries,
        tasks,
        tasks_by_id,
        project_configs,
        api,
        entry_ids=[entry["id"]],
    )
    tasks_repository.upsert_tasks(ROOT, tasks)

    run_id = None
    for r in results:
        flag = "OK" if r.launched else "SKIP"
        print(f"  [{flag}] {r.entry_id}: {r.message}")
        if r.launched and r.run_id:
            run_id = r.run_id
    launched = [r for r in results if r.launched]
    if launched:
        print(f"Запущено: {task.get('title') or TASK_ID}.  run_id={run_id}")
    else:
        print("Запуск не выполнен — задача осталась в очереди.")
    return (api, run_id)


def _supervise(api: runtime_api.ExecutionCenterAPI, run_id: str) -> int:
    """Stay alive: reconcile the supervisor and sync task state until the run
    reaches a terminal state.  The supervisor's reader threads are daemon
    threads — if this process exits, they die and no output is captured."""
    print(f"\nСупервизия запуска {run_id[:12]}…  (Ctrl+C для досрочного выхода)\n")
    last_stage = None
    while True:
        try:
            api.reconcile()
        except Exception as exc:
            print(f"  reconcile error: {exc}")

        # Sync the task from the run row.
        tasks = tasks_repository.load_tasks(ROOT)
        tasks_by_id = {t["id"]: t for t in tasks}
        task = tasks_by_id.get(TASK_ID)
        run = run_db.get_run(RUN_DB, run_id) if run_id else None

        if run is not None and task is not None:
            mutated = task_sync.sync_task_from_run(task, run, db_path=RUN_DB)
            if mutated:
                tasks_repository.upsert_tasks(ROOT, tasks)

            state = run.get("state", "?")
            stage = task.get("current_stage", "?")
            progress = task.get("progress", 0)
            first_output = "✓" if run.get("first_output_at") else "·"
            if stage != last_stage:
                print(f"  [{state:>12}] {progress:3d}%  stage={stage}  output={first_output}")
                last_stage = stage

            if state in run_db.TERMINAL_STATES:
                # A terminal row is necessary but not sufficient: the
                # supervisor commits it *before* appending `process_exited`,
                # auto-committing the agent's work and writing the run report,
                # all on a daemon thread that this process's exit would kill
                # without joining. Wait for the supervisor's own finalization
                # signal, then re-sync the task from the settled row.
                waited_from = time.monotonic()
                finalized = api.supervisor.wait_for_run(run_id, timeout=60.0)
                wedged = time.monotonic() - waited_from >= 60.0
                run = finalized or run
                state = run.get("state", state)
                if task_sync.sync_task_from_run(task, run, db_path=RUN_DB):
                    tasks_repository.upsert_tasks(ROOT, tasks)
                print(f"\nПрогон завершён: state={state}  stage={stage}  progress={progress}%")
                if wedged:
                    # Same rule as the debug CLI: a terminal state whose
                    # finalization never completed is not a clean finish, and
                    # returning 0 here would tell the caller it was. The
                    # `process_exited` event, the auto-commit and the report may
                    # all be missing.
                    print(
                        "ВНИМАНИЕ: состояние терминальное, но супервизор не завершил "
                        "финализацию за 60 с — событие process_exited, автокоммит "
                        "работы агента и отчёт могут отсутствовать.",
                        file=sys.stderr,
                    )
                    return 5
                return 0 if state == "COMPLETED" else 1
        elif run is None:
            print(f"\nПрогон {run_id[:12]} не найден в БД — возможно, был удалён.")
            return 1

        time.sleep(SUPERVISE_INTERVAL)


def main() -> int:
    api, run_id = _launch()
    if api is None or run_id is None:
        return 2
    if not WAIT:
        return 0
    try:
        return _supervise(api, run_id)
    except KeyboardInterrupt:
        print("\nСупервизия остановлена пользователем. Процесс агента продолжит работать.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())