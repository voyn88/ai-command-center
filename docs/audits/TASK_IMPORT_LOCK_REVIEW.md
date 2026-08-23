# Task-import lock review

**Task:** `VOYN-W0-AICC-REPORT-229` (`task-import-lock-review`), Wave 0, P2.
**Scope:** the locking discipline of the task-package import path —
`command_center/task_import.py::apply_task_package`, the `tasks_repository`
lock primitives it builds on, and the two callers that surface its failures
(`scripts/import_tasks.py`, `app.py`'s Create Task page).
**Reviewed at:** `5010b46`.

## Verdict

The lock *primitive* is sound. `tasks_repository.tasks_lock` is a real OS
advisory lock (`fcntl.flock` / `msvcrt.locking` via `storage.file_lock`),
`mutate_tasks` holds it across the whole load-mutate-save cycle, and
`tests/test_task_import_concurrency.py` proves cross-process exclusion with
genuinely separate interpreters. None of that is in question.

What is wrong is the layer above it. `apply_task_package` stopped being a
single transaction, and **nothing in the codebase said so** — five separate
places still described the pre-refactor behavior, one of them a comment
promising an operator that "nothing was written" at the exact moment
something had been. One consequence was a live defect: a concurrent id claim
crashed the importer with an uncaught traceback instead of an ordinary error.

That defect and the documentation drift are fixed in this change. The
underlying design question — whether the import should be atomic again — is
**left open for a decision**; see [Finding 2](#finding-2--the-import-is-no-longer-one-transaction-open).

## What is *not* at risk

Worth stating plainly, because the findings below could be misread as worse
than they are:

- **No duplicate ids.** `JSONTasksRepository.create` re-checks for a
  colliding id *under the lock* and refuses. A racing writer cannot produce
  two records with the same id.
- **No lost updates on any individual write.** Every single write still goes
  through `mutate_tasks`, so no writer's read-half and write-half can be
  interleaved by another.
- **No torn or truncated `tasks.json`.** `save_tasks` is `tempfile` +
  `os.replace`, and `mutate_tasks` loads with `strict=True` so a bad read
  raises rather than overwriting the store with a wrongly-empty list.

The findings are about *batch* atomicity and about the code lying regarding
which of these guarantees it offers.

## Finding 1 — a concurrent id claim escaped as an uncaught `ValueError` (Major, **fixed**)

`apply_task_package` reads the store once, **without the lock**, to decide
which package ids are new:

```python
existing_ids = {t["id"] for t in _repo.load_all() if t.get("id")}
```

and only then enters its create loop. A writer that claims one of those ids
in between is caught by `create`'s under-lock collision guard — correctly —
but that guard raises a bare `ValueError`, and the loop's only handler caught
`storage.LockTimeoutError`. So the `ValueError` propagated straight out of
`apply_task_package`.

Neither caller catches it. Both catch `TaskImportError` and nothing else:

- `scripts/import_tasks.py:118` → traceback, exit code 1 instead of the
  intended 2.
- `app.py:2066` → uncaught exception on the Create Task page, directly
  contradicting that handler's own comment ("never an uncaught exception;
  nothing was written in either case").

Reproduced deterministically by committing the second package id through the
ordinary locked path between the unlocked read and the create loop:

```
=== calling apply_task_package ===
RAISED builtins.ValueError: refusing to create task with colliding id: 'PKG-B'
  ^ NOT a TaskImportError -> escapes CLI/UI handlers

store now holds: ['PKG-A', 'PKG-B']
```

Note `PKG-A`: the first task was already committed and stays committed.

**Fix.** The create loop now converts a store refusal into `TaskImportError`,
naming the task that could not be written. `TaskImportError` gained an
`imported_ids` attribute carrying exactly what was committed before the
failure, and the message repeats it, so neither caller can report a clean
abort when tasks are on disk:

```
RAISED TaskImportError (caught by CLI/UI): импорт прерван на задаче 'PKG-B':
хранилище отклонило запись (refusing to create task with colliding id: 'PKG-B').
Уже записано задач: 1 (PKG-A) — они остаются в хранилище.
```

The same partial-state reporting was added to the pre-existing lock-timeout
path, which previously raised an error that — by its own docstring's
admission — "does not carry a count of how many tasks succeeded".

## Finding 2 — the import is no longer one transaction (Major, **open — decision required**)

`98d7714` ("Add transactional task import and shared task storage locking")
shipped the remediation for Founder audit `AICC-AUDIT-001` item 2: the whole
import ran inside one `mutate_tasks` call, one lock acquisition, one write.
`tests/test_task_import_concurrency.py`'s module docstring still states that
requirement as the reason the file exists — *"`apply_task_package` must hold a
real cross-process lock across its **entire** read-modify-write cycle, not
just around the final write."*

`14c13b8` ("feat(tasks): wire app.py + task_import.py through
get_repository() factory") reverted it. From its own commit message:

> In task_import.py, replace the single mutate_tasks() call in
> apply_task_package with individual get_repository(root).create() calls
> per new task; also update
> **test_apply_performs_a_single_save_tasks_call to
> test_apply_writes_all_tasks_to_store** reflecting that the new
> implementation does one locked write per task (not one batch write).

The test that guarded the audited invariant was not made to fail — it was
rewritten to assert the new behavior. That is why the regression is three
commits old and still unflagged.

The stated justification (in `apply_task_package`'s docstring) is that the
AIOS backend has no batch-write primitive, so a per-task `create()` is the
only shape both backends can share. That is a legitimate engineering reason,
and this review does **not** unilaterally reverse it — restoring atomicity
means either dropping the shared code path or giving the ports layer a batch
primitive, which is a design call, not a cleanup.

What it costs today, all following from the same non-atomicity:

1. **Partial imports are reachable.** Any failure part-way leaves earlier
   tasks written. Now reported honestly (Finding 1) rather than silently.
2. **Concurrent writers interleave mid-package.** A Kanban edit or a second
   import can land between two tasks of the same package.
3. **The dependency check can go stale** *(by inspection, not reproduced)*.
   `depends_on` is validated against the unlocked snapshot; a concurrent
   delete of a dependency id between that read and the create loop lets a
   task import whose prerequisite no longer exists — the precise outcome the
   default `allow_unresolved_dependencies=False` policy exists to prevent
   ("never silently import a task that will stay blocked forever").

**Recommended disposition:** accept the trade-off explicitly and track it, or
schedule a batch-write primitive on the ports layer. Either way the audit
trail needs a row, because `AICC-AUDIT-001` item 2 is currently recorded as
remediated and is not.

## Finding 3 — five sites documented locking the code does not do (Minor, **fixed**)

`350ce18` ("fix(tasks): update apply_task_package docstring to reflect
N-creates behavior") corrected the *function* docstring after `14c13b8`, and
stopped there. Everything else still described the transactional version:

| Site | Claim | Reality |
| --- | --- | --- |
| `task_import.py` module docstring | "the entire load → … → save cycle runs inside `tasks_repository.mutate_tasks`" | N separate `create()` calls; `mutate_tasks` not called at all |
| same | "can therefore never race each other and lose an update, any more than two concurrent imports can" | true per write, false per package |
| `IMPORT_LOCK_TIMEOUT_SECONDS` comment | "still just forwarded to `mutate_tasks`'s own `timeout`" | forwarded to each `create()`; bounds one wait, so an N-task package can wait up to N × it |
| `tasks_repository.tasks_lock` docstring | "a manual Kanban edit/creation and a package import can never race each other" | only a *single* create from that import |
| `scripts/import_tasks.py` docstring | "commits the new tasks in a single write" | one write per task |
| `app.py:2067` comment | "nothing was written in either case" | false whenever the loop failed after task 1 |

All six corrected, and each now points at the real behavior rather than
merely deleting the claim.

## Finding 4 — the window between the unlocked read and the create loop was untested (Minor, **fixed**)

`tests/test_task_import.py::test_apply_does_not_duplicate_a_task_written_
concurrently_between_validate_and_apply` covers a write landing *before*
`apply_task_package`'s own read — which the fresh read absorbs correctly.
The window *after* that read, which is the one `14c13b8` opened, had no
coverage at all; that is why Finding 1 survived.

Four tests added to `tests/test_task_import_concurrency.py`, using a
deterministic interleaving (another writer commits through the ordinary
locked path during the first `create()`):

- `test_id_claimed_between_read_and_create_raises_task_import_error`
- `test_partial_import_failure_reports_the_tasks_it_already_committed`
- `test_collision_on_the_first_task_reports_an_empty_committed_list`
- `test_task_import_error_defaults_to_no_committed_ids`

Verified to fail against the unfixed code with the raw
`ValueError: refusing to create task with colliding id`, and to pass with the
fix.

## Evidence

```
tests/test_task_import_concurrency.py ....  12 passed
tests/test_task_import.py
tests/test_task_import_formats.py
tests/test_import_tasks_cli.py .........    85 passed
```

Baseline for `test_task_import_concurrency.py` was 8 tests; 4 added.
