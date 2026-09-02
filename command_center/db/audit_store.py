"""`audit_run` / `audit_finding` PostgreSQL mirrors (SRV-01B, slice 8).

Part of the batch slice: no new conversion class. `checks_json` is `jsonb`
(slice 4), `audit_finding.run_id` is a foreign key to `audit_run` (slice 5),
and `started_at`/`completed_at` are nullable `timestamptz` (slice 3).

What this family *does* repeat is the trap that got slice 4 rejected, so it is
handled here in the same slice rather than after a rejection:
`runtime/db/audit.py` decodes on the way out. `_decode_run_row` pops
`checks_json` and substitutes a decoded `checks` list, and **every** public
reader of `audit_run` returns that shape — `get_audit_run`, `list_audit_runs`,
and `set_audit_run_status`'s return value. Reconciliation fed any of them
reports every run divergent on the one column that needed converting, so
`runtime/db/audit.py` gained `list_audit_runs_stored`, and
`audit_run_divergence` says which shape it takes.

`audit_finding` has no such reader: its rows come back as stored, so it needs
nothing extra. Two tables in one family with different answers to the same
question is exactly why "does this table have a runnable reconciliation entry
point?" is a per-table check rather than a per-slice one.

**Why this file is not in the AIOS boundary baseline.** The frozen-engine
detector's `audit` name signature used to classify on the name alone, which
made this module — two `MirroredTable`s and two mirror classes over tables
that already exist, with no statement of its own — the false positive
`docs/AIOS_BOUNDARY.md` calls Direction 2. Renaming, the usual remedy, was
unavailable: any name keeping the token still trips, and a name without it
would be wrong for a module about `audit_run` and `audit_finding`.

`VOYN-W0-AICC-AUDIT-CATEGORY-CORROBORATION` closed that gap the way `memory`
and `queue` already had one: `_behaves_like_an_audit_engine` in
`tests/architecture/aios_boundary.py` corroborates the `audit` name against a
single, positively-validated shape — a module that is nothing but imports,
constant table declarations and well-formed `PostgresTableMirror` subclasses.
This file matches it, so it dropped out of
`tests/architecture/AIOS_BOUNDARY_BASELINE.json` instead of carrying a
justified entry in it. A module that adds so much as a bare function call, a
lambda, or a mirror subclass with anything beyond `spec = ...` in its body
does not match the shape and keeps classifying, so the exemption cannot be
used to smuggle an actual audit engine past the gate.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "AUDIT_FINDING_COLUMNS",
    "AUDIT_RUN_COLUMNS",
    "MIRROR_UNAVAILABLE",
    "PostgresAuditFindingMirror",
    "PostgresAuditRunMirror",
    "audit_finding_divergence",
    "audit_run_divergence",
]

#: Column order as declared in `command_center/db/sql/0001_initial.up.sql`,
#: pinned against that file by a test rather than trusted to stay in step.
AUDIT_RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "project_ref",
    "status",
    "checks_json",
    "finding_count",
    "version",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
)

AUDIT_FINDING_COLUMNS: tuple[str, ...] = (
    "id",
    "run_id",
    "category",
    "severity",
    "summary",
    "file_path",
    "loc",
    "status",
    "owner",
    "project_ref",
    "promoted_task_id",
    "version",
    "created_at",
    "updated_at",
)

#: `started_at` and `completed_at` are both nullable: a run is created before it
#: starts and has no completion until it ends, so `None` is a real value on
#: both sides rather than an absence to paper over.
AUDIT_RUN = MirroredTable(
    table="audit_run",
    columns=AUDIT_RUN_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"started_at", "completed_at", "created_at", "updated_at"}),
        json_values=frozenset({"checks_json"}),
    ),
)

AUDIT_FINDING = MirroredTable(
    table="audit_finding",
    columns=AUDIT_FINDING_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"created_at", "updated_at"})),
    references={"run_id": "audit_run"},
)


class PostgresAuditRunMirror(PostgresTableMirror):
    """The `audit_run` table — the family's foreign-key parent."""

    spec = AUDIT_RUN


class PostgresAuditFindingMirror(PostgresTableMirror):
    """The `audit_finding` table — its child."""

    spec = AUDIT_FINDING


audit_run_divergence = divergence_against(
    AUDIT_RUN,
    """Rows where the SQLite authority and a mirror disagree on `audit_run`.

    **Takes rows in the shape SQLite stores** — `runtime/db/audit.py`'s
    `list_audit_runs_stored`, not `get_audit_run` / `list_audit_runs` /
    `set_audit_run_status`, all of which return `_decode_run_row` output with
    `checks_json` replaced by a decoded `checks`. Fed any of those, this reports
    every run divergent on the one column that needed converting, and the
    failure looks like a broken mirror rather than a wrong question.
    """,
)

audit_finding_divergence = divergence_against(AUDIT_FINDING)
