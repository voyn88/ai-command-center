"""The `digest_item` table's PostgreSQL mirror (VOYN-W0-AICC-SRV-01B, slice 4).

Fourth table, chosen for the two things it adds rather than for its size, and
both were declared blockers before that slice started rather than discovered
inside it.

**`jsonb`.** `refs_json` is the first mirrored column PostgreSQL stores as
`jsonb` while the authority stores JSON text, and 22 columns in the accepted map
follow it. The hazard is not the conversion but the *comparison*: a text ->
`jsonb` -> text round trip does not preserve the source bytes. Measured on
PostgreSQL 17.6 rather than assumed — `{"b": 1, "a": 2}` comes back
`{"a": 2, "b": 1}` — so key order and separators belong to the database, and
comparing the column as text would report every object-valued row as different.
That is the permanently-red cutover gate this migration keeps almost building,
and the one somebody eventually satisfies by loosening the comparison. These
columns are compared as parsed values instead (`ColumnCodec.comparable`), and
the map's other requirement is met on the way in: unparseable text raises
rather than reaching the column.

**Deletes.** `delete_digest_items_for_day` is the first authority operation in
this migration that *removes* rows: the digest engine rebuilds a day by deleting
it and re-inserting. A mirror that only upserts would keep every superseded row
forever, and reconciliation would report a mirror permanently ahead of the
system of record — true, useless, and exactly the noise that gets a check
switched off.

Shape otherwise follows slices 2 and 3. SQLite is the authority, this is a
dual-write, reads are not switched, and the cutover waits on reconciliation
plus the rollback and backup/restore drills.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "DIGEST_ITEM_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "PostgresDigestItemMirror",
    "divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
DIGEST_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "body",
    "category",
    "refs_json",
    "created_at",
    "day",
    "position",
    "project_ref",
)

#: `day` is deliberately not a timestamp: the map keeps it `text` on both sides
#: because it is a build label the caller supplies, not a date the application
#: computed. `position` is an ordinary integer here — unlike the queue's, it is
#: the authority's own column rather than something the mirror added to
#: preserve order, so it is compared like any other value.
DIGEST_ITEM = MirroredTable(
    table="digest_item",
    columns=DIGEST_ITEM_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"refs_json"}),
    ),
)


class PostgresDigestItemMirror(PostgresTableMirror):
    """The `digest_item` table on the accepted PostgreSQL seam."""

    spec = DIGEST_ITEM

    def delete_day(self, day: str) -> None:
        """Remove every mirrored row built for `day`.

        The authority's own predicate rather than the ids it removed: deriving
        ids would mean reading the authority before the delete, which is an
        extra query and a race with the rebuild this is following.
        """
        self.delete_where("day", day)


divergence = divergence_against(
    DIGEST_ITEM,
    """Rows where the SQLite authority and a mirror disagree.

    **Takes rows in the shape SQLite stores** — `runtime/db/wave1.py`'s
    `list_digest_items_stored`, not its other readers. Worth saying here
    because for this table alone that is not the shape the repository hands
    out: every public reader returns `_decode_digest_row` output, which pops
    `refs_json` and substitutes a decoded `refs`. Fed one of those, this
    reports every row divergent on the one column the slice exists to migrate,
    and the failure looks like a broken mirror rather than a wrong question.

    Passes the codec, which is what makes `refs_json` compare as a parsed value
    rather than as text — see `mirror_support.divergence` for the rest.
    """,
)
