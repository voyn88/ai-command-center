"""The provenance family's PostgreSQL mirrors (VOYN-W0-AICC-SRV-01B, slice 13).

Four tables recording where a run's work came from and how it was routed:
`run_provenance` (one row per run), `provenance_evidence` (adapter payloads),
`run_provider_route` (the provider plan) and `provider_attempt` (each try).

**The last unmet shape in the schema is here.** `provider_attempt` is keyed by
`(run_id, attempt_number)` — a *composite* primary key. Every mirror so far had
a single key column, and the machinery assumed it: `ON CONFLICT (id)` would
have named a constraint the table does not have, and `divergence` pairing rows
by one column would have collapsed every attempt of a run into one and reported
agreement. `MirroredTable.key` now takes a tuple, the statement names all of
it, and reconciliation pairs on the tuple.

Found by reading the DDL before writing the mirror. It fails the same silent
way as the previous two key surprises — the statement raises, the dual-write
hook swallows, the table never mirrors — which is why the reading happens
first and the contract now checks the declared key against the DDL's actual
primary key.

Three of the four are keyed by `run_id` alone, which makes them one-to-one with
a run; `provenance_evidence` is the exception, keyed by its own
`integrity_id`. Five `jsonb` columns between them and nothing else new.

**Why this file is in the AIOS boundary baseline.** The frozen-engine detector
classifies it under `audit`, because that category's tokens include
`provenance` — and `audit` is deliberately matched on the name alone, with no
behavioural corroboration. This module declares four `MirroredTable`s and four
mirror classes over tables that already exist; it contains no function, no SQL
and no connection handling, so it adds no audit capability. That makes it the
false positive `docs/AIOS_BOUNDARY.md` calls Direction 2, whose preferred
remedy — renaming — is unavailable: a module about `run_provenance` and
`provenance_evidence` cannot honestly drop the token, and choosing a name to
slip past a name-based detector is evasion rather than remedy.

This is the **second** such entry, after `audit_store.py` in slice 8, and two
is where a pattern starts. The structural fix is not more entries: it is to
give the `audit` category a behavioural corroboration the way `queue` has one,
after which both entries retire in the shrink direction the policy actually
wants. Recorded as `VOYN-W0-AICC-AUDIT-CATEGORY-CORROBORATION`; until it
closes, any further table of this family should be declared **inside** one of
the two already-baselined modules rather than in a third file.
"""

from __future__ import annotations

from command_center.db.mirror_support import MIRROR_UNAVAILABLE, ColumnCodec
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "MIRROR_UNAVAILABLE",
    "PROVENANCE_EVIDENCE_COLUMNS",
    "PROVIDER_ATTEMPT_COLUMNS",
    "RUN_PROVENANCE_COLUMNS",
    "RUN_PROVIDER_ROUTE_COLUMNS",
    "PostgresProvenanceEvidenceMirror",
    "PostgresProviderAttemptMirror",
    "PostgresRunProvenanceMirror",
    "PostgresRunProviderRouteMirror",
    "provenance_evidence_divergence",
    "provider_attempt_divergence",
    "run_provenance_divergence",
    "run_provider_route_divergence",
]

RUN_PROVENANCE_COLUMNS: tuple[str, ...] = (
    "run_id",
    "task_id",
    "repository_path",
    "worktree_path",
    "branch",
    "base_branch",
    "base_sha",
    "head_sha",
    "pull_request_number",
    "pull_request_url",
    "pull_request_head_sha",
    "ci_conclusions_json",
    "ci_observed_at",
    "accepted_sha",
    "accepted_at",
    "deployed_sha",
    "deployment_environment",
    "deployed_at",
    "deployment_verified_at",
    "created_at",
    "updated_at",
)

PROVENANCE_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "integrity_id",
    "run_id",
    "adapter",
    "status",
    "candidate_sha",
    "reported_sha",
    "native_payload_json",
    "normalized_json",
    "observed_at",
)

RUN_PROVIDER_ROUTE_COLUMNS: tuple[str, ...] = (
    "run_id",
    "providers_json",
    "max_attempts",
    "selection_reason",
    "policy_version",
    "created_at",
)

