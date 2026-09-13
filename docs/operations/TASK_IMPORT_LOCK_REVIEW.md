# Task-import lock review (VOYN-W0-AICC-REPORT-229)

Status: **Reviewed.** This is a review, not a remediation — see "Finding" below for the one
item that should become a follow-up task rather than being fixed here.

## Scope

Of everything `pr229.diff` originally touched, only two items are still live against current
`main`:

1. Is the timeout-based repository lock `command_center/task_import.py` uses for
   `apply_task_package` sufficient, compared with the registry-lock mechanism
   `command_center/portfolio_launch.py` uses for `data/portfolio_launches.json`?
2. `pr229.diff`'s `.gitignore` change.

Everything else `pr229.diff` covered has already landed on `main` independently (the shared
`tasks_lock`/`mutate_tasks` machinery itself, `tests/test_task_import_concurrency.py`) and is
out of scope here.

## 1. The current mechanism

`task_import.apply_task_package` does not hold a dedicated import lock. `IMPORT_LOCK_TIMEOUT_SECONDS`
(`task_import.py:105`) is just `tasks_repository.TASKS_LOCK_TIMEOUT_SECONDS` (30s) forwarded through —
the lock itself is `tasks_repository.tasks_lock`, the same OS advisory `fcntl.flock`/`msvcrt.locking`
lock (`storage.file_lock`, backed by `data/tasks.lock`) that every other task-store writer
(`create_task`, `update_task_status`, `delete_task`, a manual Kanban edit) acquires through
`mutate_tasks`. A package import and a manual edit therefore always serialize against each other;
neither can read a pre-write snapshot the other is about to overwrite.

`apply_task_package` computes duplicates/dependencies from one `load_all()` snapshot, then persists
each new task with its own `JSONTasksRepository.create()` call — a separate `tasks_lock` acquire/
release per task (documented trade-off, `task_import.py:591-596`, needed because the AIOS backend has
no batch-write primitive). Each `create()` re-checks the id for a collision against a *fresh* load
taken under its own lock (`tasks_repository.py:558-561`), not the caller's stale snapshot, so a
genuine concurrent duplicate is still rejected rather than silently double-inserted.

This is exercised well beyond a single-process check: `tests/test_task_import_concurrency.py` proves
two threads importing disjoint packages both survive, two independent OS *processes*
(`multiprocessing`, spawn context) importing disjoint packages both survive with the union intact,
a lock held elsewhere times out with a clear `TaskImportError` instead of hanging, and — the case
that matters most for "sufficiency" — a process that acquires the lock and then hard-exits
(`os._exit`, no `finally`) never wedges a later import, because the lock is released by the kernel
the instant the crashed process's file descriptor closes.

## 2. Comparison against `_registry_lock` (`portfolio_launch.py`)

Portfolio launches use a **two-tier** lock: an exclusive-create claim-lock file per `task_id` in
`data/portfolio_locks/` (prevents double-dispatching the same subprocess launch) plus
`_registry_lock`, an `fcntl.flock` guarding the read-modify-write cycle on
`data/portfolio_launches.json`, with a re-check under the lock. The claim-lock tier exists to solve
a different problem than `tasks.json` writes: "has this exact launch already started," not "did two
writers race on the same file."

`task_import` has no equivalent of that first tier, and does not need one — it is not preventing a
side-effecting action (a subprocess launch) from running twice, only serializing CRUD on a single
JSON file, which `tasks_lock` already does for every other writer. Reusing the shared lock (rather
than adding an import-specific one) is also what keeps a package import and a concurrent manual
Kanban edit from losing an update to each other — a second, independent lock would not provide that.

One consequence is actually in `task_import`'s favor: the claim-lock tier is an *exclusive-create
marker file*, which is exactly the kind of lock `storage.file_lock`'s own docstring calls out as
having a "stale lock" problem a plain advisory flock does not — and the Founder audit confirms it in
practice (`docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459.md` MAJOR-6 / A-8: a portfolio claim-lock
orphaned by a crash between claim and release stays stuck until an operator deletes the file by
hand). `tasks_lock` carries no such marker-file state; §1's crash test
(`test_a_crashed_lock_holder_never_permanently_blocks_a_later_import`) is the same scenario for
`task_import` and it recovers automatically, no operator step required.

**Verdict: sufficient.** The plain shared `tasks_lock`, held via `mutate_tasks`/`create()`, is the
right-sized mechanism for `task_import`'s actual problem (serialize writes to one JSON file across
every writer) and is strictly more crash-resilient than `portfolio_launch`'s heavier two-tier
mechanism, which is solving a genuinely different problem (prevent double-launch) that
`task_import` doesn't have.

### Finding (follow-up, not fixed here)

The per-task `create()` trade-off in §1 has one gap the existing tests don't cover: if two
overlapping packages (sharing a task id) are applied concurrently and the collision lands *after*
`apply_task_package`'s own `existing_ids` snapshot but *during* its `create()` loop, `create()` raises
a bare `ValueError` (`tasks_repository.py:561`) that `apply_task_package`'s
`except storage.LockTimeoutError` (`task_import.py:654`) does not catch — it propagates as a raw
`ValueError` instead of the `TaskImportError` every other import failure surfaces as, and the
existing "partial-import on lock timeout" docstring note doesn't cover this failure mode. The lock
itself still does its job (no duplicate is ever persisted), so this is a narrow error-contract gap,
not a locking-sufficiency gap. Worth a small follow-up: wrap `ValueError` alongside
`LockTimeoutError` in `apply_task_package` and extend the partial-import docstring note to cover it.

## 3. The `.gitignore` part is outdated

`pr229.diff`'s `.gitignore` change added `data/tasks.lock` by name (this matches
`98d7714`, "Add transactional task import and shared task storage locking", which introduced the
lock file alongside the import pipeline). That by-name entry was already superseded on `main` before
this review: `cc4b41f` ("chore: commit audit docs and state update", 2026-08-07) replaced every
by-name `data/*.lock` entry — `data/tasks.lock`, `data/execution_queue.lock`,
`data/pipeline_settings.lock`, `data/task_pipeline.lock` — with the single glob rule `data/*.lock`
(`.gitignore:14`), specifically so a new lock file added by a future feature can't be missed again.

**No action needed.** `data/tasks.lock` is already covered by the current glob; re-applying
`pr229.diff`'s specific line would be redundant.

## References

- `command_center/task_import.py` — `apply_task_package`, `IMPORT_LOCK_TIMEOUT_SECONDS`
- `command_center/tasks_repository.py` — `tasks_lock`, `mutate_tasks`, `JSONTasksRepository.create`
- `command_center/portfolio_launch.py` — `_registry_lock`, `_claim`
- `command_center/storage.py` — `file_lock`, `LockTimeoutError`
- `tests/test_task_import_concurrency.py` — cross-thread/cross-process/crash-recovery coverage
- `docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459.md` — MAJOR-6 / A-8 (portfolio claim-lock has no
  stale-lock recovery)
- `.gitignore` (`data/*.lock`), `cc4b41f` (glob generalization)
