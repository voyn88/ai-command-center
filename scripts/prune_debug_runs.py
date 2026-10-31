#!/usr/bin/env python3
"""Delete run records that are artifacts of debugging, not real execution history.

Two classes qualify, and only these two — both identified by a terminal state
with **no `failure_reason` at all**, which is what distinguishes a run that was
lost from one that genuinely failed:

- `INTERRUPTED` with no reason — the supervising process exited while the child
  was still alive (an app restart, a `pkill`), so reconciliation could not say
  what became of it. Nothing was learned from these runs.
- `FAILED` with no reason — the provider CLI exited non-zero before doing any
  work. In this repository's history these are the runs that hit an expired
  OAuth session: zero tokens spent, no attempt made.

Everything else is kept, including every classified failure
(`blocked:permission_denied`, `timeout`, `blocked:final_response`): those record
something that actually happened and that a human may need to review.

Deleting run rows changes the retry budget a task has consumed, because the
scheduler counts terminal runs. That is the *point* here — attempts spent on an
expired session or a killed supervisor should not count against a task — but it
is a real consequence, so this script is deliberately not run automatically by
anything.

Usage:
    python scripts/prune_debug_runs.py --dry-run   # list what would go
    python scripts/prune_debug_runs.py --apply     # delete, after a backup

`--apply` refuses to run unless `--backup` names a file that does not yet
exist; the database is copied there first. There is no undo otherwise.

Deletion runs in fixed-size batches (`PRUNE_BATCH_SIZE`), not one sweep, so a
long-neglected database cannot put its entire backlog into a single statement
or a single write-lock hold (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE`). The
trade is that a failure part-way through is *not* a no-op: whatever batches
committed stay committed, the script says how many, and the backup is the way
back.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center.runtime import db  # noqa: E402

# Child rows that reference a run. Ordered children-first so no statement can
# leave a row pointing at a deleted parent, whatever the schema's cascade rules.
_CHILD_TABLES: tuple[str, ...] = (
    "run_event",
    "report",
    "completion_event",
    "validation_result",
    "completion",
)

#: How many runs one statement may name. Every DELETE here binds one SQL
#: variable per run, so an unbatched sweep of a long-neglected database both
#: holds the write lock for the whole backlog and can blow past SQLite's
#: SQLITE_LIMIT_VARIABLE_NUMBER (999 on builds before 3.32) outright
#: (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE`).
PRUNE_BATCH_SIZE = 500


def _existing_child_tables(conn) -> tuple[str, ...]:
    """The subset of `_CHILD_TABLES` this schema actually has.

    Resolved once, up front, rather than by catching an error per table per
    batch: that catch could not tell "this old schema has no `completion`
    table" apart from "this DELETE failed for a real reason", and swallowing
    the latter would let the run rows go while their child rows stayed.
    """
    present = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = [t for t in _CHILD_TABLES if t not in present]
    for table in missing:
        print(f"  пропущено {table}: нет в этой схеме")
    return tuple(t for t in _CHILD_TABLES if t in present)


def debug_artifacts(db_path: Path) -> list[dict]:
    """Runs that are debugging residue. See the module docstring for why the
    test is "terminal state *and* no failure_reason": a classified failure is
    history, an unclassified one is a run that never reported anything."""
    return [
        run
        for run in db.list_runs(db_path)
        if run["state"] in ("INTERRUPTED", "FAILED") and not (run.get("failure_reason") or "")
    ]


def prune(db_path: Path, run_ids: list[str], *, batch_size: int = PRUNE_BATCH_SIZE) -> None:
    """Delete `run_ids` and their child rows, `batch_size` runs per transaction.

    Batched rather than one sweep so no single statement names the whole
    backlog and no single transaction holds the write lock for it
    (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE`). Within a batch the order is
    still children-first, so no statement leaves a row pointing at a deleted
    parent whatever the schema's cascade rules.

    The cost of batching is that this is no longer all-or-nothing: if a batch
    raises, that batch rolls back but every batch already committed stays
    committed. The `--backup` this script demands before `--apply` is the way
    back, and `main` reports what actually got through.
    """
    if not run_ids:
        return
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    with db.connect(db_path) as conn:
        child_tables = _existing_child_tables(conn)
        for start in range(0, len(run_ids), batch_size):
            batch = run_ids[start : start + batch_size]
            placeholders = ", ".join("?" for _ in batch)
            with db.transaction(conn):
                for table in child_tables:
                    conn.execute(
                        f"DELETE FROM {table} WHERE run_id IN ({placeholders})", batch
                    )
                conn.execute(f"DELETE FROM run WHERE id IN ({placeholders})", batch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="только показать, ничего не менять")
    mode.add_argument("--apply", action="store_true", help="удалить (требует --backup)")
    parser.add_argument("--backup", type=Path, help="куда скопировать БД перед удалением")
    args = parser.parse_args(argv)

    db_path = db.resolve_db_path()
    doomed = debug_artifacts(db_path)
    total = len(db.list_runs(db_path))

    print(f"База: {db_path}")
    print(f"Всего прогонов: {total}")
    print(f"К удалению: {len(doomed)}  ·  Остаётся: {total - len(doomed)}")
    print()
    for run in doomed:
        why = "осиротел при перезапуске" if run["state"] == "INTERRUPTED" else "провайдер не начал работу"
        print(f"  {run['state']:<12} {(run.get('task_id') or '—')[:24]:<26} {run['id'][:8]}  {why}")

    if args.dry_run:
        print("\n(ничего не изменено)")
        return 0

    if args.backup is None:
        print("\nОШИБКА: --apply требует --backup — отката нет.", file=sys.stderr)
        return 2
    if args.backup.exists():
        print(f"\nОШИБКА: файл бэкапа уже существует: {args.backup}", file=sys.stderr)
        return 2

    args.backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, args.backup)
    print(f"\nБэкап: {args.backup}")

    try:
        prune(db_path, [run["id"] for run in doomed])
    except Exception as exc:  # noqa: BLE001 — report what committed, then fail
        # Deletion is batched, so a failure here is not a no-op: say how far it
        # actually got instead of letting the operator assume nothing changed.
        left = len(debug_artifacts(db_path))
        print(f"\nОШИБКА при удалении: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            f"Удаление идёт батчами по {PRUNE_BATCH_SIZE}, поэтому уже зафиксировано: "
            f"удалено {len(doomed) - left}, осталось {left}. "
            f"Полный откат: восстановите БД из {args.backup}.",
            file=sys.stderr,
        )
        return 1
    print(f"Удалено: {len(doomed)}  ·  Осталось: {len(db.list_runs(db_path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
