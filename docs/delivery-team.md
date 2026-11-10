# Native delivery team

The delivery team is an opt-in stage ledger over canonical PostgreSQL backlog tasks and the existing work queue. It is not a second queue, daemon, publisher or deployment controller. Enable with `AICC_DELIVERY_TEAM_ENABLED=1` only after migration and compatible workers are installed. `AICC_DELIVERY_TEAM_STOP=1` stops new assignments and holds implementation readiness; it does not bypass gates. Disabled mode preserves the legacy planner path for tasks that have never been enrolled. Enrollment gates remain enforced by PostgreSQL even if the process flag is later removed.

For the first pilot, set `AICC_DELIVERY_TEAM_TASK_IDS` to a JSON array of explicit canonical task IDs. This scopes enrollment and new role assignments; it does not revoke existing gates or discard in-flight results. Invalid or empty scope fails closed. Removing the scope permits bounded enrollment of the whole canonical backlog, so do that only after the system-level product review and pilot acceptance.

The existing planner calls `delivery_team.tick(factory, limit=4)` while holding its `planner:global` lease. Each tick synchronizes a bounded batch of tasks, ingests existing results and dispatches a bounded batch. SQL owns task/revision prerequisites and idempotent queue admission. Role work items must have a NULL canonical queue task_id; their linkage lives in the stage ledger so legacy implementation-result ingestion cannot consume a role result. Existing implementation work must not be redispatched.

Roles are BA, business technologist, architect, QA plan, security, UX, CTO, independent reviewer, DevOps, production QA, auditor and CTO corrective-action verification. A roster or assignment is not a completed review. BA traces the actual expected business result and measurable criteria to the source request. Missing business decisions block the task. The technologist checks real operational workflows and exceptions. CTO resolves dependencies and blockers; it cannot replace missing proof with consensus. Scope changes require an explicit recorded decision.

Each role runs the existing read-only `review` profile. Explicit subscriptions-only routing names Codex and Claude; it does not enable a paid provider fallback. The role cannot push, deploy or acquire a new production writer. The implementation and publication chain remains the established planner, isolated worker, guarded publisher, independent review and merge flow.

## Contracts and authority

Payload metadata `delivery_team` has exactly task_id, revision, stage, input_digest. Results use schema `delivery-team-review/v1` and those exact bindings, plus verdict, criteria, evidence, blockers and summary. A verdict is pass, blocked or changes_requested. Evidence links a criterion to a reference, SHA and verification claim; production evidence additionally names the production environment. The worker parses the complete bounded JSON result before tail truncation; SQL validates it again. Stale or foreign results must not advance the stage. Retries are bounded by database stage policy and the existing queue attempt budget.

A model's `verified: true` is only a review claim. It cannot create a trusted deployment receipt or acceptance check. Production acceptance requires both an operator-recorded release receipt and distinct operator-admitted `acceptance_checks` covering the BA criteria, bound to the current task revision and exact release SHA. The production QA result must reference the admitted checks; repeating a deployment observation does not establish business acceptance. Legacy backlog DONE retains its implementation/merge meaning, while delivery acceptance is derived separately from the ledger.

The bounded `deployment.delivery_receipts` collector measures configured same-origin HTTPS version/health endpoints and persists a private observation artifact. Before recording it, the collector reads the configured HTTPS artifact URL back and requires identical bytes. The authenticated `/api/team/artifacts/{sha256}.json` route serves only bounded, hash-matching release, acceptance and corrective observations with exact allowlisted schemas from the optional operator-controlled `AICC_TEAM_ARTIFACT_DIR`; it rejects symlinks and nonregular files without changing permissions. The serving account and authorized reviewers need configured access. Only the separate operator authority may obtain the current release scope or record receipts and checks; app and worker principals cannot issue them. Re-observing the same task/revision/SHA/environment preserves the first receipt and its QA digest. A file left by a refused database recording remains an observation artifact, not an admitted release receipt.

