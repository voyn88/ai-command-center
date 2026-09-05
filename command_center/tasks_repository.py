"""Task persistence (`data/tasks.json`) as a plain-Python repository, with
zero Streamlit coupling.

`app.py`'s `load_tasks`/`save_tasks`/`new_task_record` are thin wrappers
around this module (same names, same behavior, `ROOT`/`TASKS_FILE` bound in
from `app.py`'s own constants) — this mirrors `docs/desktop/ARCHITECTURE.md`
§14 ("Run/session/report/task state is owned entirely by... the existing
v1.2 JSON/JSONL stores"): a future desktop adapter reads/writes tasks
through this module, never through a second store or by touching the JSON
file directly.

Every *write* to `tasks.json` — creating a task, changing its status,
deleting it, a manual launch-status toggle, a batch package import
(`command_center.task_import.apply_task_package`), any future bulk
operation — must go through `mutate_tasks` (or one of the verb-named
helpers below that are themselves built on it), never a hand-rolled
`load_tasks()` ... mutate ... `save_tasks()` sequence. `save_tasks`'s
`tempfile` + `os.replace` makes a single write atomic, but does nothing to
prevent a *lost update* across a read-modify-write cycle: two callers that
each `load_tasks()` before either `save_tasks()`s will silently discard one
another's change, whichever writes last "winning" with a snapshot that
never saw the other's update. This is exactly the shape of the bug
Founder Gate reproduced (a batch import racing a manual Kanban edit or
another concurrent import, expected record count higher than what actually
landed on disk) — `mutate_tasks` closes it by holding `tasks_lock` (an OS
advisory `fcntl.flock`/`msvcrt.locking` lock — see `storage.file_lock`'s
docstring) across the *entire* load-mutate-save cycle, for every write path
in this application, so no two writers can ever interleave their read and
write halves.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os as _os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from command_center import models, storage, task_view

if TYPE_CHECKING:
    from command_center.application.aios_tasks import AIOSTasksRepository

T = TypeVar("T")

logger = logging.getLogger(__name__)


def is_master_projection_task(task: dict) -> bool:
    """One control predicate for records owned by the master backlog view."""
    return task.get("source") == "master"

# Fields a task record cannot be useful without; a record missing any of these
# is surfaced by `validate_tasks` rather than silently accepted (audit BLOCKER-4).
REQUIRED_TASK_FIELDS: tuple[str, ...] = ("id", "project", "title", "status")

# `load_tasks` runs on nearly every Streamlit rerun, so an integrity warning
# there must not flood the log with the same line many times per second. Keyed
# on a content signature so a newly appearing problem still warns exactly once.
_warned_signatures: set[str] = set()


class CompletionEvidenceRequired(ValueError):
    """A planning mutation attempted to assert verified engineering delivery.

    ``Done`` releases dependent tasks, so only the completion projector may
    write it after result and target-branch verification.
    """


def _warn_once(signature: str, msg: str, *args: object) -> None:
    if signature in _warned_signatures:
        return
    _warned_signatures.add(signature)
    logger.warning(msg, *args)


TASKS_LOCK_FILE_NAME = "tasks.lock"
TASKS_LOCK_TIMEOUT_SECONDS = 30.0
_TASKS_LOCK_POLL_SECONDS = 0.05


def tasks_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / "tasks.json"


def tasks_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / TASKS_LOCK_FILE_NAME


def normalize_task(task: dict) -> dict:
    task.setdefault("priority", "Medium")
    task.setdefault("owner", "")
    task.setdefault("estimate_hours", 0.0)
    task.setdefault("depends_on", [])
    # Explicit priority-ordering rank (VOYN-W2-TASKS). Absent on historical rows
    # and on freshly created tasks; `task_ordering.default_order` treats a
    # missing rank as "unranked, sinks to the end", so the field only needs to
    # exist once an operator has reordered. `None` (not 0) is the unranked
    # sentinel — 0 is a legitimate top-of-list rank.
    task.setdefault("priority_rank", None)
    task.setdefault("updated_at", task.get("created_at", ""))
    models.normalize_task_workflow(task)
    models.normalize_task_execution(task)
    # Heal a legacy unbounded timeline on read; the next save persists the bound
    # (perf root cause — see models.MAX_TIMELINE_EVENTS).
    models.trim_timeline(task)
    return task


def _decode_tasks(tasks_file: Path) -> list[dict]:
    """Read and decode the tasks file, RAISING on an existing-but-corrupt or
    unreadable file rather than masking it as empty."""
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"tasks file is not a JSON list: {tasks_file}")
    return [normalize_task(task) for task in data]


def validate_tasks(tasks: list[dict]) -> list[str]:
    """Return a list of human-readable integrity problems in `tasks`, empty
    when the list is clean. This is surfacing, not mutation — the "warn instead
    of silently accept" the audit asked for (BLOCKER-4): a record whose
    `project` is not in `models.PROJECT_IDS` is otherwise silently dropped from
    every project-scoped view (Project Intelligence, health, filters) with no
    signal that the task exists at all. Checks each record for missing required
    fields and an unknown `project` (with a "did you mean …?" hint from
    `models.PROJECT_ALIASES`), and the list for duplicate ids (BLOCKER-3's
    precondition)."""
    issues: list[str] = []
    seen_ids: dict[str, int] = {}
    for index, task in enumerate(tasks):
        label = task.get("id") or task.get("title") or f"#{index}"
        for field in REQUIRED_TASK_FIELDS:
            if not task.get(field):
                issues.append(f"task {label!r}: missing required field {field!r}")
        project = task.get("project")
        if project and project not in models.PROJECT_IDS:
            hint = models.PROJECT_ALIASES.get(project)
            suffix = f" (did you mean {hint!r}?)" if hint else ""
            issues.append(f"task {label!r}: unknown project {project!r} not in registry{suffix}")
        task_id = task.get("id")
        if task_id:
            if task_id in seen_ids:
                issues.append(f"duplicate task id {task_id!r} (indices {seen_ids[task_id]} and {index})")
            else:
                seen_ids[task_id] = index
    return issues


def _dedupe_by_id(tasks: list[dict]) -> tuple[list[dict], list[str]]:
    """Keep the first record for each id and drop later duplicates, returning
    the deduped list and the ids that were dropped. `delete_task` removes a
    single record and `create_task` refuses a colliding id, so a duplicate can
    only originate from a hand-edited or out-of-band file; collapsing it on read
    (keep-first, deterministic) guarantees a downstream `delete_task` is never
    handed two records sharing an id — the second, independent guard behind
    BLOCKER-3. Records with no id are kept as-is (`validate_tasks` flags the
    missing field)."""
    seen: set[str] = set()
    deduped: list[dict] = []
    dropped: list[str] = []
    for task in tasks:
        task_id = task.get("id")
        if task_id and task_id in seen:
            dropped.append(task_id)
            continue
        if task_id:
            seen.add(task_id)
        deduped.append(task)
    return deduped, dropped


def load_tasks(root: Path, *, example_file: Path | None = None, strict: bool = False) -> list[dict]:
    """Load the task list. A missing file is created empty (or seeded from
    `example_file`) and read back as `[]` — that is a legitimate fresh store.
    That creation is exclusive (`storage.create_json_if_absent`), never an
    overwrite: this function is called on unlocked read paths, so seeding a
    store another writer is concurrently populating must never replace what
    that writer already committed.

    `strict` controls what happens when the file *exists* but cannot be decoded
    (transient `OSError`, a torn write, or non-JSON): the read-only default
    returns `[]` so a single bad read does not crash the UI, but the
    read-modify-write path (`mutate_tasks`) passes `strict=True` so a bad read
    RAISES instead of returning `[]`. Returning `[]` there and then saving would
    persist `[]` over the real list — the "one transient read error wipes
    tasks.json" data-loss amplification the audit flagged.

    Two integrity guards run on every successful read: duplicate ids are
    dropped keep-first (`_dedupe_by_id`), so a downstream `delete_task` can
    never be handed two records sharing an id (audit BLOCKER-3), and any
    remaining problem (unknown `project`, missing required field) is logged
    once via `validate_tasks` instead of being silently accepted (audit
    BLOCKER-4). Neither guard mutates the stored file; they normalise the
    in-memory view and surface the rest."""
    data_dir = storage.resolve_data_dir(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = tasks_file_path(root)
    if not tasks_file.exists():
        # Creation, never replacement. This runs on the *read* path — including
        # `JSONTasksRepository.load_all`, which `task_import.apply_task_package`
        # calls before it takes the store lock — so an unconditional write here
        # races every locked writer and can erase a record that was already
        # committed: `save_tasks(root, [])` was exactly that lost update (the
        # first task of a concurrently imported package disappeared while every
        # writer reported success — VOYN-W0-AICC-TASK-IMPORT-CONCURRENCY-FLAKE).
        # `create_json_if_absent` publishes only when the file is still absent,
        # so losing this race costs nothing but an unused temp file.
        if example_file and example_file.exists():
            # Copied verbatim, never parsed. Decoding the example here would
            # move its decode error *outside* the handler below, so a malformed
            # example would raise from a read that `strict=False` promises will
            # not (independent review of `4b058ff`). Copying keeps the failure
            # exactly where it was before: in `_decode_tasks`, under `strict`.
            storage.create_bytes_if_absent(tasks_file, example_file.read_bytes())
        else:
            storage.create_json_if_absent(tasks_file, [])
    try:
        tasks = _decode_tasks(tasks_file)
    except (json.JSONDecodeError, OSError, ValueError):
        if strict:
            raise
        return []
    tasks, dropped = _dedupe_by_id(tasks)
    if dropped:
        _warn_once(
            "dropped-dupes:" + ",".join(sorted(set(dropped))),
            "tasks.json: dropped %d duplicate task id(s) on load (kept first): %s",
            len(dropped),
            sorted(set(dropped)),
        )
    for issue in validate_tasks(tasks):
        _warn_once("integrity:" + issue, "tasks.json integrity: %s", issue)
    return tasks


def save_tasks(root: Path, tasks: list[dict]) -> None:
    # Delegate to the shared, fsync-ing `storage.atomic_write_json` primitive
    # rather than duplicating its temp-file + `os.replace` pattern without the
    # `fsync` (audit MINOR-10): every other JSON store in the project already
    # goes through it, so `tasks.json` gets the same on-disk durability.
    #
    # Master-projection records never persist (VOYN-W0-AICC-WIRE-BACKLOG-
    # API): the read-only board view stamps them `source: "master"`, and
    # dropping them HERE — the single point every write path funnels into
    # (mutate_tasks, upsert, upsert_all, create, status updates) — is what
    # makes "no second task store" structural rather than a convention.
    # A guard at any higher layer is bypassable by the next single-record
    # helper (independent review of 92a501f, findings 1-2: repo.upsert and
    # panel-wide save callbacks both walked straight past it). Dropping is
    # the correct semantics, not an error: a view record "saved" back is a
    # no-op by definition, and the panels keep working instead of dying on
    # a PermissionError mid-render.
    writable = [task for task in tasks if not is_master_projection_task(task)]
    dropped = [task.get("id") for task in tasks if is_master_projection_task(task)]
    if dropped:
        logger.warning(
            "tasks.json: ignored %d master projection record(s): %s",
            len(dropped),
            sorted(task_id for task_id in dropped if task_id),
        )
    storage.atomic_write_json(tasks_file_path(root), writable)


@contextlib.contextmanager
def tasks_lock(root: Path, *, timeout: float = TASKS_LOCK_TIMEOUT_SECONDS):
    """The single cross-process/cross-thread lock every write to
    `tasks.json` must hold for its *entire* read-modify-write cycle —
    shared by every mutation helper in this module and by
    `command_center.task_import.apply_task_package` (never a second,
    import-specific lock file), so a manual Kanban edit/creation and a
    package import can never race each other and lose an update. See
    `storage.file_lock`'s docstring for the underlying `fcntl`/`msvcrt`
    mechanism and why `save_tasks`'s own atomic write is not, by itself,
    sufficient."""
    with storage.file_lock(tasks_lock_path(root), timeout=timeout, poll_seconds=_TASKS_LOCK_POLL_SECONDS):
        yield


def mutate_tasks(
    root: Path,
    mutator: Callable[[list[dict]], T],
    *,
    timeout: float = TASKS_LOCK_TIMEOUT_SECONDS,
    persist_if: Callable[[T], bool] | None = None,
) -> T:
    """The single transactional primitive every write to `tasks.json` goes
    through: acquire `tasks_lock` -> load the *current* list fresh from disk
    (never a caller-held snapshot, which may already be stale by the time
    this runs — that staleness is exactly what causes a lost update) -> call
    `mutator(tasks)`, which mutates the list in place (append/remove/find-
    and-edit) and may return a value -> persist via one `save_tasks` call
    (skipped only if `persist_if` is given and returns falsy for the
    mutator's result — an opt-in for callers that want to write only when
    something actually changed, e.g. a polling reconciler) -> release.
    Returns whatever `mutator` returned.

    If `mutator` raises, nothing is saved and the exception propagates after
    the lock is released (the `with` block's `finally` always runs) — a
    partially-applied mutation is never persisted.

    Every verb-named helper below (`create_task`/`upsert_task`/
    `update_task_status`/`set_manual_launch_status`/`delete_task`) is built
    on this; so is `command_center.task_import.apply_task_package`.
    """
    with tasks_lock(root, timeout=timeout):
        # strict=True: if the existing file cannot be read/decoded, RAISE rather
        # than proceed against a wrongly-empty list — persisting the mutator's
        # result would then overwrite tasks.json with just the new record and
        # drop every other task (the audit's data-loss amplification).
        tasks = load_tasks(root, strict=True)
        result = mutator(tasks)
        if persist_if is None or persist_if(result):
            save_tasks(root, tasks)
        return result


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
    """`title` is the short, dedicated heading (Название задачи); `goal`
    (Цель задачи) is the independent objective description. If `goal` is
    omitted it defaults to `title` for call sites that don't yet collect a
    separate objective — this keeps every caller trivially valid without
    silently losing the objective text.

    `workspace_path`/`branch`/`executor`/`prompt` are the engineering
    environment fields a task inherits from its project at creation time
    (see `command_center.project_config.task_defaults_from_project`) — the
    caller (the Create Task UI) resolves inherited-vs-overridden before
    calling this, so this function just persists whatever final values it is
    given. Omitting them (the pre-existing call signature) leaves the
    pre-existing defaults from `models.default_task_execution_fields`/
    `default_task_workflow_fields` untouched — `workspace_path=None` still
    means "unset, resolve via Launch's own fallback chain," exactly as
    before."""
    now = models.iso_now()
    record = {
        "id": uuid.uuid4().hex,
        "project": project,
        "title": title,
        "task_type": task_type,
        "status": status,
        "priority": priority,
        "owner": owner,
        "estimate_hours": estimate_hours,
        "depends_on": depends_on or [],
        "created_at": now,
        "updated_at": now,
    }
    record.update(models.default_task_workflow_fields())
    record["parent_task_id"] = parent_task_id
    record["prior_run_id"] = prior_run_id
    record["workflow_stage"] = workflow_stage
    record.update(models.default_task_execution_fields())
    record["goal"] = goal if goal is not None else title
    record["notes"] = notes
    if workspace_path:
        record["workspace_path"] = workspace_path
    if branch:
        record["branch"] = branch
    if executor:
        record["executor"] = executor
        record["agent"] = executor
    if prompt:
        record["prompt"] = prompt
    if untrusted_import:
        # App-set provenance flag: the task originates from untrusted content
        # (an imported package, or a candidate parsed from an agent report) and
        # must run read-only by default. `agent_runner.is_untrusted_task` gates
        # on exactly this flag (audit D7 / SEC-1 / SEC-D-02).
        record["untrusted_import"] = True
    models.append_timeline_event(record, "task_created", f"Задача создана: {title}")
    return record


def create_task(root: Path, project: str, title: str, task_type: str, status: str, **kwargs) -> dict:
    """Locked equivalent of `tasks.append(new_task_record(...)); save_tasks(...)`
    — every caller that creates a task must go through here rather than
    appending to its own possibly-stale in-memory list and saving that,
    which is exactly the pattern that silently drops a concurrent writer's
    task (see this module's docstring). `**kwargs` forwards verbatim to
    `new_task_record` (`goal`, `priority`, `depends_on`, `workspace_path`,
    ...). Returns the created record."""

    def _mutator(tasks: list[dict]) -> dict:
        record = new_task_record(project, title, task_type, status, **kwargs)
        # A uuid4 collision is astronomically unlikely, but making the "every
        # task id is unique" invariant explicit and fail-closed here — rather
        # than appending a second record with a colliding id — is what keeps a
        # later delete_task/update_task_status unambiguous (audit BLOCKER-3).
        if any(existing.get("id") == record["id"] for existing in tasks):
            raise ValueError(f"refusing to create a task with a colliding id: {record['id']!r}")
        tasks.append(record)
        return record

    return mutate_tasks(root, _mutator)


def upsert_task(root: Path, task: dict, *, timeout: float = TASKS_LOCK_TIMEOUT_SECONDS) -> None:
    """Persist `task` — already fully mutated in place by the caller (e.g.
    `command_center.launch_service`, which threads one task dict through
    several in-place edits across a multi-step launch) — as the current
    state for its id: replaces any existing entry with that id in a
    freshly-loaded list, or appends it if absent. Only this one task's
    record is touched; every other task in the fresh list (including one a
    concurrent writer added since the caller's own load) survives
    untouched — the right merge semantics for "commit this one already-
    computed task's latest state," as opposed to `mutate_tasks`'s general
    "run this mutator against the fresh list."""
    task_id = task.get("id")

    def _mutator(tasks: list[dict]) -> None:
        for index, existing in enumerate(tasks):
            if existing.get("id") == task_id:
                tasks[index] = task
                return
        tasks.append(task)

    mutate_tasks(root, _mutator, timeout=timeout)


def upsert_tasks(root: Path, tasks_to_upsert: list[dict], *, timeout: float = TASKS_LOCK_TIMEOUT_SECONDS) -> None:
    """Bulk form of `upsert_task`: persists every task in `tasks_to_upsert`
    (each already fully mutated in place by the caller — e.g.
    `command_center.execution_queue.launch_ready`, which mutates one `task`
    dict per launched queue entry) as the current state for its id, all
    within a *single* locked read-modify-write cycle — never one lock
    acquisition per task. Every task in the fresh list not present in
    `tasks_to_upsert` (including ones a concurrent writer added since the
    caller's own load) survives untouched. Safe to call with a caller's
    entire task list, not just the subset that actually changed — every
    task is upserted by id, so an unchanged task is simply overwritten with
    an identical copy, never duplicated."""
    by_id = {t["id"]: t for t in tasks_to_upsert if t.get("id")}

    def _mutator(tasks: list[dict]) -> None:
        seen: set[str] = set()
        for index, existing in enumerate(tasks):
            existing_id = existing.get("id")
            if existing_id in by_id:
                tasks[index] = by_id[existing_id]
                seen.add(existing_id)
        for task_id, task in by_id.items():
            if task_id not in seen:
                tasks.append(task)

    mutate_tasks(root, _mutator, timeout=timeout)


def update_task_status(root: Path, task_id: str, new_status: str) -> dict | None:
    """Returns the updated task record, or `None` if `task_id` was not
    found (a no-op — nothing is saved in that case since `mutate_tasks`
    still runs `save_tasks` on the unchanged fresh list, harmless but see
    `persist_if` if that ever needs to change)."""

    if new_status == "Done":
        raise CompletionEvidenceRequired(
            "Done is completion-owned: a task reaches it only after its result "
            "and target branch have been verified"
        )

    def _mutator(tasks: list[dict]) -> dict | None:
        for task in tasks:
            if task.get("id") == task_id:
                task["status"] = new_status
                task["updated_at"] = models.iso_now()
                models.append_timeline_event(
                    task, "status_changed", f"Задача переведена в статус {new_status}."
                )
                return task
        return None

    return mutate_tasks(root, _mutator)


def set_manual_launch_status(root: Path, task_id: str, status: str, note: str) -> dict | None:
    """Pause/Resume/Restart find-and-persist — same shape as
    `update_task_status`/`delete_task` above. The actual per-task mutation
    (advisory-only; see `command_center.launch`'s module docstring) lives in
    `command_center.task_view.set_manual_launch_status`. Returns the updated
    record, or `None` if `task_id` was not found."""

    def _mutator(tasks: list[dict]) -> dict | None:
        for task in tasks:
            if task.get("id") == task_id:
                task_view.set_manual_launch_status(task, status, note)
                return task
        return None

    return mutate_tasks(root, _mutator)


def delete_task(root: Path, task_id: str) -> bool:
    """Remove a *single* task by id, returning whether one was removed.

    Deliberately deletes at most one record — not every record whose id matches
    (audit BLOCKER-3). The historical `tasks[:] = [t ... if id != task_id]`
    filter removed *all* matches, so if a duplicate id ever reached the store (a
    hand-edited file, an out-of-band import) a single "delete this card" click
    would silently erase several tasks at once. `load_tasks` now also drops
    duplicate ids on read, so a duplicate should never survive to here; removing
    only the first match is the second, independent guard."""

    def _mutator(tasks: list[dict]) -> bool:
        for index, task in enumerate(tasks):
            if task.get("id") == task_id:
                del tasks[index]
                return True
        return False

    return mutate_tasks(root, _mutator)


def reconcile_project_aliases(root: Path) -> dict[str, int]:
    """One-shot, idempotent data migration for BLOCKER-4: rewrite any task whose
    `project` is a known non-canonical alias (`models.PROJECT_ALIASES`) to its
    canonical `models.PROJECT_IDS` id, held under `tasks_lock` so it is safe to
    run against a live store while the app is in use. Returns a `{alias: count}`
    summary of what was rewritten (empty on a second run). A record whose
    `project` is unknown *and* not a listed alias is left untouched and keeps
    surfacing via `validate_tasks` — guessing a canonical id for an
    unrecognised label would hide exactly the divergence the audit wants
    visible."""

    def _mutator(tasks: list[dict]) -> dict[str, int]:
        changed: dict[str, int] = {}
        for task in tasks:
            alias = task.get("project")
            canonical = models.PROJECT_ALIASES.get(alias)
            if canonical:
                changed[alias] = changed.get(alias, 0) + 1
                task["project"] = canonical
                task["updated_at"] = models.iso_now()
        return changed

    return mutate_tasks(root, _mutator)


def task_label(task: dict) -> str:
    title = (task.get("title") or "—")[:50]
    return f"[{task.get('project')}] {title} · {task.get('status')}"


# ---------------------------------------------------------------------------
# Port interface and factory
# ---------------------------------------------------------------------------


class JSONTasksRepository:
    """Thin wrapper around the module-level JSON functions, implementing the TasksPort contract."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load_all(self) -> list[dict]:
        return load_tasks(self._root)

    def create(self, task_dict: dict, *, timeout: float = TASKS_LOCK_TIMEOUT_SECONDS) -> dict:
        task_id = task_dict.get("id")
        if not task_id:
            raise ValueError("task_dict must have an 'id' field")

        def _mutator(tasks: list[dict]) -> dict:
            if any(t.get("id") == task_id for t in tasks):
                raise ValueError(f"refusing to create task with colliding id: {task_id!r}")
            tasks.append(task_dict)
            return task_dict

        return mutate_tasks(self._root, _mutator, timeout=timeout)

    def upsert(self, task_dict: dict) -> None:
        upsert_task(self._root, task_dict)

    def upsert_all(self, tasks: list[dict]) -> None:
        upsert_tasks(self._root, tasks)

    def update_status(self, task_id: str, new_status: str) -> dict | None:
        return update_task_status(self._root, task_id, new_status)

    def delete(self, task_id: str) -> bool:
        return delete_task(self._root, task_id)


def get_repository(root: Path) -> JSONTasksRepository | AIOSTasksRepository:
    """Return the active task store backend.

    ``AICC_TASKS_BACKEND=json`` (default) → ``JSONTasksRepository``
    ``AICC_TASKS_BACKEND=aios`` → ``AIOSTasksRepository`` (requires AICC_AIOS_URL + AICC_AIOS_TOKEN)
    """
    backend = _os.environ.get("AICC_TASKS_BACKEND", "json").lower()
    if backend == "aios":
        url = _os.environ.get("AICC_AIOS_URL")
        token = _os.environ.get("AICC_AIOS_TOKEN")
        if not url:
            raise RuntimeError(
                "AICC_TASKS_BACKEND=aios requires AICC_AIOS_URL to be set"
            )
        if not token:
            raise RuntimeError(
                "AICC_TASKS_BACKEND=aios requires AICC_AIOS_TOKEN to be set"
            )
        from command_center.application.aios_tasks import build_aios_tasks_repository

        return build_aios_tasks_repository(
            url=url,
            token=token,
            map_path=storage.resolve_data_dir(root) / "aios_task_map.json",
        )
    return JSONTasksRepository(root)
