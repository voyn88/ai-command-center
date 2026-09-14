# AICC autonomy P00 backlog cleanup

This package is the executable backlog layer to import before starting an unattended autonomous backlog run.
It intentionally keeps history in the master roadmap, but makes the active autonomous top layer small, bounded,
and machine-routable.

- **VOYN-AICC-AUTO-SUPERVISOR** | Wave 0 | OPEN | **P0** | AICC | `backlog-supervisor-p00` | Build a backlog-supervisor tick that chooses the next autonomous task by priority, dependency readiness, repo route, current queue pressure, and recent failure history, then records why it did or did not dispatch work. Target repo: `ai-command-center`
  Definition of Done: a scheduled supervisor tick can explain the chosen next backlog item, skip unsafe or blocked rows with durable evidence, and dispatch only one bounded slice per repo/window; tests cover dependency order, repo route, queue pressure, and no-work cases.
  Forbidden scope: no bypass of existing review, acceptance, merge, or deployment gates; no direct database writes outside existing store APIs.

- **VOYN-AICC-AUTO-FAILURE-CLASSIFIER** | Wave 0 | OPEN | **P0** | AICC | `failure-classifier-p00` | Classify failed checks and runtime failures into flaky/transient, infrastructure, semantic code defect, policy blocker, and needs-human-decision buckets, feeding remediation and supervisor decisions. Target repo: `ai-command-center`
  Definition of Done: representative GitHub check failures, worker failures, merge conflicts, stale acceptance, and policy blocks produce stable categories and recommended next actions; flaky rerun cases do not spawn code-fix tasks and real defects do.
  Forbidden scope: no opaque LLM-only classification without persisted evidence; no automatic approval or merge decision from the classifier alone.

- **VOYN-AICC-AUTO-SMOKE-ROLLBACK** | Wave 0 | OPEN | **P0** | AICC | `post-merge-smoke-rollback-p00` | After a merge/deploy, run a bounded production smoke and either mark the change healthy or stop the autonomous lane and open a rollback/remediation task with evidence. Target repo: `ai-command-center`
  Depends on: VOYN-AICC-AUTO-SUPERVISOR, VOYN-AICC-AUTO-FAILURE-CLASSIFIER.
  Definition of Done: a merged PR produces a post-merge smoke record tied to the merge SHA; green smoke keeps autonomy running, red smoke creates an actionable rollback/remediation task and prevents further risky dispatch until reconciled.
  Forbidden scope: no destructive rollback without an existing verified previous release target; no suppression of safety gates or audit logs.

- **VOYN-AICC-AUTO-SPLIT** | Wave 0 | OPEN | **P0** | AICC | `auto-split-backlog-p00` | Detect oversized or ambiguous backlog items before dispatch, split them into ordered executable slices, and park the parent until its children close. Target repo: `ai-command-center`
  Depends on: VOYN-AICC-AUTO-SUPERVISOR.
  Definition of Done: a too-large backlog row is converted into child rows with dependencies, repo route, acceptance criteria, and parent lineage; planner dispatches children instead of the parent; tests cover idempotent repeated split attempts.
  Forbidden scope: no silent semantic rewriting of user intent; ambiguous product decisions must be parked as needs-human-decision, not guessed.

- **VOYN-AICC-AUTO-BUDGETS** | Wave 0 | OPEN | **P0** | AICC | `autonomy-budgets-p00` | Add per-window limits for autonomous PR creation, reruns, remediation attempts, CPU/GitHub usage, and consecutive failures, with visible stop reasons and safe resume controls. Target repo: `ai-command-center`
  Depends on: VOYN-AICC-AUTO-SUPERVISOR, VOYN-AICC-AUTO-FAILURE-CLASSIFIER.
  Definition of Done: the autonomous loop refuses to exceed configured PR/rerun/remediation/failure budgets, records the refusal reason, and exposes a resume path; tests cover budget exhaustion, reset windows, and per-repo isolation.
  Forbidden scope: no global unlimited autonomous mode; no hidden defaults that can create unbounded PRs or reruns.

- **VOYN-AICC-APP-COMMAND-SURFACE** | Wave 0 | OPEN | **P1** | AICC | `aicc-app-command-surface-h1` | Build the AICC application into the primary desktop and mobile command surface: all backlog, PR, merge, deploy, agent, runtime health, evidence, permissions, and audit data visible and controllable from the app. Target repo: `ai-command-center`
  Depends on: VOYN-AICC-AUTO-SUPERVISOR, VOYN-AICC-AUTO-FAILURE-CLASSIFIER, VOYN-AICC-AUTO-SMOKE-ROLLBACK, VOYN-AICC-AUTO-SPLIT, VOYN-AICC-AUTO-BUDGETS.
  Definition of Done: desktop and mobile surfaces show the same canonical operational state; an operator can inspect backlog, queue, PRs, merges, deploys, worker health, evidence, and stop/resume controls without using GitHub or shell for routine operation; browser/mobile/desktop acceptance tests cover the core workflows.
  Forbidden scope: no direct privileged GitHub, database, SSH, or systemd access from clients; all controls go through authenticated, audited AICC/AIOS APIs.
