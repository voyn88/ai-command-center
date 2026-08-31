"""`run_event` and `report` — the two direct children of `run` (SRV-01B slice 12).

Both hang straight off `run` with `ON DELETE CASCADE`, and between them they
carry two shapes already solved: `run_event.id` is an identity column
(slice 6) with `payload_json` as `jsonb` (slice 4), and `report` is keyed by
`run_id` rather than `id` — the second table after `council_decision` whose
primary key is not called `id` (slice 9).

`run_event` is the highest-volume table in the schema: every line of
stream-json, every stderr line and every lifecycle event is one row. That makes
it the first table where `VOYN-W0-AICC-MIRROR-RECONCILE-STREAMING` stops being
theoretical — reconciling it materialises the whole journal in one process. The
mirror is unchanged by that; the reconciliation entry point is what will have
to page, and the task says so.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "MIRROR_UNAVAILABLE",
    "REPORT_COLUMNS",
    "RUN_EVENT_COLUMNS",
    "PostgresReportMirror",
    "PostgresRunEventMirror",
    "report_divergence",
    "run_event_divergence",
]

RUN_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "run_id",
    "seq",
    "event_type",
    "payload_json",
    "created_at",
)

REPORT_COLUMNS: tuple[str, ...] = ("run_id", "path", "created_at")

RUN_EVENT = MirroredTable(
    table="run_event",
    columns=RUN_EVENT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"payload_json"}),
    ),
    identity=True,
    references={"run_id": "run"},
)

#: Keyed by `run_id`: one report per run, and the column is both key and
#: foreign key — like `council_decision`, and the reason `MirroredTable.key`
#: exists.
REPORT = MirroredTable(
    table="report",
    columns=REPORT_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at"})),
    key="run_id",
    references={"run_id": "run"},
)


class PostgresRunEventMirror(PostgresTableMirror):
    """The `run_event` journal — identity column, one row per output line."""

    spec = RUN_EVENT


class PostgresReportMirror(PostgresTableMirror):
    """The `report` table — at most one immutable row per run."""

    spec = REPORT


run_event_divergence = divergence_against(
    RUN_EVENT,
    """Rows where the SQLite authority and a mirror disagree on `run_event`.

    **Takes rows in the shape SQLite stores** — `runtime/db/execution.py`'s
    `list_run_events_stored`, not `list_run_events`, which selects an explicit
    column list without `id`. Reconciliation pairs rows by the table's key, so
    fed the projected rows it sees `None` on every one and reports the whole
    journal divergent. A third variant of slice 4's trap: that one decoded a
    column away, this one projects it away.

    `list_run_events_stored` takes no `run_id` (SRV-07f): a whole-table
    reader, not one scoped to a single owner, because reconciliation has to
    see every run's rows to catch one whose `run_id` was itself dropped or
    corrupted on write — a scoped read never would.
    """,
)

report_divergence = divergence_against(REPORT)
