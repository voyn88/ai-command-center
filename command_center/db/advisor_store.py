"""The `advisor_proposal` table's PostgreSQL mirror (SRV-01B, slice 8).

The simplest table left in the schema and the last one that stands alone: no
foreign key, no `jsonb`, no identity column, no delete path, and its readers
return rows as stored. Two `timestamptz` columns and nothing else to convert.

It is in the batch rather than in a slice of its own precisely because it adds
nothing — after slice 7 a table like this is a declaration and three dual-write
hooks, and giving it its own review round would cost more than it proves.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "ADVISOR_PROPOSAL_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "PostgresAdvisorProposalMirror",
    "divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
ADVISOR_PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "title",
    "body",
    "expected_gain",
    "effort",
    "project_ref",
    "status",
    "promoted_task_id",
    "version",
    "created_at",
    "updated_at",
)

ADVISOR_PROPOSAL = MirroredTable(
    table="advisor_proposal",
    columns=ADVISOR_PROPOSAL_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
)


class PostgresAdvisorProposalMirror(PostgresTableMirror):
    """The `advisor_proposal` table on the accepted PostgreSQL seam."""

    spec = ADVISOR_PROPOSAL


divergence = divergence_against(ADVISOR_PROPOSAL)
