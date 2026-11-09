# Provisional CTO assignment authority

These SQL files are deliberately outside the migration loader. They target the
existing AICC backlog/queue and are not permission to apply production DDL.
The numbered migration, lineage manifest, canonical environment check and
rollout wrapper belong to the integration owner. The published old policy
0036 must not be silently overwritten or applied as a dependency.

## API

- `cto_assign(task_id, revision, principal_id, payload_json, reference)` returns
  `(ok, reason, assignment_id)`. Only an authenticated, non-superuser session
  whose `session_user` is exactly `aicc_operator` may issue assignments.
  The database binds the complete JSON payload hash, canonical task revision,
  repository, and active enrolled worker principal. Claimed actor/executor
  payload fields are not authority. After revocation an explicit new assignment
  may use the same task revision, but needs a new work identity; old bindings
  cannot silently move to it. One assignment authorizes exactly one work identity:
  a unique constraint and serialized precheck reject another queue/key with
  `binding_conflict` before inserting work. Exact queue/key/payload replay returns
  the same work ID only while its original binding remains valid, including the
  legitimate dispatch revision transition. Revoked bindings never revive.
- `cto_revoke(assignment_id, reason)` serializes with claim and start. It does
  not stop an attempt already authorized to start.
- `cto_activate(fleet_verification_reference)` enables enforcement once. The
  reference is audit metadata, not automatic proof that the fleet is compatible.
  The release owner must verify every worker supports the start gate first.
- `cto_authorize_start(attempt_id, sha256_claim_token)` returns `queue_verdict`.
  It validates authenticated claim owner, token hash, active lease, current
  fence, assignment and revision. A committed successful call is the start
  linearization point. The worker must not invoke a handler without success.

## Compatibility and scope

Schema installation starts disabled. The existing queue signatures and attempt
budget/history behavior remain; `agent_run` and `simple_task` both require an
assignment once activated because both can invoke an agent. There is no generic
service exemption or queue-name bypass. A taskless existing advisory job will
therefore need an explicit canonical task and assignment before activation;
inventing that task or authorizing a wildcard is not part of this patch.

At activation, only exact active, unexpired, currently fenced attempt IDs are
recorded for legacy drain. Ready items and later retries are not grandfathered.
Revocation after claim but before start denies execution. A committed start may
drain after later revocation, but its expired or superseded lease still fails.
Principal revocation also refuses any subsequent start call, even for a recorded
start or legacy drain. It does not asynchronously abort an already-running handler.
The enabled schema cannot be downgraded to silently remove authorization.

`backlog_dispatch` binds its own atomic revision transition from R to actual
R+1. Other task edits do not inherit permission. Raw enqueue/claim, retries and
recovery still enter the same database checks. The full JSON hash means the
operator must approve the final constructed payload; changing a prompt, model
route, timeout, repository or role payload requires a new assignment.

Every control function shares one transaction-scoped advisory lock. This
serializes authorization decisions; it is not a second planner or runtime
writer. Claim selection searches authorized candidates beyond a denied prefix,
auditing at most one refused head per call without spending attempt budgets.
Eligibility starts from an index of active assignments for the authenticated
principal, then unique assignment-to-work bindings and work primary-key lookups.
A 7,000-item denied-prefix fixture verifies those actual plan nodes and concurrent
revocation progress. This is not constant-time: sorting and probing still scale
with that principal's active eligible assignment cardinality, and the global
lock serializes decisions. The fixture is evidence, not a production latency SLA.
Combined foundation 0035 trigger/lock-order regression remains an integration
gate because this provisional suite starts from main's schema 0034.

App/worker have no DML on the five new tables or execution rights on renamed
legacy entry points. `roles.py` includes the final read/API grant matrix so
grant reassertion cannot accidentally strip the gate or expose a helper. Until
numbered migration integration, the normal schema-inventory tests cannot treat
the provisional main-only tree as a shippable release.

Denial functions return refusals and append server-side audit events. A caller
can roll back its transaction (including that event), so a redacted `RAISE LOG`
also records the decision outside the transaction. Retention of PostgreSQL
server logs is a rollout requirement. Never claim rolled-back audit table rows
persist. Function permission errors use the PostgreSQL denial/logging mechanism.

## Validation

`tests/db/test_cto_assignment.py` uses disposable databases and actual product
logins. The test harness under local `outputs/cto-enforcement` supplies a separate
PostgreSQL 16 container on an ephemeral loopback port and removes it in `finally`.
No production database or another task's container is an acceptable test target.
Runtime enforcement outside the supported SQL/worker route, including arbitrary
manual tools in another chat, is not implemented or claimed here.

## Existing SQL producers

All existing SQL callers of `queue_enqueue` were inspected:

- `backlog_dispatch` supports an exact approved payload and atomic R→R+1 binding.
  A NULL refusal rolls back its inner status/lease mutations before recording a
  durable denial; it cannot report IN_PROGRESS with a NULL work identity.
- Foundation `delivery_team_dispatch` supports its exact final role payload.
  A NULL refusal leaves the stage pending and queued count unchanged, returning
  `cto_enqueue_refused` with a durable audit event. The wrapper acquires the CTO
  lock before the foundation lock. Installation is conditional on foundation0035;
  the integrated numbered migration must have that approved predecessor.
- `coordinator_enqueue` remains unchanged while enforcement is disabled. When
  enabled, its taskless/repository-NULL advisory producer is explicitly held as
  `cto_assignment_required` before its global in-flight CAS. Canonical task/repo
  integration and operator assignment are required before this producer may run.
  A disabled-mode NULL enqueue also releases its CAS and returns a truthful error.

These producer refusals preserve committed audit rows. Explicit caller rollback
still requires the redacted server log, as described above.