The authenticated owner endpoint `/api/version` reports an immutable startup snapshot. The owner module's physical path is captured at import; application creation validates `releases/<40-character SHA>/command_center/webapi/owner_app.py` against the existing self-deployer's `.aicc-release-sha` marker. Later changes to the `current` symlink do not change the running application's reported SHA. A mutable checkout, missing or inconsistent marker, or unsupported path returns HTTP 503 with an unknown SHA. Environment variables and the current Git HEAD are not substituted. This trusts the existing immutable-release deployment contract; it is not a cryptographic attestation against an operator capable of changing release files.

`record_acceptance_artifact` admits an independently produced `delivery-acceptance-observation/v1` artifact. The trusted operator selects its producer, expected check ID and checksum. The reader fetches the actual bounded HTTPS bytes, rejects redirects, checks SHA256, rejects duplicate JSON fields, and validates task, revision, production environment, exact SHA, criterion, check ID, passed outcome and aware observation timestamp. SQL rechecks authoritative release scope and freshness. The configured private reader pins credentials to the selected origin. A model-supplied URL, checksum or claim is not a trusted producer. The resulting HTTPS reference includes the verified hash; retained artifacts must be available to authorized reviewers on other hosts. These adapters admit existing evidence; they neither run the check nor manufacture its result.

## CTO assignment enforcement scope (migration 0037)

Migration 0036 installs operator-issued CTO assignments, disabled. Migration 0037 limits what activation (`cto_activate`) affects, and adds an off switch.

**Scope is explicit and operator-controlled.** `cto_scope` lists the tasks under enforcement. Only `cto_scope_add(task_id, reference)` and `cto_scope_remove(task_id, reason)` write it, and both are restricted to `aicc_operator`, like `cto_activate`.

- `cto_scope_add` accepts only a task that is already enrolled in the 0035 stage ledger, so scope is always a subset of enrollment.
- Enrollment growth does not extend enforcement. Enrollment can only grow, and an unset `AICC_DELIVERY_TEAM_TASK_IDS` enrolls the whole backlog. Neither of these adds anything to the scope.
- Trade-off: the operator must add and remove each pilot task explicitly.

**What is in scope**, with identities compared after trimming outer whitespace and folding case:

- Work whose canonical queue `task_id`, payload `backlog_task_id` or payload `delivery_team.task_id` names a task in `cto_scope`. A conflicting, padded or case-variant identity is therefore in scope, and then fails 0036's exact checks. Role items have a NULL queue task_id; their `delivery_team.task_id` places them in scope.
- Task-less `agent_run`/`simple_task` work that can write. "Can write" means anything except work whose task type and every cascade entry's task type are all in the worker's read-only or model-only sets (`review`, `final_gate`, `architecture_review`, `verification_review`, `independent_review`). Such work carries no task and so cannot be assigned. While enforcement is on it is refused, as 0036 did. Task-less read-only work, such as the authenticated audit route, cannot change any task and stays out of scope.
- Any work item already bound to an assignment.

**What in-scope work must pass.** It needs exactly the 0036 checks at enqueue, `backlog_dispatch`, claim and start. Missing assignments, stale revisions, digest or binding mismatches, revoked or ambiguous assignments, foreign or inactive principals and binding conflicts are all refused.

**Repository rule.** The binding names the canonical repository: `backlog_task.repo`, which is the planner route-table key and the repository the PR URL names.

- `backlog_dispatch` and `delivery_team_dispatch` already pass it.
- The `review_merge` writer now passes the repository its PR URL names for every review, chunk, `:retry:N` and verification item.
- In-scope work with `repository_id` NULL is refused (`repository_unresolved`). It is never matched to a task's repository.

