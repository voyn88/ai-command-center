# VOYN CRM autonomous backlog lane

This package imports the existing CRM backlog as a separate executable lane for the autonomous runner.
The lane is deliberately routed to `voyn-logistics-crm` so CRM work can run in parallel with AICC autonomy
work while keeping repository locks, review windows, budgets, and acceptance evidence isolated.

Source backlog: `/Users/dmitrijcernikov/Documents/Codex/2026-09-13/voyn-ux-u4-audit-filter-labels/docs/VOYN_CRM_CANONICAL_BACKLOG.md`

- **VOYN-CRM-AUTO-LANE** | Wave 0 | OPEN | **P0** | CRM | `crm-autonomous-lane` | Configure the CRM backlog lane so the supervisor can select CRM work independently from AICC work, with repo-specific budget, review, merge, smoke, and stop controls. Target repo: `voyn-logistics-crm`
  Definition of Done: CRM rows import with repo `voyn-logistics-crm`; the supervisor can dispatch at most one bounded CRM slice per window; AICC and CRM queues do not block each other except through global safety budgets; stop reasons name the affected repo and task.
  Forbidden scope: no shared dirty worktree, no direct production deployment without CRM-specific smoke and rollback gates, no bypass of existing acceptance evidence.

- **VOYN-CRM-P0-SHELL** | Wave 0 | OPEN | **P0** | CRM | `crm-role-shell-navigation` | Build the CRM app shell and role navigation around the canonical workflow: dashboard, role menu, breadcrumbs, global search, saved filters, and return-to-list behavior. Target repo: `voyn-logistics-crm`
  Definition of Done: each role sees only allowed sections; direct URL access is server-gated; dashboard counters drill into filtered registries; desktop and mobile acceptance covers navigation, search, filters, and no dead-end routes.
  Source item: P0.1 from the canonical CRM backlog.

- **VOYN-CRM-P0-CARD-DIRECTORIES** | Wave 0 | OPEN | **P0** | CRM | `crm-shipment-card-directories` | Implement the canonical shipment/request card and core directories: customer, carrier, vehicle, driver, cargo, addresses, contract, rate, VAT, and time windows. Target repo: `voyn-logistics-crm`
  Definition of Done: a request cannot be confirmed without required canonical fields; unavailable or non-compliant transport cannot be assigned; changes are audited; browser acceptance covers create, edit, validation, and role visibility.
  Source item: P0.2 from the canonical CRM backlog.

- **VOYN-CRM-P0-DISPATCH-LIFECYCLE** | Wave 0 | OPEN | **P0** | CRM | `crm-dispatch-lifecycle` | Build the dispatch board and full shipment lifecycle: table, kanban/calendar views, statuses, multimodal legs, plan/fact timestamps, ETA, delays, and personal queues. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-SHELL, VOYN-CRM-P0-CARD-DIRECTORIES.
  Definition of Done: a dispatcher can find unassigned work, late shipments, and their own queue in two clicks; status transitions are validated; multimodal legs remain linked to the parent request; acceptance covers desktop and mobile.
  Source item: P0.3 from the canonical CRM backlog.

- **VOYN-CRM-P0-DOCUMENTS** | Wave 0 | OPEN | **P0** | CRM | `crm-document-completeness` | Build the document registry and completeness rules: required document types, deadlines, versions, request/send flows, and close blockers. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-DISPATCH-LIFECYCLE.
  Definition of Done: missing UPD/TN/TTN/act documents create tasks; configurable close rules block only when intended; document download/export respects server-side permissions; acceptance covers document lifecycle and role access.
  Source item: P0.4 from the canonical CRM backlog.

- **VOYN-CRM-P0-FINANCE-CARD** | Wave 0 | OPEN | **P0** | CRM | `crm-finance-card` | Build the shipment finance card: multiple revenue and cost lines, VAT components, currency, corrections, plan/fact values, cost of money, and object-linked primary rows. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-DISPATCH-LIFECYCLE.
  Definition of Done: P&L by shipment, customer, lane, and carrier is reproducible from primary financial rows; every financial fact has `legal_entity_id`; sensitive finance views are role-gated; CSV totals match screen totals.
  Source item: P0.5 from the canonical CRM backlog.

- **VOYN-CRM-P0-EXCEPTIONS-SLA** | Wave 0 | OPEN | **P0** | CRM | `crm-exceptions-sla` | Build exceptions and SLA queues with owner, deadline, severity, cause, resolution, traceability, and Dmitry's private read-only expense-deviation queue. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-SHELL, VOYN-CRM-P0-DISPATCH-LIFECYCLE, VOYN-CRM-P0-FINANCE-CARD.
  Definition of Done: every exception has audit history and an owner; SLA breaches are visible from operations; Dmitry's private queue is isolated by server checks and has no approve/reject actions; acceptance covers direct URL leakage attempts.
  Source item: P0.6 from the canonical CRM backlog.

