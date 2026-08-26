"""`model_entry` / `model_event` PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 6).

Sixth slice, and the first **identity column**: `model_event.id` is
`INTEGER PRIMARY KEY AUTOINCREMENT` in SQLite and
`bigint GENERATED ALWAYS AS IDENTITY` in PostgreSQL. Seven columns in the
accepted map are declared that way, and the other six all live in the 15-table
family this migration has not reached yet — so the class was worked out here,
on two tables, rather than on fifteen.

Everything below was measured against PostgreSQL 17.6, not read off the manual:

* An explicit `id` is **rejected**: `GeneratedAlways: cannot insert a
  non-DEFAULT value into column "id"`. So the mirror writes
  `OVERRIDING SYSTEM VALUE`, because the authority's id is the only id that
  makes a row identifiable on both sides — a mirror that let PostgreSQL mint
  its own would reconcile nothing, since `divergence` matches rows by id.
* `OVERRIDING SYSTEM VALUE` composes with `ON CONFLICT (id) DO UPDATE`, so the
  upsert contract the other mirrors have survives here.
* The identity sequence is **not advanced** by those inserts: after mirroring
  ids 1..N it still reads `last_value = 1, is_called = false`.
* Therefore the first row PostgreSQL generates after a cutover starts at 1 and
  collides — `UniqueViolation: duplicate key value violates unique constraint
  "…_pkey"`, reproduced. `resync_identity()` exists for exactly that, and the
  cutover has to call it. This is the hazard the accepted map flagged as "two
  separate importer steps"; the first (`OVERRIDING SYSTEM VALUE`) is inherent
  to every write here, the second is a one-time operation nothing in a
  dual-write would otherwise trigger.

The other two properties are already-solved shapes, deliberately: `metadata_json`
is `jsonb` (slice 4) and `model_event.model_id` is a foreign key to
`model_entry` (slice 5). They are here because the table has them, not because
the slice needed them, which is what keeps the new class isolated.

`cost`/`quality` are `REAL` -> `double precision` and need no conversion: the
map records both as IEEE-754 binary64 already, bit-for-bit identical.

The statements themselves live in `command_center/db/table_mirror.py` since
slice 7; `identity=True` below is what selects the `OVERRIDING SYSTEM VALUE`
form and unlocks `resync_identity()`.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "MIRROR_UNAVAILABLE",
    "MODEL_ENTRY_COLUMNS",
    "MODEL_EVENT_COLUMNS",
    "PostgresModelEntryMirror",
    "PostgresModelEventMirror",
    "entry_divergence",
    "event_divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
MODEL_ENTRY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "provider",
    "status",
    "cost",
    "quality",
    "latency_ms",
    "provenance",
    "download_progress",
    "version",
    "created_at",
    "updated_at",
)

MODEL_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "model_id",
    "seq",
    "action",
    "actor",
    "target_ref",
    "provenance",
    "metadata_json",
    "created_at",
)

MODEL_ENTRY = MirroredTable(
    table="model_entry",
    columns=MODEL_ENTRY_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
)

#: A model event is write-once: `created_at` and no `updated_at`. `identity`
#: is the whole point of this table — see the module docstring.
MODEL_EVENT = MirroredTable(
    table="model_event",
    columns=MODEL_EVENT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"metadata_json"}),
    ),
    identity=True,
    references={"model_id": "model_entry"},
)


class PostgresModelEntryMirror(PostgresTableMirror):
    """The `model_entry` table — an ordinary keyed row, `text` primary key."""

    spec = MODEL_ENTRY


class PostgresModelEventMirror(PostgresTableMirror):
    """The `model_event` table — the identity column this slice exists for.

    `upsert` carries **the authority's own id** via `OVERRIDING SYSTEM VALUE`,
    and `resync_identity()` is the cutover step that keeps the first native
    write from colliding with a mirrored key.
    """

    spec = MODEL_EVENT


#: Rows where the SQLite authority and a mirror disagree on `model_entry`.
entry_divergence = divergence_against(MODEL_ENTRY)

event_divergence = divergence_against(
    MODEL_EVENT,
    """Rows where the SQLite authority and a mirror disagree on `model_event`.

    **Takes rows in the shape SQLite stores** — `runtime/db/model_registry.py`'s
    `list_model_events_stored`, not `list_model_events`, which pops
    `metadata_json` in favour of a decoded `metadata` and drops the row id.
    Fed the decoded reader this reports every event divergent, and the failure
    looks like a broken mirror rather than a wrong question.
    """,
)
