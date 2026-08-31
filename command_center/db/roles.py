"""Least-privilege role inventory for the AICC server database.

This module is the single source of truth for who may touch what. The same
`PRIVILEGES` mapping renders the `GRANT` statements that provision a database
and drives `tests/db/test_role_privileges.py`, which connects *as each role*
and asserts both halves of the matrix — what the role can do and what it must
not. That coupling is deliberate: the defect behind `VOYN-W0-SEC-AUDIT-PG-CRED`
was a grant that no test exercised, so a grant added here without a matching
row in the matrix cannot silently widen access.

Four roles, by what fails if each one is compromised:

* `aicc_migrator` owns the schema and is the only role with DDL. It is used by
  the migration runner and by nothing else at runtime.
* `aicc_app` runs the API and dispatcher: full DML on every domain table, no
  DDL, no ownership, so a SQL-injection foothold in the web layer cannot drop
  or alter a table.
* `aicc_worker` runs on execution hosts, which are the least trusted component
  (they run agent processes against untrusted repository content). It sees
  only the queue and execution tables. Governance data — proposals, council
  motions and votes, audit findings, provenance evidence, the marketplace and
  the model registry — is unreachable from a worker credential, so a
  compromised execution host cannot read or forge the decision record. The
  independent review's verdict lives on `completion`, which a worker does write,
  so the three `review_*` columns are carved out via column-level grants (see
  COLUMN_PRIVILEGES) rather than left inside a table-wide UPDATE.
* `aicc_operator` is the human admission lever added with `0003_worker_enrollment`.
  It holds no DML on any domain table at all; its whole reach is EXECUTE on
  minting an enrolment ticket, revoking one, and revoking a host. It exists
  because two decisions must be unreachable from the control plane — readmitting
  a host an incident retired, and taking a host offline — and "unreachable from
  the control plane" can only be expressed as a second, more trusted
  `session_user`. A compromised operator credential can admit and evict hosts; it
  still cannot read any host's secret, because no role can.

`DELETE` is granted to no role on any table: this schema is an append/update
ledger, and row removal is a migration-time operation performed by the owner.

The queue-claim protocol added by `0002_queue_claim` is granted differently, and
the difference is the point. `aicc_worker` gets **no table privilege at all** on
`work_item`, `work_attempt`, `work_result` or `work_event`; its entire reach is
`EXECUTE` on the four functions that are the four steps of the protocol. That
turns the acceptance clauses into properties of the grant graph rather than of a
`WHERE` clause somewhere:

* "two workers cannot own one attempt" — a worker cannot `UPDATE work_item` at
  all, so `queue_claim()` is the only route to a claim, and the exclusivity
  lives inside it. There is no second route to audit.
* "a stale owner cannot write a result" — a worker cannot `INSERT` into
  `work_result`, so `queue_complete()` is the only route, and it fences.
* "acknowledge only after a durable result" — the one granted path that can
  move an item to `succeeded` takes the result as a required argument.

`aicc_app` is the mirror image: it reads the queue and its audit and gets no
write privilege, because "an item became dead" or "an item was re-delivered"
must have exactly one code path, which is also the one that audits. It is *not*
granted `queue_claim()`: dispatch is not execution, and a control plane that
could claim would be recording an executor it was merely told about.
`work_attempt` is granted to nobody, the same treatment the `review_*` columns
of `completion` get, because it holds `claim_token_hash` — the capability
itself. Reads go through `work_attempt_public`, which omits it.
"""

from __future__ import annotations

from types import MappingProxyType

__all__ = [
    "APP_ROLE",
    "MIGRATOR_ROLE",
    "OPERATOR_ROLE",
    "WORKER_ROLE",
    "ALL_ROLES",
    "ALL_TABLES",
    "ALL_VIEWS",
    "COLUMN_PRIVILEGES",
    "FUNCTION_PRIVILEGES",
    "merge_privileges",
    "VIEW_PRIVILEGES",
    "apply_bootstrap",
    "apply_table_grants",
    "IDENTITY_SEQUENCES",
    "PRIVILEGES",
    "render_bootstrap",
    "render_worker_host_role",
    "render_grants",
    "render_table_grants",
    "render_role_creation",
]

MIGRATOR_ROLE = "aicc_migrator"
APP_ROLE = "aicc_app"
WORKER_ROLE = "aicc_worker"
#: The tier-0 admission role added by `0003_worker_enrollment`. It exists for
#: exactly one reason: readmitting a host an incident retired must not be
#: reachable by the control plane, and "not reachable by the control plane"
#: needs a second, more trusted `session_user` to be expressible at all. It
#: carries no DML on any domain table — its whole reach is EXECUTE on the
#: admission and revocation levers.
OPERATOR_ROLE = "aicc_operator"

ALL_ROLES = (MIGRATOR_ROLE, APP_ROLE, WORKER_ROLE, OPERATOR_ROLE)

