"""The Council family's PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 9).

Four tables of one family: `motion` and its three children — `council_vote`,
`council_decision`, `council_event`. Kept out of slice 8's batch because the
combination is denser than anything mirrored so far, and density is what makes a
review expensive even when no single property is new.

What each one contributes, and where it was first proved:

* `motion` — the parent; nullable `decided_at` (slice 3);
* `council_vote` — a child, plus `UNIQUE (motion_id, voter_id)`: a second
  uniqueness constraint beside the key. The mirror does not need to know about
  it — the authority mints one vote per voter and the upsert is keyed on `id` —
  but it is the reason a *backfill* of this table must not invent rows;
* `council_decision` — **two** `jsonb` columns in one row, and they are written
  differently: `tally_json` with `sort_keys=True`, `roles_json` without. A text
  comparison would survive on one column and fail on its neighbour, which is
  the clearest argument yet for comparing `jsonb` by value (slice 4);
* `council_event` — identity column (slice 6), `jsonb`, and
  `UNIQUE (motion_id, seq)`.

**The one genuinely new property is `council_decision`'s key.** Its primary key
is `motion_id`, and it carries an `id` column that is *not* unique — so
`ON CONFLICT (id)` names a constraint the table does not have. Every mirror
before this one was keyed on `id`, and the machinery assumed it. Found by
reading the schema before writing the mirror, which matters because the failure
mode is silent: the insert would raise, the dual-write hook would swallow it,
and the table would simply never mirror. `MirroredTable.key` now carries this,
`divergence` pairs rows by it, and the shared contract checks the declaration
against the DDL's actual primary key so the next such table announces itself.

Both decoding readers in this family get the same treatment `digest_item`,
`model_event` and `audit_run` did: `list_decisions_stored` and
`list_events_stored` exist because `_decode_decision_row` and
`_decode_council_event_row` pop the JSON columns, and `tests/db/
test_stored_reader_fitness.py` now fails when a table with a decoding reader has
no stored one.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "COUNCIL_DECISION_COLUMNS",
    "COUNCIL_EVENT_COLUMNS",
    "COUNCIL_VOTE_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "MOTION_COLUMNS",
    "PostgresCouncilDecisionMirror",
    "PostgresCouncilEventMirror",
    "PostgresCouncilVoteMirror",
    "PostgresMotionMirror",
    "decision_divergence",
    "event_divergence",
    "motion_divergence",
    "vote_divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by the shared contract rather than by eye.
MOTION_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "body",
    "proposed_by",
    "quorum",
    "project_ref",
    "proposal_ref",
    "source_ref",
    "status",
    "opened_at",
    "decided_at",
    "version",
    "created_at",
    "updated_at",
)

COUNCIL_VOTE_COLUMNS: tuple[str, ...] = (
    "id",
    "motion_id",
    "voter_id",
    "voter_kind",
    "role",
    "choice",
    "rationale",
    "created_at",
)

COUNCIL_DECISION_COLUMNS: tuple[str, ...] = (
    "motion_id",
    "id",
    "outcome",
    "tally_json",
    "roles_json",
    "rationale",
    "quorum",
    "decided_at",
    "created_at",
)

COUNCIL_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "motion_id",
    "seq",
    "event_type",
    "actor",
    "role",
    "message",
    "metadata_json",
    "created_at",
)

MOTION = MirroredTable(
    table="motion",
    columns=MOTION_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"opened_at", "decided_at", "created_at", "updated_at"})
    ),
)

#: Write-once: one `created_at` and no `updated_at`. `UNIQUE (motion_id,
#: voter_id)` is the authority's business rule; the mirror upserts by key and
#: never mints a vote of its own, so it cannot violate it.
COUNCIL_VOTE = MirroredTable(
    table="council_vote",
    columns=COUNCIL_VOTE_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at"})),
    references={"motion_id": "motion"},
)

#: Keyed by `motion_id`, not `id` — see the module docstring. Two `jsonb`
#: columns whose writers disagree about canonicalisation, which is why both are
#: compared as parsed values.
COUNCIL_DECISION = MirroredTable(
    table="council_decision",
    columns=COUNCIL_DECISION_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"decided_at", "created_at"}),
        json_values=frozenset({"tally_json", "roles_json"}),
    ),
    key="motion_id",
    references={"motion_id": "motion"},
)

COUNCIL_EVENT = MirroredTable(
    table="council_event",
    columns=COUNCIL_EVENT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"metadata_json"}),
    ),
    identity=True,
    references={"motion_id": "motion"},
)


class PostgresMotionMirror(PostgresTableMirror):
    """The `motion` table — the family's foreign-key parent."""

    spec = MOTION


class PostgresCouncilVoteMirror(PostgresTableMirror):
    """The `council_vote` table."""

    spec = COUNCIL_VOTE


class PostgresCouncilDecisionMirror(PostgresTableMirror):
    """The `council_decision` table — keyed by `motion_id`."""

    spec = COUNCIL_DECISION


class PostgresCouncilEventMirror(PostgresTableMirror):
    """The `council_event` table — identity column, like `model_event`."""

    spec = COUNCIL_EVENT


motion_divergence = divergence_against(MOTION)

vote_divergence = divergence_against(COUNCIL_VOTE)

decision_divergence = divergence_against(
    COUNCIL_DECISION,
    """Rows where the SQLite authority and a mirror disagree on `council_decision`.

    **Takes rows in the shape SQLite stores** — `runtime/db/council.py`'s
    `list_decisions_stored`, not `get_decision` / `list_decisions` /
    `record_decision`, which return `_decode_decision_row` output with
    `tally_json` and `roles_json` replaced by decoded `tally`/`roles`. Fed those,
    this reports every decision divergent on both JSON columns.

    Rows are paired by `motion_id`, this table's actual primary key; its `id`
    column is not unique.
    """,
)

event_divergence = divergence_against(
    COUNCIL_EVENT,
    """Rows where the SQLite authority and a mirror disagree on `council_event`.

    **Takes rows in the shape SQLite stores** — `runtime/db/council.py`'s
    `list_events_stored`, not `list_events`, which returns
    `_decode_council_event_row` output with `metadata_json` decoded away.
    """,
)
