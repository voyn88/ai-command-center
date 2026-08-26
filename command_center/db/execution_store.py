"""`task` and `session` PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 10).

The first two tables of the 15-table family this migration has been circling
since the correspondence map: `task` is its root and `session` its first child.
Everything else in the family hangs off one of them, directly or through `run`,
which is why they go first and alone — the map's FK graph makes `run` a
bottleneck touched by ten tables, and a slice that moved it would move half the
schema at once.

No new conversion class: two `timestamptz` columns each, one foreign key
(slice 5), nothing else to convert. What *is* new is the delete.

**The first mirrored cascade.** `delete_task` removes one row and relies on
`ON DELETE CASCADE` to take every session, run, run_event, report and
completion with it — the authority enables `PRAGMA foreign_keys=ON` per
connection precisely so that works. The target declares the same cascades, so
the mirror deletes the same single row and lets PostgreSQL do the same thing.

That symmetry is the claim, and it is the kind that looks obvious and is not:
it holds only while *both* sides declare the cascade, and the mirror can neither
see nor enforce the other side's. A test drives the real `delete_task` and
checks the mirrored child is gone, so the day a cascade is dropped on either
side, something says so rather than leaving orphans that reconciliation would
report as "mirror ahead" forever.

Deliberately **not** here: `run` and the ten tables that reference it. They are
the next slices, in FK order, for blast radius rather than for machinery —
`task` alone is touched by 120 modules, and this slice touches none of them
because the mirror hangs off the repository tier.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "MIRROR_UNAVAILABLE",
    "SESSION_COLUMNS",
    "TASK_COLUMNS",
    "PostgresSessionMirror",
    "PostgresTaskMirror",
    "session_divergence",
    "task_divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by the shared contract.
TASK_COLUMNS: tuple[str, ...] = (
    "id",
    "project",
    "title",
    "task_type",
    "legacy_task_id",
    "created_at",
    "updated_at",
)

SESSION_COLUMNS: tuple[str, ...] = (
    "id",
    "task_id",
    "project",
    "repository_path",
    "legacy_run_id",
    "created_at",
    "updated_at",
)

TASK = MirroredTable(
    table="task",
    columns=TASK_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
)

SESSION = MirroredTable(
    table="session",
    columns=SESSION_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
    references={"task_id": "task"},
)


class PostgresTaskMirror(PostgresTableMirror):
    """The `task` table — the family's root."""

    spec = TASK

    def delete_task(self, task_id: str) -> None:
        """Remove one task and let the target cascade, exactly as the authority does.

        `delete_task` in `runtime/db/execution.py` deletes a single row and
        relies on `ON DELETE CASCADE` with `PRAGMA foreign_keys=ON`; the target
        declares the same cascades. Mirroring the same single delete keeps the
        two sides symmetric — and deleting the children explicitly would not:
        it would race the cascade and mean the mirror had its own opinion about
        what a task's removal implies.
        """
        self.delete_where("id", task_id)


class PostgresSessionMirror(PostgresTableMirror):
    """The `session` table — the first child, and cascade-deleted with its task."""

    spec = SESSION


task_divergence = divergence_against(TASK)

session_divergence = divergence_against(SESSION)
