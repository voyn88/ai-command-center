"""Regression coverage for the Founder Gate finding that prompted this
remediation: `tasks_repository.save_tasks()` (and every write path built
directly on it — task creation, status change, deletion, manual launch-
status toggles) was still exposed to a lost-update race even after
`command_center.task_import.apply_task_package` got its own lock, because
that lock was import-specific and nothing else in the application held it.
The reported symptom was concrete: "ожидалось 80 записей, получено 44"
(expected 80 records, got 44) — many concurrent task creations, most of
them silently discarded.

Every test here uses real, separate OS processes (`multiprocessing`, spawn
context, a real `Barrier` for synchronized starts) against the *same* shared
`tasks_repository.tasks_lock` (`data/tasks.lock`) now used by every write
path in `app.py` and by `task_import.apply_task_package` — never threads
alone, which share one interpreter/GIL and would not prove cross-process
exclusion. See `tests/test_portfolio_registry_concurrency.py` and
`tests/test_task_import_concurrency.py` for the same pattern applied to the
two write paths that already had this fix.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
import os
from pathlib import Path

from command_center import tasks_repository


def _mp_create_worker(data_dir: str, project: str, count: int, prefix: str, barrier) -> None:
    os.environ["AICC_DATA_DIR"] = data_dir
    from command_center import tasks_repository as _tr  # re-imported fresh in this process

    barrier.wait()
    for i in range(count):
        _tr.create_task(Path(data_dir), project, f"{prefix}-{i}", "implementation", "Backlog")


def test_many_concurrent_processes_creating_tasks_lose_nothing(tmp_path):
    """Direct reproduction of the Founder Gate scenario: 8 processes each
    create 10 tasks (80 total) at the same synchronized instant. Before this
    remediation, `create`'s underlying `load -> tasks.append -> save_tasks`
    sequence held no lock at all, so most of these 80 creations would
    silently discard each other — the "80 expected, got 44" bug. After the
    fix, the store must contain exactly 80 distinct tasks."""
    data_dir = os.environ["AICC_DATA_DIR"]
    ctx = multiprocessing.get_context("spawn")
    worker_count = 8
    per_worker = 10
    barrier = ctx.Barrier(worker_count)

    processes = [
        ctx.Process(target=_mp_create_worker, args=(data_dir, "AIOS", per_worker, f"W{i}", barrier))
        for i in range(worker_count)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker process failed (exitcode={p.exitcode})"

    stored = tasks_repository.load_tasks(tmp_path)
    ids = [t["id"] for t in stored]

    assert len(stored) == worker_count * per_worker, (
        f"expected {worker_count * per_worker} records, got {len(stored)} "
        "— this is exactly the Founder Gate lost-update scenario"
    )
    assert len(ids) == len(set(ids)), "duplicate ids after concurrent creation"

    # Every worker's full batch of titles must be present — nobody's tasks
    # were silently overwritten by another worker's write.
    titles = {t["title"] for t in stored}
    expected_titles = {f"W{i}-{j}" for i in range(worker_count) for j in range(per_worker)}
    assert titles == expected_titles

    # JSON on disk is valid and complete, not partially written.
    tasks_file = tasks_repository.tasks_file_path(tmp_path)
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == worker_count * per_worker


def _mp_import_worker(data_dir: str, task_ids: list[str], barrier) -> None:
    os.environ["AICC_DATA_DIR"] = data_dir
    from command_center import task_import as _ti  # re-imported fresh in this process

    tasks = [
        {
            "id": tid,
            "title": f"Import {tid}",
            "goal": f"Goal for {tid}",
            "repository_path": "/repo",
            "workspace_path": "/repo",
            "branch": "main",
            "status": "Backlog",
            "project": "AIOS",
            "task_type": "implementation",
            "priority": "Medium",
            "depends_on": [],
        }
        for tid in task_ids
    ]
    parsed = _ti.parse_task_package(json.dumps(tasks))
    validation = _ti.validate_task_package(parsed)
    assert validation.errors == []
    barrier.wait()
    result = _ti.apply_task_package(Path(data_dir), parsed, validation)
    assert result.imported_ids == task_ids, result


def test_concurrent_manual_creation_and_package_import_never_lose_records(tmp_path):
    """The exact race the Founder Gate flagged as still open: a manual
    Kanban task creation racing a batch package import. Both write paths now
    share `tasks_repository.tasks_lock` — one process creates tasks the
    ordinary "Create Task" way, another imports a package, at the same
    synchronized instant. Nothing from either side may be lost."""
    data_dir = os.environ["AICC_DATA_DIR"]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)

    import_ids = [f"PKG-{i}" for i in range(10)]

    processes = [
        ctx.Process(target=_mp_create_worker, args=(data_dir, "AIOS", 10, "MANUAL", barrier)),
        ctx.Process(target=_mp_import_worker, args=(data_dir, import_ids, barrier)),
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker process failed (exitcode={p.exitcode})"

    stored = tasks_repository.load_tasks(tmp_path)
    ids = [t["id"] for t in stored]

    assert len(stored) == 20, f"expected 20 records (10 manual + 10 imported), got {len(stored)}"
    assert len(ids) == len(set(ids))
    assert set(import_ids) <= set(ids)
    manual_titles = {t["title"] for t in stored if t["title"].startswith("MANUAL")}
    assert len(manual_titles) == 10


def _mp_update_status_worker(data_dir: str, task_id: str, new_status: str, barrier) -> None:
    os.environ["AICC_DATA_DIR"] = data_dir
    from pathlib import Path as _Path

    from command_center import tasks_repository as _tr

    barrier.wait()
    result = _tr.update_task_status(_Path(data_dir), task_id, new_status)
    assert result is not None, f"task {task_id} not found for status update"


def _mp_delete_worker(data_dir: str, task_id: str, barrier) -> None:
    os.environ["AICC_DATA_DIR"] = data_dir
    from pathlib import Path as _Path

    from command_center import tasks_repository as _tr

    barrier.wait()
    _tr.delete_task(_Path(data_dir), task_id)


def test_create_update_delete_and_import_all_share_one_lock_and_lose_nothing(tmp_path):
    """The strongest proof: every verb the Definition of Done names — create,
    status change, delete, batch import — running concurrently as four
    genuinely separate OS processes against one store, all through
    `tasks_repository.tasks_lock`. Seeds two tasks up front (one to update,
    one to delete) outside the barrier so those two workers have a real
    target; the create and import workers add their own new tasks at the
    same synchronized instant. Final state must reflect every operation
    exactly: nothing lost, nothing duplicated, the deleted task gone, the
    updated task's new status visible."""
    data_dir = os.environ["AICC_DATA_DIR"]

    to_update = tasks_repository.create_task(tmp_path, "AIOS", "To be updated", "implementation", "Backlog")
    to_delete = tasks_repository.create_task(tmp_path, "AIOS", "To be deleted", "implementation", "Backlog")

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    import_ids = [f"MIX-PKG-{i}" for i in range(5)]

    processes = [
        ctx.Process(target=_mp_create_worker, args=(data_dir, "AIOS", 5, "MIXCREATE", barrier)),
        ctx.Process(target=_mp_import_worker, args=(data_dir, import_ids, barrier)),
        ctx.Process(target=_mp_update_status_worker, args=(data_dir, to_update["id"], "Review", barrier)),
        ctx.Process(target=_mp_delete_worker, args=(data_dir, to_delete["id"], barrier)),
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker process failed (exitcode={p.exitcode})"

    stored = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}

    # 2 seeded - 1 deleted + 5 created + 5 imported = 11
    assert len(stored) == 11, f"expected 11 records, got {len(stored)}"
    assert to_delete["id"] not in stored
    assert stored[to_update["id"]]["status"] == "Review"
    assert set(import_ids) <= set(stored.keys())
    created_titles = {t["title"] for t in stored.values() if t["title"].startswith("MIXCREATE")}
    assert len(created_titles) == 5

    tasks_file = tasks_repository.tasks_file_path(tmp_path)
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 11


