"""The completion family's PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 14).

`completion` (one row per run, keyed by `run_id`), `completion_event` (the
append-only log) and `completion_validation` (each validation attempt).

No new conversion class: two identity columns (slice 6), three `jsonb` columns
between them (slice 4), two booleans on `completion` (slice 2), nullable
lifecycle timestamps (slice 3), and keys that are not `id` (slice 9). The
declarations below were derived from `0001_initial.up.sql` by script rather than
transcribed, because thirty-five columns copied by hand is a transcription error
waiting to be found by reconciliation — and the shared contract checks every one
of them against the DDL anyway.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "COMPLETION_COLUMNS",
    "COMPLETION_EVENT_COLUMNS",
    "COMPLETION_VALIDATION_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "PostgresCompletionEventMirror",
    "PostgresCompletionMirror",
    "PostgresCompletionValidationMirror",
    "completion_divergence",
    "completion_event_divergence",
    "completion_validation_divergence",
]

COMPLETION_COLUMNS: tuple[str, ...] = (
    "run_id", "task_id", "session_id", "project", "repository_path", "branch",
    "base_branch", "head_commit", "remote", "remote_branch", "pull_request_number",
    "pull_request_url", "pull_request_state", "replaced_pull_request_number",
    "replaced_pull_request_url", "merge_commit", "merge_mode", "merge_method",
    "completion_state", "last_reason_code", "requires_human", "is_recoverable",
    "recommended_action", "validation_summary", "policy_json", "last_checked_at",
    "next_retry_at", "retry_count", "recovery_count", "version", "created_at",
    "updated_at", "review_verdict", "review_run_id", "review_summary",
)

COMPLETION_EVENT_COLUMNS: tuple[str, ...] = (
    "id", "run_id", "seq", "event_type", "reason_code", "message",
    "metadata_json", "created_at",
)

COMPLETION_VALIDATION_COLUMNS: tuple[str, ...] = (
    "id", "run_id", "attempt", "command", "exit_code", "started_at",
    "finished_at", "stdout_summary", "stderr_summary", "created_at",
)

#: Keyed by `run_id`: one completion per run, and the column is both key and
#: foreign key — the third table of that shape after `council_decision` and
#: `report`.
COMPLETION = MirroredTable(
    table="completion",
    columns=COMPLETION_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"last_checked_at", "next_retry_at", "created_at", "updated_at"}),
        flags=frozenset({"requires_human", "is_recoverable"}),
        json_values=frozenset({"policy_json"}),
    ),
    key="run_id",
    references={"run_id": "run", "task_id": "task", "session_id": "session"},
)

COMPLETION_EVENT = MirroredTable(
    table="completion_event",
    columns=COMPLETION_EVENT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"metadata_json"}),
    ),
    identity=True,
    references={"run_id": "run"},
)

COMPLETION_VALIDATION = MirroredTable(
    table="completion_validation",
    columns=COMPLETION_VALIDATION_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"started_at", "finished_at", "created_at"})),
    identity=True,
    references={"run_id": "run"},
)


class PostgresCompletionMirror(PostgresTableMirror):
    """`completion` — one row per run, keyed by `run_id`."""

    spec = COMPLETION


class PostgresCompletionEventMirror(PostgresTableMirror):
    """`completion_event` — the append-only log, identity column."""

    spec = COMPLETION_EVENT


class PostgresCompletionValidationMirror(PostgresTableMirror):
    """`completion_validation` — one row per validation attempt."""

    spec = COMPLETION_VALIDATION


completion_divergence = divergence_against(COMPLETION)
completion_event_divergence = divergence_against(
    COMPLETION_EVENT,
    """Rows where the SQLite authority and a mirror disagree on `completion_event`.

    **Takes rows in the shape SQLite stores** — `runtime/db/completion.py`'s
    `list_completion_events_stored`, not `list_completion_events`, which does
    both things that hide a column: it selects an explicit list without `id`
    and pops `metadata_json` in favour of a decoded `metadata`. Fed those, this
    pairs rows on `None` and compares a column that is not there.
    """,
)
completion_validation_divergence = divergence_against(COMPLETION_VALIDATION)