#: The roles the matrix grants to. The migrator is excluded because its rights
#: come from ownership; naming it here rather than repeating a literal tuple at
#: each of the four loops in `render_table_grants()` is what stops a fifth role
#: from being added to three of them.
_GRANTED_ROLES = (APP_ROLE, WORKER_ROLE, OPERATOR_ROLE)

# Every domain table created by the migration set, plus the runner's own
# bookkeeping table. `test_upgrade_creates_the_declared_schema` fails if the
# database and this tuple disagree, so a table added by a later migration cannot
# end up with no declared owner of its access policy — and being listed here is
# not the same as being reachable: `work_attempt` appears with an empty
# privilege set for every role, which is a declaration that nobody may read it,
# not an omission.
ALL_TABLES: tuple[str, ...] = (
    "advisor_proposal",
    "audit_finding",
    "audit_run",
    "backlog_dependency",
    "backlog_event",
    "backlog_evidence",
    "backlog_task",
    "backlog_task_remediation",
    "backlog_scan_cursor",
    "backlog_writer_lease",
    "completion",
    "completion_event",
    "completion_validation",
    "conflict",
    "contact",
    "council_decision",
    "council_event",
    "council_vote",
    "digest_item",
    "enrollment_ticket",
    "market_install_log",
    "market_item",
    "message",
    "model_entry",
    "model_event",
    "motion",
    "networking_invitation",
    "owner_item",
    "principal",
    "principal_credential",
    "principal_event",
    "proposal",
    "proposal_event",
    "proposal_evidence",
    "provenance_evidence",
    "provider_attempt",
    "queue_entry",
    "report",
    "run",
    "run_event",
    "run_finalization_claim",
    "run_provenance",
    "run_provider_route",
    "schema_migration",
    "session",
    "task",
    "work_attempt",
    "work_event",
    "work_item",
    "work_result",
    "worker_host_fingerprint",
)

# Views created by the migration set. Separate from `ALL_TABLES` because the
# schema assertions compare `ALL_TABLES` against `BASE TABLE` rows: folding the
# two together would make a view able to stand in for a dropped table.
ALL_VIEWS: tuple[str, ...] = (
    "backlog_eligible",
    "enrollment_ticket_public",
    "principal_credential_public",
    "work_attempt_public",
    "work_dlq",
    "work_item_public",
)

# Tables whose primary key is `bigint GENERATED ALWAYS AS IDENTITY`, mapped to
# the sequence PostgreSQL creates for them. Inserting into these needs USAGE on
# the sequence in addition to INSERT on the table.
IDENTITY_SEQUENCES: MappingProxyType[str, str] = MappingProxyType(
    {
        "backlog_event": "backlog_event_event_id_seq",
        "backlog_evidence": "backlog_evidence_evidence_id_seq",
        "completion_event": "completion_event_id_seq",
        "completion_validation": "completion_validation_id_seq",
        "council_event": "council_event_id_seq",
        "model_event": "model_event_id_seq",
        "principal_event": "principal_event_id_seq",
        "proposal_event": "proposal_event_id_seq",
        "proposal_evidence": "proposal_evidence_id_seq",
        "run_event": "run_event_id_seq",
        "work_event": "work_event_id_seq",
        "worker_host_fingerprint": "worker_host_fingerprint_id_seq",
    }
)

_APP_DML = frozenset({"SELECT", "INSERT", "UPDATE"})
_WORKER_WRITE = frozenset({"SELECT", "INSERT", "UPDATE"})
_READ = frozenset({"SELECT"})
_NONE: frozenset[str] = frozenset()