def test_unlocked_read_seeding_never_erases_a_committed_record(tmp_path, monkeypatch):
    """The read path seeds a missing store — and it must never do so by
    overwriting.

    `JSONTasksRepository.load_all()` is called *outside* the store lock (see
    `task_import.apply_task_package`, which reads existing ids before it takes
    the lock for each create). When it observed a missing `tasks.json` it used
    to write the empty seed with `save_tasks(root, [])`, an unconditional
    `os.replace`. Between that observation and that write, a locked writer can
    commit a real record — and the seed then landed on top of it and erased it.
    Measured live in CI as the first task of a concurrently imported package
    vanishing while every writer reported success
    (VOYN-W0-AICC-TASK-IMPORT-CONCURRENCY-FLAKE).

    The window is made deterministic here rather than raced for: the reader is
    held at exactly the point where it has decided the file is absent, the
    writer commits, and only then is the reader allowed to continue. Without
    the exclusive-create fix this fails with the committed record gone.
    """
    reader_saw_missing = threading.Event()
    writer_committed = threading.Event()

    real_exists = Path.exists

    def exists_with_the_race_window_held_open(self: Path) -> bool:
        if self.name == "tasks.json" and not reader_saw_missing.is_set():
            reader_saw_missing.set()
            # The writer commits its record inside this window.
            assert writer_committed.wait(timeout=30), "writer did not commit"
            return False  # the reader proceeds on its now-stale observation
        return real_exists(self)

    def commit_under_the_lock() -> None:
        assert reader_saw_missing.wait(timeout=30), "reader never reached the window"
        tasks_repository.create_task(tmp_path, "AIOS", "committed", "implementation", "Backlog")
        writer_committed.set()

    writer = threading.Thread(target=commit_under_the_lock)
    writer.start()
    try:
        monkeypatch.setattr(Path, "exists", exists_with_the_race_window_held_open)
        tasks_repository.load_tasks(tmp_path)
    finally:
        monkeypatch.undo()
        writer.join(timeout=30)
    assert not writer.is_alive(), "writer thread did not finish"

    titles = [t["title"] for t in tasks_repository.load_tasks(tmp_path)]
    assert titles == ["committed"], "the unlocked read path erased a committed record"
