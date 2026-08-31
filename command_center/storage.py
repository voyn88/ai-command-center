"""Generic local JSON/JSONL persistence primitives.

Two storage shapes are used in this project, deliberately:

- **Whole-file JSON** (`data/tasks.json`, `data/chats.json`, `data/project_config.json`):
  read-modify-write documents where the current state is the entire file. Writes are
  atomic: a temp file is written in the same directory, fsynced, then swapped in with
  `os.replace` (a single filesystem rename), so a crash mid-write cannot corrupt the
  destination file.
- **Append-only JSON Lines** (`data/runs.jsonl`, `data/activity.jsonl`): every write is a
  single `open(..., "a")` + one line + `flush` + `fsync`, never a rewrite of prior
  content. This is used for run records and activity events because both are
  write-heavy logs (a run is written at queued/running/completed and can carry a large
  stdout blob) where rewriting the whole file on every update would be slower and would
  briefly risk the *entire* history during the rewrite window. JSONL reduces that risk to
  only the single new line being appended. The "current" state of a run is the last line
  seen for its id (last-write-wins fold), which the caller performs.

`file_lock` below is a third, orthogonal primitive: `atomic_write_json`'s temp-file +
`os.replace` makes any *single* write atomic, but does nothing to prevent a lost update
across a read-modify-write *cycle` — two concurrent callers can both read the same
pre-write state, each compute a result from it, and the second `atomic_write_json` call
silently discards the first caller's update. Any read-modify-write cycle on a whole-file
JSON document (the registry in `command_center.portfolio_launch`, the task-package import
transaction in `command_center.task_import`) must hold `file_lock` for its *entire*
read-through-write span, not just around the final write.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def resolve_data_dir(root: Path) -> Path:
    """`root / "data"`, unless the `AICC_DATA_DIR` environment variable overrides it.

    Every `command_center` module and `app.py` itself call this the same way, so a
    single environment variable redirects *all* runtime storage at once. This exists
    primarily so the test suite (Streamlit `AppTest`-driven page renders, in
    particular) can point every module at an isolated temp directory and never read
    or write the developer's real `data/*.json(l)` files — see `tests/conftest.py`.
    """
    override = os.environ.get("AICC_DATA_DIR")
    return Path(override) if override else root / "data"


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def create_json_if_absent(path: Path, data: Any) -> bool:
    """Create `path` holding `data` **only if it does not already exist**, and
    report whether this call was the one that created it.

    This is the initialisation counterpart to `atomic_write_json`, and the
    difference is the whole point: `atomic_write_json` ends in `os.replace`,
    which overwrites unconditionally. Using it to seed a store that another
    writer may be creating at the same instant is a lost update — the classic
    check-then-write race, where two callers each observe "the file is
    missing" and the slower one's empty seed lands *after* the faster one has
    already committed a real record under the store's lock, silently erasing
    it (measured live: see `VOYN-W0-AICC-TASK-IMPORT-CONCURRENCY-FLAKE`, where
    the first task of a concurrently imported package vanished from
    `tasks.json` while every writer reported success).

    A lock cannot close this on its own, because the read paths that seed the
    store (`tasks_repository.load_tasks` via `JSONTasksRepository.load_all`)
    are deliberately *not* holding the store's write lock — they are reads.
    So the creation itself is made exclusive instead: the payload is written
    to a temp file and published with `os.link`, which fails rather than
    replaces when the target already exists. A loser of the race gets
    `False` and leaves the winner's content — whatever it now contains —
    completely untouched.

    `os.link` is used rather than `open(..., "x")` so the file is never
    observable in a half-written state: it becomes visible at its final path
    only once it is complete and fsynced. On the filesystems that do not
    support hard links, the exclusive `open` is the fallback, which keeps the
    no-overwrite guarantee (the only thing correctness depends on here) and
    gives up only the invisibility of the write window.
    """
    return create_bytes_if_absent(
        path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    )


def create_bytes_if_absent(path: Path, payload: bytes) -> bool:
    """`create_json_if_absent` for content that is already serialized.

    Exists so a caller can seed a store from a file it must copy **verbatim**,
    without parsing it first. Parsing to re-serialize looks equivalent and is
    not: it moves the decode error to a different place in the call stack,
    where the caller's own error handling may not cover it (independent review
    of `4b058ff`, on exactly that regression in `tasks_repository.load_tasks`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return False
        except OSError:
            # Filesystem without hard links: fall back to an exclusive create.
            try:
                with open(path, "xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                return False
        return True
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    """Durably write text (e.g. a markdown report) via temp + flush + fsync + os.replace.

    Same crash-atomicity discipline as `atomic_write_json`, for non-JSON artifacts
    (report markdown). The temp file lives next to the target so the rename is
    atomic on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def ensure_seeded(path: Path, empty_default: Any) -> None:
    """Ensure `path` exists, initialized to `empty_default` if missing.

    Deliberately does **not** read the tracked `.example` sibling file: those files
    hold illustrative sample content for documentation purposes (see e.g.
    `data/runs.example.jsonl`) and must never be copied into a live runtime file —
    doing so would present fabricated data as if it were real, which the app must
    never do. Compare `app.py`'s `load_tasks()`, which seeds from
    `tasks.example.json` deliberately, because that example is `[]` (truly empty).

    Seeding goes through `create_json_if_absent`, never `atomic_write_json`: a
    concurrent writer that has already created and populated `path` must not
    have its content replaced by this empty default (see that function).
    """
    create_json_if_absent(path, empty_default)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def ensure_seeded_jsonl(path: Path) -> None:
    """Ensure `path` exists as an empty log. See `ensure_seeded` for why the
    tracked `.example.jsonl` sibling is never copied into the live file, and
    why the creation is exclusive: `write_text` truncates, so a writer that
    appended a record between the existence check and this call would have it
    erased."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8"):
            pass
    except FileExistsError:
        pass


