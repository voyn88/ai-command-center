"""The `run` table's PostgreSQL mirror (VOYN-W0-AICC-SRV-01B, slice 11).

The bottleneck of the correspondence map's FK graph: ten tables reference
`run`, so it moves alone. Forty-two columns, and not one new conversion class —
two `jsonb` (slice 4), three `INTEGER 0/1` -> `boolean` (slice 2), seven
`timestamptz` including five nullable (slice 3, plus `finalized_at` from
`0004_run_finalized_at`), two foreign keys (slice 5). The machinery carries all
of it; what this slice is actually about is the write path.

**The record a caller assembles is not the row the database stores**, and this
is the write-side sibling of slice 4's reader trap. `create_run` builds its
`INSERT` column list from `PRAGMA table_info(run)` intersected with its record
— deliberately, so a part-way-migrated database still works — so the record it
returns is missing whatever it never set.

Measured rather than asserted, because the first version of this docstring
overstated it: the record has 38 keys and the stored row 42, and the four that
differ are `failure_reason`, `first_output_at`, `pre_run_head` and
`finalized_at` — the last of which cannot be otherwise, since it is written
only at the end of finalization, long after `create_run` has returned. All four
are **nullable**, so mirroring the record would work today — it would send
`NULL` where the authority also has `NULL`, and reconciliation would be clean.
The dramatic version of this paragraph claimed a `NOT NULL` violation that
would lose every run silently. That is not true of the columns actually
missing, and it is corrected here rather than quietly deleted, because a
plausible hazard stated with confidence is how this migration has gone wrong
five times.

The hook still mirrors the row **read back inside the transaction**, for the
reason that survives measurement: what the record omits depends on which
optional arguments a caller passed and on which columns that database has, so
"the record happens to be complete enough" is a property of today's callers
rather than of the code. The stored row is the row; reading it costs one
`SELECT` inside a transaction that is already open.

A real limit worth stating: on a database migrated only part-way — the
historical-schema tests build these — the stored row genuinely lacks the newer
columns, so the mirror would send `NULL` for a target column declared
`NOT NULL DEFAULT`. Those databases are migration fixtures, never a mirrored
deployment, and the dual-write is best-effort by design.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = ["MIRROR_UNAVAILABLE", "RUN_COLUMNS", "PostgresRunMirror", "run_divergence"]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by the shared contract.
RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "task_id",
    "sequence",
    "is_resume",
    "state",
    "project",
    "task_type",
    "repository_path",
    "prompt",
    "command_json",
    "timeout_seconds",
    "pid",
    "process_start_identity",
    "pre_run_git_status",
    "post_run_git_status",
    "working_tree_changed",
    "exit_code",
    "cancel_requested",
    "cancel_requested_at",
    "started_at",
    "completed_at",
    "version",
    "created_at",
    "updated_at",
    "failure_reason",
    "expected_branch",
    "launch_source",
    "commit_hash",
    "pull_request_url",
    "prompt_version",
    "first_output_at",
    "provider_id",
    "provider_metadata_json",
    "pre_run_head",
    "capability_profile",
    "capability_override",
    "required_capabilities",
    "granted_capabilities",
    "capability_preflight",
    "command_policy",
    # Added by `0004_run_finalized_at` rather than declared in 0001, so the
    # contract that pins this tuple against the SQL now applies the migration
    # set's `ALTER TABLE ... ADD COLUMN`s in version order. Both engines append
    # a new column at the end of the ordinal order, so the position here is the
    # position the databases have.
    "finalized_at",
)

#: Three flags, six timestamps (four of them nullable — a run is created before
#: it starts and has no completion until it ends), two JSON columns. The
#: capability_* columns are plain text on both sides despite the names: the map
#: records them as opaque strings the runtime parses itself.
RUN = MirroredTable(
    table="run",
    columns=RUN_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset(
            {
                "cancel_requested_at",
                "started_at",
                "completed_at",
                "first_output_at",
                "created_at",
                "updated_at",
                "finalized_at",
            }
        ),
        flags=frozenset({"is_resume", "working_tree_changed", "cancel_requested"}),
        json_values=frozenset({"command_json", "provider_metadata_json"}),
    ),
    references={"session_id": "session", "task_id": "task"},
)


class PostgresRunMirror(PostgresTableMirror):
    """The `run` table on the accepted PostgreSQL seam."""

    spec = RUN


run_divergence = divergence_against(
    RUN,
    """Rows where the SQLite authority and a mirror disagree on `run`.

    Takes rows as SQLite stores them — `list_runs` and `get_run` already return
    that shape, so unlike `digest_item` or `audit_run` this table needs no
    reader of its own. What it does need is that the *writer* hand over the
    stored row rather than the record it assembled; see the module docstring.
    """,
)
