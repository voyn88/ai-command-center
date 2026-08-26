"""The proposal family's PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 15).

The last three tables in the schema: `proposal` and its two append-only
children, `proposal_event` and `proposal_evidence`. With these declared, all 33
tables of the accepted correspondence map have mirrors.

No new conversion class. Four `jsonb` columns on `proposal` alone, one on each
child, identity columns on both children (slice 6), a boolean on each of
`proposal` and `proposal_evidence` (slice 2), and three foreign keys on
`proposal` — two of which are `ON DELETE SET NULL` rather than `CASCADE`, which
changes nothing for the mirror: the target declares the same rules, and the
mirror never invents a parent either way.

Declarations derived from `0001_initial.up.sql` by script, then checked against
it by the shared contract.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "MIRROR_UNAVAILABLE",
    "PROPOSAL_COLUMNS",
    "PROPOSAL_EVENT_COLUMNS",
    "PROPOSAL_EVIDENCE_COLUMNS",
    "PostgresProposalEventMirror",
    "PostgresProposalEvidenceMirror",
    "PostgresProposalMirror",
    "proposal_divergence",
    "proposal_event_divergence",
    "proposal_evidence_divergence",
]

PROPOSAL_COLUMNS: tuple[str, ...] = (
    "id", "kind", "project", "task_id", "title", "rationale", "state", "risk_level",
    "policy_json", "eligibility_json", "plan_json", "evidence_digest", "requires_human",
    "last_reason_code", "decided_by", "decision_reason", "dispatched_run_id",
    "dispatched_task_id", "version", "created_at", "updated_at", "parameters_json",
)

PROPOSAL_EVENT_COLUMNS: tuple[str, ...] = (
    "id", "proposal_id", "seq", "event_type", "from_state", "to_state", "actor",
    "reason_code", "message", "metadata_json", "created_at",
)

PROPOSAL_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "id", "proposal_id", "seq", "kind", "source", "summary", "observed_at",
    "is_blocker", "data_json", "created_at",
)

PROPOSAL = MirroredTable(
    table="proposal",
    columns=PROPOSAL_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at", "updated_at"}),
        flags=frozenset({"requires_human"}),
        json_values=frozenset(
            {"policy_json", "eligibility_json", "plan_json", "parameters_json"}
        ),
    ),
    references={
        "task_id": "task",
        "dispatched_run_id": "run",
        "dispatched_task_id": "task",
    },
)

PROPOSAL_EVENT = MirroredTable(
    table="proposal_event",
    columns=PROPOSAL_EVENT_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"metadata_json"}),
    ),
    identity=True,
    references={"proposal_id": "proposal"},
)

PROPOSAL_EVIDENCE = MirroredTable(
    table="proposal_evidence",
    columns=PROPOSAL_EVIDENCE_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"observed_at", "created_at"}),
        flags=frozenset({"is_blocker"}),
        json_values=frozenset({"data_json"}),
    ),
    identity=True,
    references={"proposal_id": "proposal"},
)


class PostgresProposalMirror(PostgresTableMirror):
    """`proposal` — the family's parent."""

    spec = PROPOSAL


class PostgresProposalEventMirror(PostgresTableMirror):
    """`proposal_event` — append-only, identity column."""

    spec = PROPOSAL_EVENT


class PostgresProposalEvidenceMirror(PostgresTableMirror):
    """`proposal_evidence` — append-only, identity column."""

    spec = PROPOSAL_EVIDENCE


proposal_divergence = divergence_against(PROPOSAL)
proposal_event_divergence = divergence_against(
    PROPOSAL_EVENT,
    """Rows where the SQLite authority and a mirror disagree on `proposal_event`.

    **Takes rows in the shape SQLite stores** — `runtime/db/proposal.py`'s
    `list_proposal_events_stored`, not `list_proposal_events`, which hands out
    the caller-facing shape. Fed that, this reports every event divergent.

    `list_proposal_events_stored` takes no `proposal_id` (SRV-07f): it is a
    whole-table reader, because reconciling the table means reconciling every
    proposal's events, not one proposal fanned out over the caller.
    """,
)
proposal_evidence_divergence = divergence_against(
    PROPOSAL_EVIDENCE,
    "Rows where the SQLite authority and a mirror disagree on `proposal_evidence`.\n\n"
    "    Takes `list_proposal_evidence_stored`, **not** `list_proposal_evidence`:\n"
    "    the public reader pops `data_json` and returns a parsed `data` key, so\n"
    "    reconciliation fed its rows reports every one of them divergent while\n"
    "    agreeing about a column PostgreSQL does not have. See\n"
    "    `mirror_support.divergence` for what each reported shape means.\n\n"
    "    Takes no `proposal_id` (SRV-07f): it is a whole-table reader, because\n"
    "    reconciling the table means reconciling every proposal's evidence, not\n"
    "    one proposal fanned out over the caller.",
)