**Reviews of a scoped task need assignments.** Each review chunk, retry and verification is a distinct exact payload, so each needs its own exact-digest assignment. Those payloads are generated by the review tick from the PR diff, so an operator cannot know them in advance. Until a reviewed design exists for review-class work of scoped tasks, keep a task in scope only for the stages that are assigned. Use `cto_scope_remove` (audited) before relying on the automatic independent review of its PR.

**Out-of-scope work.** It keeps its pre-activation admission. This covers ordinary PR reviews and retries, legacy implementation, the coordinator, and read-only task-less jobs. Two 0036 details still apply to it while the latch is on:

- the claim token must be 64 lowercase hex;
- an inactive worker principal is not checked at start for out-of-scope work. Before activation there was no such check either.

The control protects against honest producers that lack an assignment. It does not protect against a compromised `aicc_app`, which can label work with an identity outside the scope.

**Invariant while active:** every ready or claimed in-scope item is bound.

- `cto_activate` refuses (`unbound_scoped_work`) while unbound in-scope work is ready or claimed. Scoped work queued while enforcement was off is neither grandfathered nor left silently unclaimable: drain it, fail it, or re-admit it after assignment, then activate.
- `cto_scope_add` refuses the same way while active.
- `queue_redrive`, the only dead→ready path, refuses to revive unbound in-scope work.

**Claim cost.** Because of the invariant, the claim selects out-of-scope work in claimable-index order with one binding-key probe per row, and stops at the first unbound row. The cost grows with the bound-but-not-claimable rows ahead (live or revoked operator assignments), not with the queue size. Revoked bound items stay `ready` forever, as in 0036, so retire them.

**Off switch.** `cto_deactivate(reason)` has activation's boundary.

- Every activation, deactivation and scope change appends an immutable `cto_control_event` row (action, task, reference or reason, `session_user`, time). The actor is always the operator role, so the reference carries the human or ticket.
- Append-only is enforced by triggers and grants. The table owner or a superuser can still disable triggers.
- Deactivation keeps assignments, bindings, start decisions, scope and audit rows.

**Side effects of deactivating and then reactivating:**

- Scoped work admitted while enforcement is off makes reactivation refuse until it is drained.
- Attempts claimed and active at reactivation are snapshotted as legacy drain.
- A `backlog_dispatch` of a bound task while enforcement is off bumps the revision without moving the binding, so after reactivation that item is `stale_revision` and needs a new key and assignment.
- Work enqueued with an assigned payload while enforcement is off is not bound.

**Rollback does not bypass enforcement.**

- The 0036 and 0037 down migrations both refuse while enforcement is active.
- 0037.down also refuses once `cto_control_event` or `cto_scope` holds anything, because dropping them would destroy the switch audit. Retain them with a reviewed forward migration instead.
- A never-used 37 downgrades to the exact 0036 definitions.

**Release order.** A database takes 0037 through the ordinary upgrade only from 36. The guarded 34→36 crossing (`ops.aicc_cto_migration`) accepts a source lineage of exactly 1..36. Release #1110 first, with enforcement off, and apply 0037 before any `cto_activate`.

## Incident and corrective-action evidence

RCA is a branch associated with an incident, not a recursively generated REM chain. External `incident_key` binds the exact project, run, SHA and failure signature. The existing review-rejection/merge-remediation transaction records observations for enrolled tasks when the feature is enabled. Replays retain the first admitted observation rather than replacing its timestamp or evidence. Incident records survive task status revisions.

Synthetic incidents from blocked, rejected, malformed or dead role work are derived from durable append-only delivery evidence, including its recorded revision and work identity. They do not disappear when the current stage is reset or replaced. Auditor and CTO-RCA failures do not recursively become new incidents in this branch. This synthetic failure evidence is distinct from an external CI or deployment observation.

An auditor distinguishes observations from hypotheses, identifies the missed control, and refers to existing canonical corrective tasks. `delivery_team_assign_correction` admits an operator-confirmed assignment linking parent task, incident, corrective task, owner, action and effectiveness criterion. It verifies the canonical remediation relationship and preserves the assignment. The current implementation does not let a CTO model automatically create that authoritative assignment.

