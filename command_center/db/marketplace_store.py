"""`market_item` / `market_install_log` PostgreSQL mirrors (SRV-01B, slice 8).

Part of the first **batch** slice. Slices 1–6 each worked out one conversion
class and cost a slice apiece; slice 7 made the machinery declarative, so a
table with no new class is now a declaration plus a dual-write hook. This
family has no new class: `metadata_json` is `jsonb` (slice 4) and
`market_install_log.item_id` is a foreign key to `market_item` (slice 5).

The one property worth naming is a difference from every earlier `jsonb`
column: `install_market_item` writes it with `json.dumps(..., sort_keys=True)`.
That makes the authority's text canonical, which would make a *text* comparison
survive here where it fails everywhere else — and relying on that would be a
trap, because the canonicalisation belongs to one writer rather than to the
column. The declaration therefore treats it exactly like the others and
compares parsed values, so the reconciliation does not silently depend on a
caller keeping `sort_keys=True`.

`lock_version` is an ordinary integer on both sides despite the name, and
`version` here is `text` (a package version string), not the optimistic-lock
counter the other tables call `version` — a reminder that the column tuples are
pinned against `0001_initial.up.sql` by a test rather than read by eye.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "INSTALL_LOG_COLUMNS",
    "MARKET_ITEM_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "PostgresInstallLogMirror",
    "PostgresMarketItemMirror",
    "install_log_divergence",
    "market_item_divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
MARKET_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "version",
    "publisher",
    "description",
    "status",
    "provenance",
    "lock_version",
    "created_at",
    "updated_at",
)

INSTALL_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "item_id",
    "actor",
    "version",
    "kind",
    "provenance",
    "installer",
    "detail",
    "metadata_json",
    "installed_at",
    "created_at",
)

MARKET_ITEM = MirroredTable(
    table="market_item",
    columns=MARKET_ITEM_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
)

#: An install log entry is write-once: two timestamps, no `updated_at`.
INSTALL_LOG = MirroredTable(
    table="market_install_log",
    columns=INSTALL_LOG_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"installed_at", "created_at"}),
        json_values=frozenset({"metadata_json"}),
    ),
    references={"item_id": "market_item"},
)


class PostgresMarketItemMirror(PostgresTableMirror):
    """The `market_item` table — the family's foreign-key parent."""

    spec = MARKET_ITEM


class PostgresInstallLogMirror(PostgresTableMirror):
    """The `market_install_log` table — its child.

    An upsert here is refused when the referenced `market_item` is absent from
    the mirror, and the refusal is swallowed by the dual-write hook. The
    consequence is the one slice 5 measured: a lost parent costs every child
    after it, silently, until reconciliation runs.
    """

    spec = INSTALL_LOG


market_item_divergence = divergence_against(MARKET_ITEM)

install_log_divergence = divergence_against(
    INSTALL_LOG,
    """Rows where the SQLite authority and a mirror disagree on `market_install_log`.

    `metadata_json` is compared as a **parsed value**, not as text, even though
    this table's writer happens to emit canonical JSON (`sort_keys=True`). The
    canonicalisation belongs to that one caller; the column does not promise
    it, and a reconciliation that depended on it would break the day another
    writer appears — see `mirror_support.ColumnCodec` for why `jsonb` cannot be
    compared as text in general.
    """,
)
