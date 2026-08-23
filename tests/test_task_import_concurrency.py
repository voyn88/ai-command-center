"""Regression coverage for the Founder Review remediation of AICC-AUDIT-001,
item 2: `apply_task_package` must hold a real cross-process lock across its
*entire* read-modify-write cycle, not just around the final write.

Mirrors `tests/test_portfolio_registry_concurrency.py`'s structure closely —
same class of bug (`tempfile` + `os.replace` makes a single write atomic but
does nothing to prevent two concurrent read-modify-write cycles from losing
one another's update), same fix shape (`storage.file_lock`, now shared by
both modules — see `command_center.storage`'s module docstring).

A thread-only test proves the lock serializes two importers inside one
process; the `multiprocessing` (spawn context) test at the bottom is the one
that actually proves cross-process exclusion — two genuinely separate OS
processes, no shared interpreter, no GIL to accidentally save us. Per the
remediation brief, a test that only mutates the store between `preview` and
`apply` (already covered in `tests/test_task_import.py`) is not accepted as
sufficient proof of concurrency safety on its own — this file is the
required, stronger proof.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from command_center import models, storage, task_import as ti, tasks_repository


def _task(task_id: str, **overrides) -> dict:
    base = {
        "id": task_id,
        "title": f"Task {task_id}",
        "goal": f"Goal for {task_id}",
        "repository_path": "/repo",
        "workspace_path": "/repo",
        "branch": "main",
        "status": "Backlog",
        "project": "AIOS",
        "task_type": "implementation",
        "priority": "Medium",
        "depends_on": [],
    }
    base.update(overrides)
    return base


def _package(*task_ids: str) -> tuple:
    parsed = ti.parse_task_package(json.dumps([_task(tid) for tid in task_ids]))
    validation = ti.validate_task_package(parsed)
    assert validation.errors == []
    return parsed, validation


# --------------------------------------------------------------------------
# The bug this module guards against: two concurrent read-modify-write
# cycles on `tasks.json`, without a lock spanning the whole cycle, losing one
# import's tasks. `_unsafe_import` deliberately bypasses `apply_task_package`
# and reproduces the pre-fix pattern (`load_tasks` -> mutate -> `save_tasks`,
# no lock around the pair) — not production code, exists only to prove the
# race is real and that the fix below closes it.
# --------------------------------------------------------------------------


def _unsafe_import(root: Path, task_ids: list[str], barrier: threading.Barrier) -> None:
    parsed, validation = _package(*task_ids)
    existing = tasks_repository.load_tasks(root)
    barrier.wait()  # force both threads to have read the same pre-write state
    now = models.iso_now()
    new_tasks = [ti._build_task_record(item, parsed, now) for item in validation.items]
    tasks_repository.save_tasks(root, existing + new_tasks)


def test_unsafe_read_modify_write_pattern_loses_tasks(tmp_path):
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_unsafe_import, args=(tmp_path, ["UNSAFE-A-1", "UNSAFE-A-2"], barrier)),
        threading.Thread(target=_unsafe_import, args=(tmp_path, ["UNSAFE-B-1", "UNSAFE-B-2"], barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    stored = tasks_repository.load_tasks(tmp_path)
    assert len(stored) < 4, "expected the unguarded read-modify-write to lose at least one package's tasks"


# --------------------------------------------------------------------------
# The fix: `apply_task_package`'s `storage.file_lock`-guarded transaction
# --------------------------------------------------------------------------


@pytest.mark.serial  # threaded lock-deadline (join timeout=15); flaky when xdist saturates all cores
def test_two_different_packages_imported_concurrently_both_survive_threaded(tmp_path):
    start = threading.Barrier(2)
    errors: list[Exception] = []
    results: dict[str, ti.ImportResult] = {}

    def run(name: str, task_ids: list[str]) -> None:
        parsed, validation = _package(*task_ids)
        start.wait()
        try:
            results[name] = ti.apply_task_package(tmp_path, parsed, validation)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("A", ["THREAD-A-1", "THREAD-A-2", "THREAD-A-3"])),
        threading.Thread(target=run, args=("B", ["THREAD-B-1", "THREAD-B-2", "THREAD-B-3"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert len(results["A"].imported_ids) == 3
    assert len(results["B"].imported_ids) == 3

    stored = tasks_repository.load_tasks(tmp_path)
    assert len(stored) == 6
    ids = [t["id"] for t in stored]
    assert len(ids) == len(set(ids)), "duplicate ids after concurrent import"
    assert set(ids) == {
        "THREAD-A-1", "THREAD-A-2", "THREAD-A-3", "THREAD-B-1", "THREAD-B-2", "THREAD-B-3",
    }


def test_lock_is_released_after_exception_inside_the_critical_section(tmp_path, monkeypatch):
    """A failure mid-transaction must not leave the lock held forever."""
    parsed, validation = _package("EXC-1")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-transaction")

    monkeypatch.setattr(tasks_repository, "save_tasks", boom)
    with pytest.raises(RuntimeError):
        ti.apply_task_package(tmp_path, parsed, validation)

    monkeypatch.undo()
    # Must be immediately re-acquirable — a short timeout would fail if the
    # exception above had left the lock held.
    parsed2, validation2 = _package("EXC-2")
    result = ti.apply_task_package(tmp_path, parsed2, validation2, lock_timeout=2.0)
    assert result.imported_ids == ["EXC-2"]


def test_import_lock_times_out_with_a_clear_error_instead_of_hanging(tmp_path):
    """If the lock is genuinely held elsewhere — by another import, or by any
    other write path sharing `tasks_repository.tasks_lock` — a caller must
    get a bounded, descriptive failure — never an indefinite hang."""
    lock_path = tasks_repository.tasks_lock_path(tmp_path)
    with storage.file_lock(lock_path, timeout=5.0):
        parsed, validation = _package("TIMEOUT-1")
        start = time.monotonic()
        with pytest.raises(ti.TaskImportError, match="блокировку"):
            ti.apply_task_package(tmp_path, parsed, validation, lock_timeout=0.3)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, "apply_task_package must not block far past its own timeout"

    # Once released, an import proceeds normally — no manual recovery needed.
    result = ti.apply_task_package(tmp_path, parsed, validation)
    assert result.imported_ids == ["TIMEOUT-1"]


@pytest.mark.serial  # many concurrent registry writers on a lock deadline; flaky when xdist saturates all cores
def test_registry_stays_valid_json_after_many_concurrent_imports(tmp_path):
    task_id_batches = [[f"BULK-{i}-1", f"BULK-{i}-2"] for i in range(8)]
    start = threading.Barrier(len(task_id_batches))

    def run(task_ids: list[str]) -> None:
        parsed, validation = _package(*task_ids)
        start.wait()
        ti.apply_task_package(tmp_path, parsed, validation)

    threads = [threading.Thread(target=run, args=(batch,)) for batch in task_id_batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    tasks_file = tasks_repository.tasks_file_path(tmp_path)
    data = json.loads(tasks_file.read_text(encoding="utf-8"))  # raises if partially written
    assert isinstance(data, list)
    expected_ids = {tid for batch in task_id_batches for tid in batch}
    assert {t["id"] for t in data} == expected_ids
    assert len(data) == len(expected_ids)


def test_no_leftover_temp_files_after_concurrent_imports(tmp_path):
    task_id_batches = [[f"TMP-{i}-1"] for i in range(5)]
    start = threading.Barrier(len(task_id_batches))

    def run(task_ids: list[str]) -> None:
        parsed, validation = _package(*task_ids)
        start.wait()
        ti.apply_task_package(tmp_path, parsed, validation)

    threads = [threading.Thread(target=run, args=(batch,)) for batch in task_id_batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    data_dir = tasks_repository.tasks_file_path(tmp_path).parent
    assert list(data_dir.glob("*.tmp")) == []


# --------------------------------------------------------------------------
# Cross-process exclusion — the reason this uses an OS advisory file lock
# (`fcntl.flock` / `msvcrt.locking`, via `storage.file_lock`) rather than
# `threading.Lock`, which would only serialize threads inside one interpreter
# and do nothing for two separate `streamlit run app.py` / `import_tasks.py`
# processes sharing the same `data/tasks.json`. This is the test the
# remediation brief explicitly requires: two independent OS processes
# importing two different packages concurrently, proving the union survives,
# nothing is lost, the JSON stays valid, and there are no duplicate ids.
# --------------------------------------------------------------------------


def _mp_import_worker(data_dir: str, task_ids: list[str], barrier) -> None:
    os.environ["AICC_DATA_DIR"] = data_dir
    import json as _json

    from command_center import task_import as _ti  # re-imported fresh in this process

    tasks = [
        {
            "id": tid,
            "title": f"Task {tid}",
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
    parsed = _ti.parse_task_package(_json.dumps(tasks))
    validation = _ti.validate_task_package(parsed)
    assert validation.errors == []

    barrier.wait()
    result = _ti.apply_task_package(Path(data_dir), parsed, validation)
    assert result.imported_ids == task_ids, result


def test_apply_task_package_provides_real_cross_process_exclusion(tmp_path):
    """Two genuinely separate OS processes, each importing a different
    package, started at the same instant via a real `multiprocessing.
    Barrier`. After both finish: the store contains the union of both
    packages, no task is lost, the JSON is valid, and there are no duplicate
    ids — exactly what the remediation brief requires and what a same-process
    thread test alone cannot prove (no shared interpreter, no GIL)."""
    data_dir = os.environ["AICC_DATA_DIR"]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)

    package_a = ["MP-A-1", "MP-A-2", "MP-A-3"]
    package_b = ["MP-B-1", "MP-B-2", "MP-B-3"]

    processes = [
        ctx.Process(target=_mp_import_worker, args=(data_dir, package_a, barrier)),
        ctx.Process(target=_mp_import_worker, args=(data_dir, package_b, barrier)),
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        assert p.exitcode == 0, f"worker process failed (exitcode={p.exitcode})"

    stored = tasks_repository.load_tasks(tmp_path)
    ids = [t["id"] for t in stored]

    # Union of both packages present — no task lost.
    assert set(ids) == set(package_a) | set(package_b)
    # No duplicate ids.
    assert len(ids) == len(set(ids))
    # JSON on disk is valid and well-formed (not partially written).
    tasks_file = tasks_repository.tasks_file_path(tmp_path)
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 6


def _mp_hold_lock_then_die(data_dir: str, ready_event, lock_held_ack) -> None:
    """Acquires the import lock and then exits the process *without*
    releasing it explicitly (no clean `finally`/context-manager unwind from
    the caller's side) — simulating a hard crash while holding the lock, to
    prove a subsequent import is never permanently blocked by a dead holder."""
    os.environ["AICC_DATA_DIR"] = data_dir
    from pathlib import Path as _Path

    from command_center import storage as _storage
    from command_center import tasks_repository as _tr

    lock_path = _tr.tasks_lock_path(_Path(data_dir))
    handle = open(lock_path, "a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
    assert _storage._try_acquire_lock(handle)
    lock_held_ack.set()
    ready_event.wait(timeout=10)
    os._exit(1)  # hard exit — no atexit/finally handlers, no lock release call


def test_a_crashed_lock_holder_never_permanently_blocks_a_later_import(tmp_path):
    """The lock is an OS advisory lock released by the kernel the instant the
    holding process's file descriptor closes — including on a crash/kill —
    so there is no "stale lock" state an operator must manually clear (see
    `storage.file_lock`'s docstring). This proves it end to end: a real OS
    process crashes while holding the lock, and a subsequent import still
    succeeds promptly."""
    data_dir = os.environ["AICC_DATA_DIR"]
    ctx = multiprocessing.get_context("spawn")
    lock_held_ack = ctx.Event()
    ready_event = ctx.Event()

    holder = ctx.Process(target=_mp_hold_lock_then_die, args=(data_dir, ready_event, lock_held_ack))
    holder.start()
    assert lock_held_ack.wait(timeout=10), "lock-holding process never signalled it had the lock"

    ready_event.set()  # tell the holder to hard-exit now
    holder.join(timeout=10)
    assert holder.exitcode == 1

    parsed, validation = _package("AFTER-CRASH-1")
    start = time.monotonic()
    result = ti.apply_task_package(tmp_path, parsed, validation, lock_timeout=10.0)
    elapsed = time.monotonic() - start
    assert result.imported_ids == ["AFTER-CRASH-1"]
    assert elapsed < 5.0, "import after a crashed lock holder should not need to wait out the full timeout"


# --------------------------------------------------------------------------
# The window the per-task-`create()` refactor (14c13b8) opened, and which no
# existing test covered: `apply_task_package` reads the store *once, without
# the lock*, decides which ids are new, and only then starts its create loop.
# Another writer that claims one of those ids in between is caught by
# `JSONTasksRepository.create`'s own under-lock collision guard — so no
# duplicate is ever written — but that guard raises a bare `ValueError`,
# which is not what the CLI (`scripts/import_tasks.py`) or the Create Task
# page (`app.py`) catch. The tests below pin the two things that must hold:
# the failure arrives as `TaskImportError`, and it tells the truth about the
# tasks the loop had already committed before it hit the collision.
#
# `tests/test_task_import.py`'s
# `test_apply_does_not_duplicate_a_task_written_concurrently_between_validate_
# and_apply` covers the *earlier* window (before `apply_task_package`'s own
# read); this covers the one after it.
# --------------------------------------------------------------------------


def _claim_id_during_create(monkeypatch, claimed_id: str) -> None:
    """Make the first `create()` of an import be preceded by another writer
    committing `claimed_id` through the ordinary locked path — i.e. exactly
    the interleaving a second process can produce, made deterministic."""
    real_create = tasks_repository.JSONTasksRepository.create
    fired: list[bool] = []

    def racing_create(self, task_dict, *, timeout=tasks_repository.TASKS_LOCK_TIMEOUT_SECONDS):
        if not fired:
            fired.append(True)
            real_create(self, _task(claimed_id), timeout=timeout)
        return real_create(self, task_dict, timeout=timeout)

    monkeypatch.setattr(tasks_repository.JSONTasksRepository, "create", racing_create)


def test_id_claimed_between_read_and_create_raises_task_import_error(tmp_path, monkeypatch):
    parsed, validation = _package("WINDOW-A", "WINDOW-B")
    _claim_id_during_create(monkeypatch, "WINDOW-B")

    with pytest.raises(ti.TaskImportError) as excinfo:
        ti.apply_task_package(tmp_path, parsed, validation)

    # Names the task that could not be written, so the operator can tell this
    # apart from a lock timeout without reading a traceback.
    assert "WINDOW-B" in str(excinfo.value)


def test_partial_import_failure_reports_the_tasks_it_already_committed(tmp_path, monkeypatch):
    parsed, validation = _package("WINDOW-A", "WINDOW-B")
    _claim_id_during_create(monkeypatch, "WINDOW-B")

    with pytest.raises(ti.TaskImportError) as excinfo:
        ti.apply_task_package(tmp_path, parsed, validation)

    # WINDOW-A was committed before the collision and is NOT rolled back;
    # the exception must say so rather than imply a clean abort.
    assert excinfo.value.imported_ids == ("WINDOW-A",)
    assert "WINDOW-A" in str(excinfo.value)

    stored_ids = {t["id"] for t in tasks_repository.load_tasks(tmp_path)}
    assert stored_ids == {"WINDOW-A", "WINDOW-B"}


def test_collision_on_the_first_task_reports_an_empty_committed_list(tmp_path, monkeypatch):
    parsed, validation = _package("WINDOW-ONLY")
    _claim_id_during_create(monkeypatch, "WINDOW-ONLY")

    with pytest.raises(ti.TaskImportError) as excinfo:
        ti.apply_task_package(tmp_path, parsed, validation)

    assert excinfo.value.imported_ids == ()


def test_task_import_error_defaults_to_no_committed_ids(tmp_path):
    """Every raise site that fires before the create loop leaves
    `imported_ids` empty — nothing was written, and callers can rely on
    that without special-casing which failure they caught."""
    parsed = ti.parse_task_package(json.dumps([_task("VALIDATION-FAIL", status="Не бывает")]))
    validation = ti.validate_task_package(parsed)
    assert validation.errors != []

    with pytest.raises(ti.TaskImportError) as excinfo:
        ti.apply_task_package(tmp_path, parsed, validation)

    assert excinfo.value.imported_ids == ()
