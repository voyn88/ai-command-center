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

Migration 0036 installs operator-issued CTO assignments, disabled. Migration 0037 limits what activation (`cto_activate`) affects. After activation, work needs an exact assignment only when it belongs to an enrolled task. A task is enrolled when `delivery_team_stage` has any row for it, at any revision. This is the same rule the 0035 implementation gate uses, and there is no separate list.

A requested payload is in scope when any of these identities, with whitespace trimmed, is enrolled:

- the canonical queue `task_id`;
- the payload's `backlog_task_id`;
- the payload's `delivery_team.task_id`.

Role work items have a NULL queue task_id, and their `delivery_team.task_id` places them in scope. A queued work item is also in scope while it is bound to an assignment (`cto_work_binding`). If a bound item's payload is later changed, it does not fall back to the legacy path.

In-scope work gets exactly the 0036 checks, at enqueue, `backlog_dispatch`, claim and `cto_authorize_start`. Each of these is refused:

- a missing assignment;
- a stale revision;
- a digest or binding mismatch;
- a revoked assignment;
- an ambiguous identity;
- a foreign or inactive principal;
- a binding conflict.

A conflicting or padded identity that names an enrolled task is in scope, and is then refused by those checks.

All other work keeps its pre-activation path unchanged, even while enforcement is active. This covers:

- ordinary independent PR reviews and their `:retry:N` items;
- legacy implementation work for tasks that were never enrolled;
- taskless jobs and the coordinator, unless its payload names an enrolled task.

The worker's principal-state check at start is likewise applied only to in-scope work. The independent review of an enrolled task's PR carries that task's id, so it is in scope and needs its own assignment.

Claims serve the higher-priority of two candidates: the authorized in-scope item and the first ready out-of-scope item. Ready, unassigned in-scope items are skipped without spending attempts. Each claim filters such items when they sit ahead of out-of-scope work. Enrollment bounds how many there are, but keep that prefix small: requeue or retire stale enrolled items rather than leaving them ready.

The operator can switch enforcement off with `cto_deactivate(reason)`. It has the same boundary as `cto_activate`: only a non-superuser `aicc_operator` session, and app/worker roles cannot execute it. Every activation and deactivation appends an immutable row (action, reference/reason, actor, time) to `cto_control_event`. Deactivation keeps all assignments, bindings, start decisions and audit rows. `cto_activate` re-enables enforcement later, with the same snapshot of the attempts already running.

Rollback does not bypass enforcement. The 0037 and 0036 down migrations both refuse while enforcement is active. When it is inactive, 0037.down restores the exact 0036 definitions and drops `cto_control_event` with its history, so back up first. A database can take 0037 through the ordinary upgrade only once it is at 36. The guarded 34→36 CTO crossing (`ops.aicc_cto_migration`) accepts a source lineage of exactly 1..36.

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