`record_corrective_artifact` verifies the actual HTTPS bytes and hash of a separately produced `delivery-corrective-observation/v1` effectiveness check. Its expected scope includes parent task/revision, incident, corrective task/revision, correction SHA, effectiveness criterion and check ID. SQL requires the current parent revision, the admitted assignment, a completed canonical corrective task at the matching revision/SHA, and an observation no earlier than the assignment. CTO closure must retain the audited owner/action bindings and reference the independently admitted check. A DONE state or green CI link alone is insufficient evidence that the process correction was effective. Production acceptance and corrective-process closure remain distinct results.

## Operational gaps and rollout prerequisites

The synchronous `ops.aicc_delivery_team_operator` CLI invokes the existing adapters with explicit operator credentials, scope and expected SHA. It is not yet an automatic release hook; see `docs/delivery-team-operator.md`.

This code does not yet establish a running autonomous delivery team. Remaining gaps include:

- No caller or retained-artifact storage has been configured in an existing release chain for these receipt/check adapters. The local artifact endpoint and mandatory HTTPS readback are implemented, but shared access, retention and producer trust still need operational integration.
- Bounded CI-attempt and retained deployment-journal collectors are implemented in `delivery_failure_collector`; they still require an existing trusted observation caller and retained source mapping. Receiving relevant incidents and closing RCA has not been demonstrated in a real pilot.
- Release evidence requires the exact authoritative postmerge SHA. There is no implementation-SHA to later batch-release mapping; an included ancestor is not silently accepted as the deployed SHA.
- Corrective assignments require operator confirmation. Automatic authoritative CTO assignment, due dates and risk-based omission of optional roles are not implemented. The present prerequisites must not be described as a completed risk-adaptive workflow.
- A full real task through the team, production release, independent acceptance and corrective effectiveness has not been performed. Local tests or a roster do not prove operational acceptance.

The existing repository integrator owns rollout. Before enabling a scoped pilot:

1. Reconcile the exact candidate, migration slot and current main with that integrator; complete independent review and mandatory checks on the final candidate. Preserve the existing queue, writer and deployment mechanism.
2. Install the compatible migration, worker, planner and owner app through the existing authorized release chain. Verify privileges, actual release SHA and health; a 503 version response is unknown provenance, not a healthy release receipt.
3. Configure the trusted operator caller, same-origin authenticated observation targets, independent check producers and retained artifact access. Demonstrate that app/worker credentials cannot issue receipts, assignments or acceptance checks, and that refusal rolls back the caller's transaction.
4. Select explicit canonical pilot IDs and record their original business criteria, owners and relevant external/human acceptance. Validate stop, restart/replay, stale-result rejection and applicable recovery in isolated environments before relying on them operationally. Read-only reviewers cannot perform destructive production recovery tests.
5. Enable only the scoped pilot under the existing planner lease. Follow one real useful change through exact-source review, mandatory gates, actual deployment observation and independently evidenced acceptance. Exercise incident/correction handling with retained evidence; report any untested path explicitly.
6. Expand only after pilot acceptance and reconciliation of the product-level backlog. Preserve established project soak, recovery and external acceptance gates. Stopping assignment or removing the process enable flag does not remove the SQL gates of already enrolled tasks; do not treat flag removal as a bypass or rollback of enrollment.

## Validation

Targeted Python tests cover strict provenance, malformed/duplicate JSON, blocked verdicts, role mutability/provider boundaries, disabled and stopped behavior, evidence requirements and incident identity. PostgreSQL tests must additionally prove concurrency, idempotent ingestion, restart/replay, stale revision invalidation, bounded retries, grants and trusted receipt enforcement. Test the existing implementation route as a regression and one real bounded end-to-end task before backfilling the full backlog. Never label primary routing or source inspection as completed specialist acceptance.