PROVIDER_ATTEMPT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "attempt_number",
    "provider_id",
    "outcome",
    "classification",
    "disposition",
    "error_code",
    "parent_attempt_number",
    "started_at",
    "completed_at",
)

#: Six nullable timestamps: a run's provenance accumulates as CI observes it,
#: acceptance signs it and a deployment verifies it — each stamp absent until
#: its event happens.
RUN_PROVENANCE = MirroredTable(
    table="run_provenance",
    columns=RUN_PROVENANCE_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset(
            {
                "ci_observed_at",
                "accepted_at",
                "deployed_at",
                "deployment_verified_at",
                "created_at",
                "updated_at",
            }
        ),
        json_values=frozenset({"ci_conclusions_json"}),
    ),
    key="run_id",
    references={"run_id": "run", "task_id": "task"},
)

PROVENANCE_EVIDENCE = MirroredTable(
    table="provenance_evidence",
    columns=PROVENANCE_EVIDENCE_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"observed_at"}),
        json_values=frozenset({"native_payload_json", "normalized_json"}),
    ),
    key="integrity_id",
    references={"run_id": "run"},
)

RUN_PROVIDER_ROUTE = MirroredTable(
    table="run_provider_route",
    columns=RUN_PROVIDER_ROUTE_COLUMNS,
    codec=ColumnCodec(
        timestamps=frozenset({"created_at"}),
        json_values=frozenset({"providers_json"}),
    ),
    key="run_id",
    references={"run_id": "run"},
)

#: The composite key this slice exists for. `attempt_number` is declared
#: `integer` (see `0001_initial.up.sql`), the one case in this schema of a key
#: that mixes a `text` column with a numeric one — `numeric_key_columns` says
#: so, and `test_text_key_columns_match_the_schemas_declared_types` checks it
#: against the DDL rather than trusting the comment.
PROVIDER_ATTEMPT = MirroredTable(
    table="provider_attempt",
    columns=PROVIDER_ATTEMPT_COLUMNS,
    codec=ColumnCodec(timestamps=frozenset({"started_at", "completed_at"})),
    key=("run_id", "attempt_number"),
    numeric_key_columns=frozenset({"attempt_number"}),
    references={"run_id": "run"},
)


class PostgresRunProvenanceMirror(PostgresTableMirror):
    """`run_provenance` — one row per run, keyed by `run_id`."""

    spec = RUN_PROVENANCE


class PostgresProvenanceEvidenceMirror(PostgresTableMirror):
    """`provenance_evidence` — adapter payloads, keyed by `integrity_id`."""

    spec = PROVENANCE_EVIDENCE


class PostgresRunProviderRouteMirror(PostgresTableMirror):
    """`run_provider_route` — the provider plan for one run."""

    spec = RUN_PROVIDER_ROUTE


class PostgresProviderAttemptMirror(PostgresTableMirror):
    """`provider_attempt` — keyed by `(run_id, attempt_number)`.

    The first composite key in this migration. Pairing rows by `run_id` alone
    would collapse every attempt of a run into one and report agreement it
    never checked.
    """

    spec = PROVIDER_ATTEMPT


run_provenance_divergence = divergence_against(RUN_PROVENANCE)
provenance_evidence_divergence = divergence_against(
    PROVENANCE_EVIDENCE,
    """Rows where the SQLite authority and a mirror disagree on `provenance_evidence`.

    **Takes rows in the shape SQLite stores** — `runtime/db/provenance.py`'s
    `list_provenance_evidence_stored`, not `get_provenance_evidence_for_runs`,
    which projects away both `jsonb` payloads because the read surface does not
    need them. Fed the projected rows this reports every evidence row divergent
    on two columns at once.
    """,
)
run_provider_route_divergence = divergence_against(
    RUN_PROVIDER_ROUTE,
    "Rows where the SQLite authority and a mirror disagree on `run_provider_route`.\n\n"
    "    Takes `list_provider_routes_stored`, **not** `get_provider_route` or\n"
    "    `get_provider_routes_for_runs`: both decode inline, popping\n"
    "    `providers_json` in favour of a parsed `providers` key, and fed those\n"
    "    rows this reports every route divergent on a column the mirror holds\n"
    "    correctly. See `mirror_support.divergence` for what each shape means.",
)
provider_attempt_divergence = divergence_against(PROVIDER_ATTEMPT)
