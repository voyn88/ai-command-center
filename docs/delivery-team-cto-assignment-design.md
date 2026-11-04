# Design: automatic CTO corrective assignment, due dates, risk-based roles

Status: design accepted 2026-11-04 by the delivery-team lead (owner delegated decisions). Implemented in migration 0036; see docs/delivery-team.md.
Upstream main 945cb9e still has migration slots 0035/0036 free. Implement as a separate `0036_delivery_team_cto_policy`
so reviewed 0035 bytes stay unchanged. No local PostgreSQL CLI; DB tests need Docker (29.9.2 available) and `AICC_TEST_PG_ADMIN_DSN`.

## 1. Automatic assignment = SQL CTO policy, never model authority
- Auditor RCA `actions` become proposals. In `delivery_team_ingest` (CREATE OR REPLACE, copy of 0035 body), for stage `auditor`,
  verdict `pass` and validation reason `unassigned_corrective_action`, call
  `_delivery_team_cto_assign(task, rev, result, work_item_id)` inside a BEGIN/EXCEPTION subtransaction. Then validate again.
  If revalidation fails, raise a custom SQLSTATE so the inserted assignments roll back, and keep the reason.
- Policy (all-or-nothing, fail closed, otherwise operator confirmation is required as before):
  - parent priority not `P0`;
  - every action's task is a canonical `backlog_task_remediation` child of the parent; the incident is in `_delivery_team_incidents`;
  - owner is in a closed role set: ba, business_technologist, architect, qa, security, ux, developer, independent_reviewer, devops, integrator, cto.
    The auditor is excluded to keep it independent;
  - an existing row for (parent, incident, task) must be equal, otherwise refuse with `assignment_conflict`. An operator row is never overridden.
- New nullable columns on `delivery_team_corrective_assignment`: `due_at timestamptz`, `source_work_item_id text`.
  For policy rows: recorded_by `policy:delivery-team-cto/v1`, reference `journal://delivery-team/<work_item_id>`.
- Digest pitfall: auditor (ordinal 11) context includes assignments. Policy rows created from the auditor result would make the
  just-passed auditor stage stale immediately. Redefine `_delivery_team_context` so ordinal 11 includes only rows with
  `source_work_item_id IS NULL`, while ordinal 12 includes all rows.
- `cto_rca` keeps requiring an assignment plus an independent effectiveness check (unchanged).

## 2. Due dates
- Table `delivery_team_due_policy(priority PK: 'P0'..'P9' | 'default', sla interval > 0)`, seeded with P0 1d, P1 2d, P2 5d, default 10d.
  Add it to roles.py (app read, worker none) and to the mirror exclusions.
- A BEFORE INSERT trigger on the assignment table sets `due_at` when it is NULL, for operator and policy rows alike.
- Owner API `read_team` adds per-task `corrections` (task, owner, action, due_at, source policy/operator, closed, overdue).
  The UI shows overdue items.

## 3. Risk-based optional roles (security, ux)
- An architect result may carry an optional `role_applicability: {security|ux: {required: bool, reason: nonempty}}`.
  Rename 0035 `_delivery_team_validate` to `_v1`, then add a wrapper that validates this key and calls `_v1` with the key stripped.
  Python `parse_role_result` must accept the key for architect only.
- `_delivery_team_role_waived(task, rev, stage)`: stage in (security, ux), priority not P0/P1, current passed architect evidence says
  `required:false`, AND the stage status is `pending` (a blocking result that already ran still stands).
- Redefine `_delivery_team_eligible` (waived stage is not dispatched; a waived pending earlier stage counts as satisfied) and
  `delivery_team_ready` (passed OR waived). Add a public SECURITY DEFINER `delivery_team_stage_waived` for the app read model.
- Down migration restores the exact 0035 bodies.

## Tests to add (tests/db/test_delivery_team.py)
Auto-assign success, P0 refusal, non-role owner refusal, conflict with operator row, rollback on failed revalidation,
auditor not made stale, due_at from policy, overdue in API, waiver for P2, no waiver for P1, blocked security not waived,
invalid role_applicability rejected.
