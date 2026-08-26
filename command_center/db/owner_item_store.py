"""The `owner_item` table's PostgreSQL mirror (VOYN-W0-AICC-SRV-01B, slice 2).

Slice 2 differs from slice 1 in the way that matters most. `queue_entry`'s
authority was already `execution_queue.json`, with SQLite as a mirror, so
adding PostgreSQL added a third mirror and moved nothing. Here **SQLite is the
authority**: `command_center/runtime/db/wave1.py` creates, reads and updates
these rows directly, and no JSON store stands behind it. So this slice is a
dual-write, SQLite stays the system of record, reads are not switched, and the
cutover waits on reconciliation plus the rollback and backup/restore drills.

Two type gaps make the conversion load-bearing rather than incidental, and the
accepted correspondence map predicted exactly this by warning that the target
is stricter:

* `done` is `INTEGER` (0/1) in SQLite and `boolean` in PostgreSQL;
* `created_at`/`updated_at` are `TEXT` in SQLite and `timestamptz` in
  PostgreSQL.

Both are converted on the way in and rendered back on the way out, so a
reconciliation compares like with like. Skipping that would not produce a
visible error: it would produce a divergence check that reports every row as
different, which is a cutover gate permanently red — and a red gate nobody can
satisfy is one someone eventually satisfies by loosening the comparison.

The conversions moved to `mirror_support` at slice 3 and the statements to
`table_mirror` at slice 7; what remains here is the declaration of what makes
this table different from the others. The history is worth keeping in view: the
version of the timestamp conversion this module first shipped was wrong in both
directions.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = ["MIRROR_UNAVAILABLE", "OWNER_ITEM_COLUMNS", "PostgresOwnerItemMirror", "divergence"]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`.
OWNER_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "detail",
    "due",
    "done",
    "source_ref",
    "version",
    "created_at",
    "updated_at",
    "project_ref",
)

#: `due` is deliberately absent from `timestamps`: the map keeps it `text` on
#: both sides because it is free user input rather than a date.
OWNER_ITEM = MirroredTable(
    table="owner_item",
    columns=OWNER_ITEM_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at", "updated_at"}),
        flags=frozenset({"done"}),
    ),
)


class PostgresOwnerItemMirror(PostgresTableMirror):
    """The `owner_item` table on the accepted PostgreSQL seam."""

    spec = OWNER_ITEM


#: Rows where the SQLite authority and a mirror disagree — see
#: `mirror_support.divergence` for what each reported shape means.
divergence = divergence_against(OWNER_ITEM)