def merge_privileges(
    *contributions: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Union the privilege sets of several contributions, per table.

    The one operation this module must never express as `a | b`. Two tasks
    adding rows for the same table is the normal case as the schema grows, and
    a dict update keeps only the later value — so the earlier task's grants
    disappear with no error, no warning, and both tasks' own tests still green,
    because each is correct in isolation. The failure surfaces later as a role
    that may execute nothing.

    The blanket per-table default is deliberately *not* a contribution: folding
    it in here would widen every narrowed table back to full DML, since a union
    can only grow. Defaults apply where nothing was declared; declarations
    union with each other.
    """
    merged: dict[str, frozenset[str]] = {}
    for contribution in contributions:
        for table, privileges in contribution.items():
            merged[table] = frozenset(merged.get(table, frozenset())) | frozenset(
                privileges
            )
    return merged


# The queue-claim tables (0002), which the control plane may read and may not
# write: every state change goes through a function so that it audits. The
# entries exist rather than being omitted — an absent key is a table nobody
# declared a policy for, an empty set is a declared refusal.
_APP_QUEUE_TABLES: dict[str, frozenset[str]] = {
    "work_item": _READ,
    "work_result": _READ,
    "work_event": _READ,
    "work_attempt": _NONE,  # holds `claim_token_hash`; read via the view
}

# Host-local fencing state has no generic runtime reader or writer.  A future
# PostgreSQL authority must expose a dedicated CAS function, never blanket DML.
_FINALIZATION_CLAIM_TABLES: dict[str, frozenset[str]] = {
    "run_finalization_claim": _NONE,
}

# The structured backlog store (0005, BO-S1), the queue-claim idiom again:
# the control plane READS; every write travels through a SECURITY DEFINER
# function so that it audits and the status machine cannot be bypassed —
# there is no SQL path that performs OPEN -> DONE. Workers get nothing: an
# execution host has no business reading the programme's plan, and a
# compromised one must not learn it.
#
# TODO(VOYN-W0-BACKLOG-ORCHESTRATOR BO-S1, after #321 merged): #321's grant
# compliance checker now verifies this matrix against the live catalog;
# extend tests/db/test_grant_compliance.py's provisioning coverage to the
# backlog tables/functions in the first post-#321 slice. [#321 merged
# 2026-08-19 as 8b9c89d — the extension rides the next BO slice to keep this
# PR's surface reviewable.]
_APP_BACKLOG_TABLES: dict[str, frozenset[str]] = {
    "backlog_task": _READ,
    "backlog_dependency": _READ,
    "backlog_evidence": _READ,
    "backlog_event": _READ,
    "backlog_writer_lease": _READ,
    "backlog_task_remediation": _READ,
    "backlog_scan_cursor": _READ,
}

_WORKER_BACKLOG_TABLES: dict[str, frozenset[str]] = {
    "backlog_task": _NONE,
    "backlog_dependency": _NONE,
    "backlog_evidence": _NONE,
    "backlog_event": _NONE,
    "backlog_writer_lease": _NONE,
    "backlog_task_remediation": _NONE,
    "backlog_scan_cursor": _NONE,
}

# The enrolment tables (0003), for the control plane. Read-only, and two of them
# not even that.
#
# `enrollment_ticket` and `principal_credential` are granted to NOBODY: they
# hold `ticket_hash` and `secret_hash`, which are the capabilities themselves,
# and the redacted views are the only read path — the same treatment
# `work_attempt` gets. That is what makes "a compromised control plane cannot
# read a pending ticket's secret, and cannot learn any enrolled host's" a
# property of the grant graph rather than of a WHERE clause.
#
# `principal` is readable and not writable, because `principal.state` IS the
# block-list: a control plane that could `UPDATE` it could un-suspend a host an
# incident retired, which is precisely the decision `enroll_mint_ticket()`
# reserves to an operator. `worker_host_fingerprint` is unreachable for the same
# family of reason — it is the record that reveals a clone, so the component
# most likely to be compromised must be unable to erase or forge it.
_APP_ENROLMENT_TABLES: dict[str, frozenset[str]] = {
    "principal": _READ,
    "principal_event": _READ,
    "principal_credential": _NONE,
    "enrollment_ticket": _NONE,
    "worker_host_fingerprint": _NONE,
}

# The same tables for the operator, which is the only role that may see the
# fingerprint history: classifying a re-enrolment as a rebuild, a hardware
# change or a clone is the decision this role exists to make.
_OPERATOR_ENROLMENT_TABLES: dict[str, frozenset[str]] = {
    "principal": _READ,
    "principal_event": _READ,
    "worker_host_fingerprint": _READ,
    "principal_credential": _NONE,
    "enrollment_ticket": _NONE,
}

# A worker reaches none of it. Not even `SELECT` on `principal`: enumerating the
# fleet tells a compromised execution host which other hosts exist and what they
# are called, and a worker's own identity arrives in the verdict
# `identity_assert()` returns.
_WORKER_ENROLMENT_TABLES: dict[str, frozenset[str]] = {
    "principal": _NONE,
    "principal_event": _NONE,
    "principal_credential": _NONE,
    "enrollment_ticket": _NONE,
    "worker_host_fingerprint": _NONE,
}

# Columns of `completion` that record the independent review's outcome. A
# worker writes its own completion row, but table-level UPDATE would also let a
# compromised execution host stamp `review_verdict = 'approved'` on any run —
# forging exactly the decision the review gate exists to make. Granted per
# column instead, so the verdict is writable only by the app.
_REVIEW_COLUMNS = ("review_verdict", "review_run_id", "review_summary")

_COMPLETION_COLUMNS = (
    "run_id",
    "task_id",
    "session_id",
    "project",
    "repository_path",
    "branch",
    "base_branch",
    "head_commit",
    "remote",
    "remote_branch",
    "pull_request_number",
    "pull_request_url",
    "pull_request_state",
    "replaced_pull_request_number",
    "replaced_pull_request_url",
    "merge_commit",
    "merge_mode",
    "merge_method",
    "completion_state",
    "last_reason_code",
    "requires_human",
    "is_recoverable",
    "recommended_action",
    "validation_summary",
    "policy_json",
    "last_checked_at",
    "next_retry_at",
    "retry_count",
    "recovery_count",
    "version",
    "created_at",
    "updated_at",
    *_REVIEW_COLUMNS,
)

_WORKER_COMPLETION_COLUMNS = tuple(
    column for column in _COMPLETION_COLUMNS if column not in _REVIEW_COLUMNS
)

# role -> table -> privilege -> the columns it is limited to. A privilege listed
# here is granted per column; anything not listed is granted table-wide.
COLUMN_PRIVILEGES: MappingProxyType[
    str, MappingProxyType[str, MappingProxyType[str, tuple[str, ...]]]
] = MappingProxyType(
    {
        WORKER_ROLE: MappingProxyType(
            {
                "completion": MappingProxyType(
                    {
                        "INSERT": _WORKER_COMPLETION_COLUMNS,
                        "UPDATE": _WORKER_COMPLETION_COLUMNS,
                    }
                )
            }
        )
    }
)

# The queue and execution tables a worker legitimately writes while running a
# job. Anything absent from this mapping is unreachable for `aicc_worker`.
_WORKER_TABLES: dict[str, frozenset[str]] = {
    # Read-only since 0002. The `UPDATE` this used to carry was labelled
    # "claims, never enqueues", and it let any worker set any queue row's state
    # directly, with no exclusivity and no attribution — the hole the claim
    # protocol closes. It was also futile: `queue_store.replace_entries()`
    # rebuilds this mirror wholesale on every sync from the authoritative JSON
    # queue, so a claim written here is destroyed by the next sync, silently,
    # and the worker has no `DELETE` privilege with which to observe the loss.
    # Claims live in `work_item` and are taken through `queue_claim()`.
    #
    # The grant changes; the table does not. `queue_entry` sits outside the
    # SRV-07 parity gate, so altering its shape would be an uncovered data
    # migration — 0002 does not touch it.
    "queue_entry": _READ,
    "run": _WORKER_WRITE,
    "run_event": _WORKER_WRITE,
    "completion": _WORKER_WRITE,
    "completion_event": _WORKER_WRITE,
    "completion_validation": _WORKER_WRITE,
    "report": _WORKER_WRITE,
    # Read-only context a worker needs to execute the run it claimed.
    "task": _READ,
    "session": _READ,
    "schema_migration": _READ,  # startup compatibility check
    # The claim protocol, declared as reachable through nothing. Not even
    # SELECT: enumerating the queue tells a compromised execution host what else
    # is pending and which hosts hold it. A worker's own work arrives in the
    # verdict `queue_claim()` returns, which carries the payload.
    "work_item": _NONE,
    "work_attempt": _NONE,
    "work_result": _NONE,
    "work_event": _NONE,
}

# Views are granted separately from tables: `information_schema` reports them
# through the same catalog, so folding them into `PRIVILEGES` would let a view
# silently satisfy an assertion about a table.
VIEW_PRIVILEGES: MappingProxyType[str, MappingProxyType[str, frozenset[str]]] = (
    MappingProxyType(
        {
            APP_ROLE: MappingProxyType(
                {
                    "backlog_eligible": _READ,
                    "enrollment_ticket_public": _READ,
                    "principal_credential_public": _READ,
                    "work_attempt_public": _READ,
                    "work_dlq": _READ,
                    "work_item_public": _READ,
                }
            ),
            WORKER_ROLE: MappingProxyType({}),
            OPERATOR_ROLE: MappingProxyType(
                {
                    "enrollment_ticket_public": _READ,
                    "principal_credential_public": _READ,
                }
            ),
        }
    )
)

# The four steps of the claim protocol, and nothing else. Signatures rather than
# bare names because PostgreSQL identifies a function by its argument types, and
# `GRANT EXECUTE ON FUNCTION queue_claim` would be ambiguous the moment an
# overload appeared.
_WORKER_FUNCTIONS = (
    "queue_claim(text, text, integer)",
    "queue_heartbeat(text, text)",
    "queue_complete(text, text, jsonb)",
    "queue_fail(text, text, text, boolean)",
)

# Deliberately not `queue_claim`: only a role that PostgreSQL authenticated as a
# worker may become the executor of an attempt.
_APP_FUNCTIONS = (
    "queue_enqueue(text, text, jsonb, text, text, integer, integer, integer, integer)",
    "queue_reap()",
    "queue_redrive(text, integer)",
)

# The backlog store's whole write surface (0005, BO-S1). Control-plane
# privileges: workers are deliberately absent.
_APP_BACKLOG_FUNCTIONS = (
    "backlog_upsert_task(text, text, text, text, text, text, text, text)",
    "backlog_transition(text, text, bigint)",
    "backlog_record_evidence(text, text, text)",
    "backlog_record_remediation(text, text, text, text)",
    "backlog_add_dependency(text, text)",
    "backlog_lease_acquire(text, text, integer)",
    "backlog_lease_heartbeat(text, text, integer)",
    "backlog_lease_release(text, text)",
    # BO-S2, the planner's atomic acts (0006).
    "backlog_dispatch(text, text, integer, integer, jsonb, integer)",
    # BO-S3, result ingest (0007; replaced 0006's backlog_release_terminal).
    "backlog_ingest_results(text)",
    "backlog_return_to_pool(text, text)",
    # DEFER_TO_USER -> OPEN for technical parks only (0014); the function is
    # the classification gate, so granting it does not grant a generic unpark.
    "backlog_resume_deferred(text)",
    # OPEN -> DEFER_TO_USER for a task the fleet cannot satisfy, called by the
    # planner INSTEAD OF backlog_dispatch (0017,
    # VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR).
    "backlog_park_requires_authority(text, text)",
    # The persisted scan cursor for the tick windows (0015): returns this
    # tick's offset and advances atomically per invocation.
    "backlog_scan_claim(text, text, text)",
    # Triage of raw findings (0008): UNTRIAGED -> OPEN/NEEDS_REFINEMENT/DONE/DECIDED.
    "backlog_triage(text, text, text)",
)

# The enrolment surface (0003), split by who may do what.
#
# A worker gets two entries and no third. It may prove its own identity and
# rotate its own secret — rotation is authorised by possession of the current
# secret, so it changes WHICH secret works and nothing else — and it may not
# mint a ticket (enrolment is not a peer-to-peer gossip protocol), redeem one
# (a single compromised host must not be able to consume the fleet's pending
# enrolments), or revoke anything.
_WORKER_ENROLMENT_FUNCTIONS = (
    "identity_assert(text)",
    "identity_current_credential(text)",
    "enroll_rotate_self(text, text, text)",
)

# The control plane may bring up a NEW host without a human at 3am, and redeem
# on a host's behalf, because the enrolling host has no credential yet and
# something has to make the call. It is deliberately NOT granted
# `identity_revoke_principal`: revoking a host is an incident decision, and a
# compromised control plane that could take the fleet offline is a different
# blast radius from one that can add to it.
_APP_ENROLMENT_FUNCTIONS = (
    "enroll_mint_ticket(text, text, text, inet, interval, text)",
    "enroll_redeem_ticket(text, text, text, jsonb)",
    "enroll_revoke_ticket(text, text)",
    "enroll_sweep_expired()",
    "identity_sweep_expired()",
)

# The operator's levers, including the two the control plane must not have:
# readmitting a retired host (through a `re_enroll` mint, gated inside the
# function on the caller's tier) and revoking one.
_OPERATOR_FUNCTIONS = (
    "enroll_mint_ticket(text, text, text, inet, interval, text)",
    "enroll_revoke_ticket(text, text)",
    "enroll_sweep_expired()",
    "identity_revoke_principal(text, text)",
    "identity_sweep_expired()",
)

FUNCTION_PRIVILEGES: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        # Concatenated rather than replaced, for the reason `merge_privileges`
        # exists: a second task's grants must add to the first's, and the
        # failure mode of getting it wrong — a role that may execute nothing —
        # is silent in each task's own suite.
        APP_ROLE: _APP_FUNCTIONS + _APP_ENROLMENT_FUNCTIONS + _APP_BACKLOG_FUNCTIONS,
        WORKER_ROLE: _WORKER_FUNCTIONS + _WORKER_ENROLMENT_FUNCTIONS,
        OPERATOR_ROLE: _OPERATOR_FUNCTIONS,
    }
)

PRIVILEGES: MappingProxyType[str, MappingProxyType[str, frozenset[str]]] = (
    MappingProxyType(
        {
            # The migrator owns every table, so its DDL rights come from
            # ownership rather than grants; it needs no row-level grants of its
            # own and receives none here.
            MIGRATOR_ROLE: MappingProxyType({}),
            APP_ROLE: MappingProxyType(
                merge_privileges(
                    # The blanket default, applied only to tables no task has
                    # declared a policy for. It is kept out of the merge on
                    # purpose: a union can only widen, so folding the default in
                    # would restore full DML on every table a task deliberately
                    # narrowed.
                    #
                    # The ledger is read by the readiness probe and written only
                    # by the migrator. Table-level write here would let an
                    # injection foothold in the web layer rewrite a checksum —
                    # defeating the one guard that stops two environments
                    # reporting the same version for different schemas — or fake
                    # a version so /readyz reports healthy against an unmigrated
                    # database.
                    {
                        table: (_READ if table == "schema_migration" else _APP_DML)
                        for table in ALL_TABLES
                        if table not in _APP_QUEUE_TABLES
                        and table not in _APP_ENROLMENT_TABLES
                        and table not in _APP_BACKLOG_TABLES
                        and table not in _FINALIZATION_CLAIM_TABLES
                    },
                    # Declared policies. A second task adding rows here for a
                    # table this one already names must union with it, not
                    # replace it — `merge_privileges` is what makes that true.
                    _APP_QUEUE_TABLES,
                    _APP_ENROLMENT_TABLES,
                    _APP_BACKLOG_TABLES,
                    _FINALIZATION_CLAIM_TABLES,
                )
            ),
            WORKER_ROLE: MappingProxyType(
                merge_privileges(
                    _WORKER_TABLES,
                    _WORKER_ENROLMENT_TABLES,
                    _WORKER_BACKLOG_TABLES,
                    _FINALIZATION_CLAIM_TABLES,
                )
            ),
            # No blanket default: this role is not a general-purpose one, and
            # folding the default in would hand the admission lever DML on every
            # domain table in the schema.
            OPERATOR_ROLE: MappingProxyType(
                merge_privileges(_OPERATOR_ENROLMENT_TABLES, _FINALIZATION_CLAIM_TABLES)
            ),
        }
    )
)


def render_role_creation(role: str) -> str:
    """SQL creating `role` as a NOLOGIN group if it does not already exist.

    Passwords are never rendered here. Login roles and their secrets are
    provisioned by the operator (or by `docker-compose.server.yml` from the
    environment), so no credential can be committed to this repository or
    leak into a migration log.

    Roles are cluster-level, not database-level: the test suite runs one
    throwaway database per xdist worker, all sharing one cluster, and every
    place that provisions roles (this repo has two: `render_bootstrap()`
    below, and the test suite's own `role_passwords` fixture, which calls
    this function directly to give the roles LOGIN -- found live,
    2026-08-21, when a first fix that only guarded `render_bootstrap()`'s
    call site did not close the flake, because that fixture's identical
    "IF NOT EXISTS THEN CREATE ROLE" races it independently) can run this
    concurrently from a different worker's connection. The check ("does the
    role exist") and the act ("create it") are two statements, not one, so
    they are not atomic across two *different* transactions racing each
    other -- only within a single one. `pg_advisory_xact_lock` here, inside
    the same DO block as the check and the create, is cluster-scoped like
    the role it guards and is released automatically when this statement's
    (or its enclosing transaction's) implicit transaction ends, so it
    protects every caller by construction instead of depending on each call
    site remembering to wrap itself (VOYN-W0-AICC-MIGRATOR-PASSWORD-FLAKE).
    """
    _require_identifier(role)
    return (
        "DO $$\n"
        "BEGIN\n"
        "    PERFORM pg_advisory_xact_lock(7823649102);\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"        CREATE ROLE {role} NOLOGIN;\n"
        "    END IF;\n"
        "END\n"
        "$$;"
    )


def render_bootstrap(schema: str = "public") -> list[str]:
    """Cluster- and schema-level setup. Must run as a superuser or schema owner.

    Separate from `render_table_grants()` because the two need different
    rights: `REVOKE ... ON SCHEMA public` requires ownership of the schema,
    which belongs to `postgres`/`pg_database_owner` and not to the migrator,
    while the table grants require ownership of the tables, which the migrator
    has and the superuser should not need to be involved in. Collapsing them
    would force every routine migration to run as a superuser.
    """
    _require_identifier(schema)
    # Concurrency-safety for the CREATE ROLE statements lives inside
    # render_role_creation() itself (see its docstring) rather than here, so
    # every caller gets it, not just this one.
    statements: list[str] = [render_role_creation(role) for role in ALL_ROLES]

    # PUBLIC is granted CREATE and USAGE on `public` by default in PostgreSQL
    # below 15, which would let any authenticated role create objects in the
    # application schema. Revoke unconditionally so the policy does not depend
    # on the server's major version.
    statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")
    statements.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;")
    statements.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC;")
    statements.append(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC;")

    statements.append(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {MIGRATOR_ROLE};")
    for role in _GRANTED_ROLES:
        statements.append(f"GRANT USAGE ON SCHEMA {schema} TO {role};")

    # Role DDL for the enrolment protocol (0003). Issuing a per-host credential
    # is `CREATE ROLE` plus `ALTER ROLE ... PASSWORD`, and revoking one is
    # `ALTER ROLE ... NOLOGIN` plus `pg_terminate_backend`; none of that is
    # reachable without these three cluster-level grants, which is why they are
    # here and not in a migration — a migration runs AS the migrator and cannot
    # widen it.
    #
    # Stated rather than buried, because it is a real weakening: this makes the
    # schema owner also the credential minter. `CREATEROLE` in PostgreSQL 16+ is
    # bounded to roles the grantee itself created, so it cannot reach `aicc_app`
    # or `aicc_operator` — but it can mint hosts. The two capabilities should sit
    # on different roles; the reason they do not yet is that
    # `render_table_grants()` opens with `REVOKE ALL ON ALL FUNCTIONS IN SCHEMA`
    # executed as the migrator, and PostgreSQL refuses that statement outright
    # once any function in the schema has a different owner (measured, not
    # assumed). Splitting the owner therefore needs this renderer to learn about
    # second owners first. Filed as VOYN-W0-AICC-SRV-03d.
    # `ADMIN TRUE` is what lets `CREATE ROLE ... IN ROLE aicc_worker` grant the
    # membership; `INHERIT FALSE, SET FALSE` is what stops the migrator picking
    # up the worker's privileges or becoming it as a side effect. Terminating a
    # revoked host's live backends needs `pg_signal_backend` — without it, a
    # leaked password that is already connected keeps its connection until the
    # credential's TTL runs out, so revocation would stop future authentication
    # only.
    #
    # All three writes are guarded IF-NOT-ALREADY-SET, not just lock-guarded:
    # apply_bootstrap() runs on every test in this suite (hundreds of calls per
    # run), and an unconditional ALTER ROLE / GRANT re-issues a real catalog
    # write on the shared cluster-level aicc_migrator row every single time,
    # even though nothing changes after the first. The advisory lock (acquired
    # above, in render_role_creation()) does serialize those writes correctly,
    # but under sustained load across many xdist workers each producing
    # hundreds of needless real writes to the SAME row, the write volume alone
    # was enough to occasionally surface `tuple concurrently updated` even with
    # every write serialized -- live-reproduced 2026-08-21, cleared by cutting
    # the writes themselves down to once per fresh cluster instead of once per
    # test. This mirrors render_role_creation()'s own IF-NOT-EXISTS discipline.
    statements.append(
        f"DO $$\n"
        f"BEGIN\n"
        f"    PERFORM pg_advisory_xact_lock(7823649102);\n"
        f"    IF NOT (SELECT rolcreaterole FROM pg_roles WHERE rolname = '{MIGRATOR_ROLE}') THEN\n"
        f"        ALTER ROLE {MIGRATOR_ROLE} CREATEROLE;\n"
        f"    END IF;\n"
        f"    IF NOT EXISTS (\n"
        f"        SELECT 1 FROM pg_auth_members m\n"
        f"        JOIN pg_roles r ON r.oid = m.roleid\n"
        f"        JOIN pg_roles g ON g.oid = m.member\n"
        f"        WHERE r.rolname = '{WORKER_ROLE}' AND g.rolname = '{MIGRATOR_ROLE}'\n"
        f"          AND m.admin_option\n"
        f"    ) THEN\n"
        f"        GRANT {WORKER_ROLE} TO {MIGRATOR_ROLE} "
        f"WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;\n"
        f"    END IF;\n"
        f"    IF NOT EXISTS (\n"
        f"        SELECT 1 FROM pg_auth_members m\n"
        f"        JOIN pg_roles r ON r.oid = m.roleid\n"
        f"        JOIN pg_roles g ON g.oid = m.member\n"
        f"        WHERE r.rolname = 'pg_signal_backend' AND g.rolname = '{MIGRATOR_ROLE}'\n"
        f"    ) THEN\n"
        f"        GRANT pg_signal_backend TO {MIGRATOR_ROLE};\n"
        f"    END IF;\n"
        f"END\n"
        f"$$;"
    )

    return statements


def render_table_grants(
    schema: str = "public",
    *,
    existing_relations: set[str] | None = None,
    existing_functions: set[str] | None = None,
) -> list[str]:
    """Per-table privileges. Runs as the table owner (`aicc_migrator`).

    Idempotent and order-independent by construction — it starts from a clean
    `REVOKE ALL`, so re-running it after a role has been widened by hand puts
    the database back on the declared matrix instead of layering on top of it.

    ``existing_relations`` / ``existing_functions`` (names, not signatures)
    restrict the GRANT statements to objects that exist. ``None`` — the pure
    default every render test uses — renders the full matrix. The filter
    exists for a database standing at an intermediate migration version
    (downgrade tests, partial upgrades): granting on a not-yet-created table
    raises, yet the matrix must still describe the whole schema. Skipping is
    safe against drift because absence of a declared grant on a LIVE object
    is exactly what tests/db/test_grant_compliance.py (#321) turns red.
    """
    _require_identifier(schema)
    statements: list[str] = []

    def _relation_exists(name: str) -> bool:
        return existing_relations is None or name in existing_relations

    def _function_exists(signature: str) -> bool:
        return (
            existing_functions is None
            or signature.partition("(")[0] in existing_functions
        )

    # Re-stripping PUBLIC here rather than only at bootstrap is what makes this
    # cover objects created by *later* migrations: at bootstrap the schema is
    # empty, so `ALL TABLES` matches nothing.
    #
    # FUNCTIONS is the one that matters. PUBLIC gets EXECUTE on every new
    # function by default, so a migration adding a SECURITY DEFINER helper — a
    # normal way to expose a governance query — would otherwise hand
    # `aicc_worker` a route to precisely the tables this matrix excludes.
    #
    # `ALTER DEFAULT PRIVILEGES` would be the tidier mechanism, but it was
    # tested against PostgreSQL 15 and 17 and does not persist a revocation of
    # PUBLIC's built-in function default (no `pg_default_acl` row is stored and
    # new functions still come out with the default ACL), so relying on it would
    # have been a security control that silently does nothing. Re-asserting
    # after every migration — which `command_center.db upgrade` already does —
    # is verifiable, and `test_public_execute_on_new_functions_is_revoked`
    # verifies it.
    statements.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;")
    statements.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC;")
    statements.append(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC;")

    for role in _GRANTED_ROLES:
        statements.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role};")
        statements.append(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role};"
        )
        # Functions too, for the same reason and with the same consequence: the
        # claim protocol is reachable only by `EXECUTE`, so leaving stale
        # function grants in place would be leaving the protocol itself widened.
        statements.append(
            f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {role};"
        )

    for role in _GRANTED_ROLES:
        for table, privileges in sorted(PRIVILEGES[role].items()):
            _require_identifier(table)
            if not _relation_exists(table):
                continue
            columns = COLUMN_PRIVILEGES.get(role, {}).get(table, {})
            table_wide = sorted(p for p in privileges if p not in columns)
            if table_wide:
                statements.append(
                    f"GRANT {', '.join(table_wide)} ON {schema}.{table} TO {role};"
                )
            for privilege in sorted(p for p in privileges if p in columns):
                for column in columns[privilege]:
                    _require_identifier(column)
                column_list = ", ".join(columns[privilege])
                statements.append(
                    f"GRANT {privilege} ({column_list}) ON {schema}.{table} TO {role};"
                )

        for view, privileges in sorted(VIEW_PRIVILEGES.get(role, {}).items()):
            _require_identifier(view)
            if not _relation_exists(view):
                continue
            statements.append(
                f"GRANT {', '.join(sorted(privileges))} ON {schema}.{view} TO {role};"
            )

        for signature in FUNCTION_PRIVILEGES.get(role, ()):
            _require_function_signature(signature)
            if not _function_exists(signature):
                continue
            statements.append(
                f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {role};"
            )

    # Identity columns draw from a sequence; INSERT alone is not enough. Granted
    # per sequence rather than schema-wide, so a role still cannot advance the
    # counters of tables it may not write.
    for role in _GRANTED_ROLES:
        for table, sequence in sorted(IDENTITY_SEQUENCES.items()):
            if "INSERT" not in PRIVILEGES[role].get(table, frozenset()):
                continue
            if not _relation_exists(table):
                continue
            _require_identifier(sequence)
            statements.append(f"GRANT USAGE ON SEQUENCE {schema}.{sequence} TO {role};")

    return statements


def render_worker_host_role(role: str) -> list[str]:
    """One LOGIN role per execution host, inheriting `aicc_worker`.

    The claim protocol's identity is the PostgreSQL role the server itself
    authenticated, so the fleet must not share one: a single `aicc_worker`
    password makes `work_attempt.claimed_by_role` uninformative and turns the
    compromise of any execution host into the compromise of all of them.

    No new identity machinery is needed for this — role membership already
    carries the grants, and revoking a host is `ALTER ROLE ... NOLOGIN`. No
    password is rendered here, for the reason `render_role_creation()` gives.
    """
    _require_identifier(role)
    return [
        "DO $$\n"
        "BEGIN\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"        CREATE ROLE {role} LOGIN IN ROLE {WORKER_ROLE};\n"
        "    END IF;\n"
        "END\n"
        "$$;"
    ]


def render_grants(schema: str = "public") -> list[str]:
    """Bootstrap plus table grants, in the order they must be applied."""
    return render_bootstrap(schema) + render_table_grants(schema)


def apply_bootstrap(conn, schema: str = "public") -> int:
    """Execute `render_bootstrap()`. Requires a superuser or the schema owner."""
    return _execute(conn, render_bootstrap(schema))


def apply_table_grants(conn, schema: str = "public") -> int:
    """Execute `render_table_grants()`. Requires ownership of the tables.

    Run after every migration: this is what stops a newly created table from
    shipping with no grants (invisible to the app) or with inherited ones.
    Grants are restricted to the objects the catalog actually holds, so a
    database standing at an intermediate version (a downgrade test, a partial
    upgrade) re-asserts the matrix for what exists instead of erroring on
    what does not; the #321 compliance checker is the guard against a LIVE
    object missing its declared grant.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = %s "
            "AND c.relkind IN ('r', 'v')",
            (schema,),
        )
        relations = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname = %s",
            (schema,),
        )
        functions = {row[0] for row in cur.fetchall()}
    return _execute(
        conn,
        render_table_grants(
            schema, existing_relations=relations, existing_functions=functions
        ),
    )


def _execute(conn, statements: list[str]) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
    return len(statements)


def _require_function_signature(signature: str) -> None:
    """Guard `name(type, type)` the same way a bare identifier is guarded.

    A function grant interpolates more than a name, so the identifier check is
    not enough on its own: the argument list is part of the statement and would
    otherwise be an unguarded string.
    """
    name, _, rest = signature.partition("(")
    if not rest.endswith(")"):
        raise ValueError(f"{signature!r} is not a function signature.")
    _require_identifier(name)
    for argument in (a.strip() for a in rest[:-1].split(",")):
        if not argument:
            continue
        _require_identifier(argument)


def _require_identifier(name: str) -> None:
    """Guard the string interpolation above against anything but a plain name."""
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"{name!r} is not a safe unquoted SQL identifier.")