- **VOYN-CRM-P0-HISTORICAL-IMPORT** | Wave 0 | OPEN | **P0** | CRM | `crm-historical-import-staging` | Build historical import through preview, mapping, staging, approved run, idempotency, error report, and rollback plan. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-FINANCE-CARD.
  Definition of Done: files never write directly to production tables; repeated imports do not create duplicates; validation errors are reviewable before commit; rollback plan is recorded with each accepted import batch.
  Source item: P0.7 from the canonical CRM backlog.

- **VOYN-CRM-P1-FINANCE-INTEGRATIONS** | Wave 0 | OPEN | **P1** | CRM | `crm-finance-integrations` | Implement the next finance and integration layer: payment calendar, AR/AP ageing, bank import reconciliation, integration platform, and 1C/EDO/GPS/fuel/mail adapters. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-FINANCE-CARD, VOYN-CRM-P0-HISTORICAL-IMPORT.
  Definition of Done: payment totals match screen and CSV by legal entity; bank uploads are immutable and idempotent; ambiguous reconciliation never mutates cashflow; adapters have sandbox proof, least-privilege credentials, monitoring, and disabled-by-default behavior without configuration.
  Source items: P1.8 through P1.11 from the canonical CRM backlog.

- **VOYN-CRM-P1-TENDERS** | Wave 0 | OPEN | **P1** | CRM | `crm-tender-workspace` | Build the tender workspace with assumptions, price, taxes, cost of money, document package, approvals, and manual submission flow. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-FINANCE-CARD.
  Definition of Done: tender decisions are reproducible from saved assumptions; external submission only exists behind an explicitly configured adapter; acceptance covers create, calculate, approve, package, and manual submit workflows.
  Source item: P1.12 from the canonical CRM backlog.

- **VOYN-CRM-P2-PRODUCTION-MATURITY** | Wave 0 | OPEN | **P2** | CRM | `crm-production-maturity` | Complete CRM production maturity: approvals, carrier ratings, claims, BI reports, PostgreSQL migrations, object storage, SSO/MFA, CSRF, backups, restore drill, SLO alerts, load tests, and security tests. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-P0-SHELL, VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-DISPATCH-LIFECYCLE, VOYN-CRM-P0-DOCUMENTS, VOYN-CRM-P0-FINANCE-CARD, VOYN-CRM-P0-EXCEPTIONS-SLA, VOYN-CRM-P0-HISTORICAL-IMPORT.
  Definition of Done: production readiness is proven by current migration, security, accessibility, browser, backup/restore, and operational-smoke evidence; BI reports drill down to source rows and export consistently.
  Source items: P2.13 through P2.15 from the canonical CRM backlog.

- **VOYN-CRM-FINAL-DEEP-ACCEPTANCE** | Wave 0 | OPEN | **P0** | CRM | `crm-final-deep-acceptance-remediation` | After the 11 CRM lane tasks are implemented, run a maximally deep product acceptance across architecture, code, interface, speed, usability, security, fault tolerance, and multi-user role workflows, then loop acceptance and remediation until no findings remain. Target repo: `voyn-logistics-crm`
  Depends on: VOYN-CRM-AUTO-LANE, VOYN-CRM-P0-SHELL, VOYN-CRM-P0-CARD-DIRECTORIES, VOYN-CRM-P0-DISPATCH-LIFECYCLE, VOYN-CRM-P0-DOCUMENTS, VOYN-CRM-P0-FINANCE-CARD, VOYN-CRM-P0-EXCEPTIONS-SLA, VOYN-CRM-P0-HISTORICAL-IMPORT, VOYN-CRM-P1-FINANCE-INTEGRATIONS, VOYN-CRM-P1-TENDERS, VOYN-CRM-P2-PRODUCTION-MATURITY.
  Definition of Done: acceptance emulates several users across every role; verifies role isolation, direct URL/API access control, end-to-end operational flows, finance/document correctness, desktop and mobile UX, accessibility, performance, security, resilience, backup/restore, and failure handling; produces a prioritized remediation plan; fixes every confirmed blocker/major/minor finding; repeats the full relevant acceptance loop after each remediation pass; closes only when the latest pass has zero open findings or an explicit founder-approved deferral with owner, reason, and acceptance impact.
  Forbidden scope: no superficial checklist-only acceptance; no closing while confirmed findings remain untriaged or unremediated; no claiming production readiness without current evidence tied to the exact accepted commit; no stopping after a single audit if remediation changed behavior.
