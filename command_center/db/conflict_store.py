"""The `conflict` table's PostgreSQL mirror (VOYN-W0-AICC-SRV-01B, slice 3).

Third table, and the first written against shared machinery rather than
carrying its own copy of the conversion. That was the point of doing it then:
the timestamp conversion had already been wrong once, and a third hand-written
copy would have been a third place to fix it the next time.

Shape follows slice 2. SQLite is the authority: `runtime/db/conflict.py`
creates, updates and transitions these rows directly and no JSON store stands
behind it, so this is a dual-write, SQLite stays the system of record, reads are
not switched, and the cutover waits on reconciliation plus the rollback and
backup/restore drills.

What is new here is `resolved_at`: a **nullable** `timestamptz`, and the first
one mirrored. A conflict is created with it `NULL` and acquires a value when it
resolves, so both states are real and the reconciliation has to compare `None`
against `None` without calling it a difference. Nullable timestamps are the
common case in what remains: of the 75 `TEXT` -> `timestamptz` columns in the
accepted map, most are optional lifecycle stamps like this one.

An earlier version of this docstring justified the whole-row upsert by saying
`resolved_at` returns to `NULL` when a conflict reopens. **That is false**, and
independent review caught it: `CONFLICT_TRANSITIONS["resolved"]` is empty, so
`resolved` is terminal and the clearing branch in `_conflict_transition` cannot
be reached. The claim was written from reading that branch instead of the
allowlist above it, and the test offered as its evidence upserted two
hand-built dicts — a sequence the authority cannot produce. It is recorded here
rather than quietly deleted because "proved against data the writer cannot
emit" is the same defect class that put a wrong timestamp conversion into
`main` two slices earlier.

The whole-row upsert stands on reasons that survive checking. `update_conflict_
fields` changes at most four columns — the one or two the caller named, plus
`updated_at` and `version` — and mirrors the whole row, because the mirror has
no other source for the columns it did not touch. The backfill runs more than
once by design. And if `resolved -> open` is ever added to the allowlist, a
field-by-field mirror would keep a resolution the authority had withdrawn —
which is a reason to write whole rows now, not a description of what happens
today.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = ["CONFLICT_COLUMNS", "MIRROR_UNAVAILABLE", "PostgresConflictMirror", "divergence"]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
CONFLICT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "source_ref",
    "severity",
    "status",
    "owner",
    "mitigation",
    "project_ref",
    "opened_at",
    "resolved_at",
    "version",
    "created_at",
    "updated_at",
)

#: Four timestamps, one of them nullable; no boolean and no `jsonb` column in
#: this table, so the codec carries nothing else.
CONFLICT = MirroredTable(
    table="conflict",
    columns=CONFLICT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"opened_at", "resolved_at", "created_at", "updated_at"})
    ),
)


class PostgresConflictMirror(PostgresTableMirror):
    """The `conflict` table on the accepted PostgreSQL seam."""

    spec = CONFLICT


divergence = divergence_against(CONFLICT)