def fold_latest_by_id(records: Iterable[dict], id_key: str = "id") -> dict[str, dict]:
    """Fold an append-only sequence of snapshots into the latest record per id.

    Later records in `records` win over earlier ones with the same id, so callers
    should pass records in the order they were appended (oldest first).
    """
    latest: dict[str, dict] = {}
    for record in records:
        record_id = record.get(id_key)
        if record_id is None:
            continue
        latest[record_id] = record
    return latest


# --------------------------------------------------------------------------
# Cross-process file locking — the whole-read-modify-write-cycle primitive
# --------------------------------------------------------------------------
#
# Extracted (behavior-preserving) from `command_center.portfolio_launch`'s
# original `_registry_lock`/`_try_acquire_lock`/`_release_lock`, which was the
# first place this project needed real cross-process mutual exclusion around
# a JSON read-modify-write cycle (`data/portfolio_launches.json`). Generalized
# here so any future whole-file-JSON read-modify-write cycle — starting with
# `command_center.task_import.apply_task_package`'s import transaction — reuses
# the same primitive instead of re-implementing the fcntl/msvcrt dance.


class LockTimeoutError(Exception):
    """Raised by `file_lock` when the lock cannot be acquired within its timeout."""


def _try_acquire_lock(handle) -> bool:
    """Non-blocking attempt to take the OS advisory lock on `handle`. Returns
    `False` (never raises) if another holder — thread or process, doesn't
    matter, both primitives below are OS-level, not Python-level — already
    holds it."""
    try:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_lock(handle) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(lock_path: Path, *, timeout: float = 30.0, poll_seconds: float = 0.05):
    """Cross-process, cross-thread mutual exclusion for the *entire*
    critical section the caller runs inside this context — not just around
    a single write. Correct usage for a read-modify-write cycle on some other
    file (`registry.json`, `tasks.json`, ...) is to acquire this lock *before*
    the read and hold it through the write; acquiring it only around the
    write still lets two callers both read the same pre-write state and each
    write back a version that discards the other's change (the lost-update
    race this primitive exists to close).

    Backed by a dedicated lock file at `lock_path` (never the file being
    protected itself, so a lock holder never blocks a plain unlocked read of
    that file) holding an OS advisory lock — `fcntl.flock` on POSIX,
    `msvcrt.locking` on Windows, both stdlib, no third-party dependency.
    Both are released by the kernel the instant the holding process's file
    descriptor closes, including on a crash or kill, so a dead holder can
    never wedge every future caller (unlike an exclusive-create marker file,
    there is no "stale lock" state to manually clean up).

    Acquisition polls with a bounded `timeout` (rather than blocking forever)
    so a bug that fails to release the lock surfaces as `LockTimeoutError`
    instead of an indefinite hang; release always happens in `finally`, so a
    normal exception raised by the code running inside this context still
    releases the lock immediately.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            # Windows byte-range locking needs at least one real byte to
            # lock; POSIX flock ignores file content/length entirely.
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not _try_acquire_lock(handle):
            if time.monotonic() >= deadline:
                raise LockTimeoutError(f"could not acquire lock within {timeout:.0f}s: {lock_path}")
            time.sleep(poll_seconds)
        try:
            yield
        finally:
            _release_lock(handle)
    finally:
        handle.close()
