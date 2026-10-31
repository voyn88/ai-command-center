# AICC Native — Phase 0 product blueprint

## Product boundary

AIOS owns execution and proof. AICC Native is a calm, glanceable visual control center. A delivery is never shown as complete from CI alone: its evidence chain is `task → attempt → head SHA → PR → exact-SHA CI → independent acceptance → merged SHA → deployed SHA`. Missing evidence is `UNKNOWN`.

## Information architecture

| Surface | Primary question | First view | Detail |
| --- | --- | --- |
| Overview | Is owner action needed? | health, bottleneck, delivery stages, attention queue | project/task drill-down |
| Projects | Which portfolio area needs focus? | health, throughput, cost, risk, deploy | project delivery history |
| Tasks | What is true about this task? | status derived from evidence, blocker in plain language | attempts, remediation and full proof chain |
| Dialogues | What needs my answer or discussion? | conversations, decision requests and message summaries | context, participants, sources and a safe reply action |
| Decisions | What choice is needed and why now? | options, impact and deadline | decision package, alternatives and later outcome |
| Agents & infrastructure | Can the system deliver? | lanes, hosts, heartbeat, circuit-breaker | safe redacted timeline |
| Delivery pipeline | Where is work waiting? | task-to-deploy flow | gate/reviewer/remediation evidence |
| Activity | What changed and why? | filterable ordered events | correlated object detail |

Desktop uses a command palette, shortcuts and multi-select. Mobile uses an Overview, Attention, Dialogues, Tasks and Activity tab model; it does not compress desktop tables. A full backlog-derived feature inventory and release order lives in `BACKLOG_PRODUCT_COVERAGE.md`; the whole-product navigation and interaction model lives in `NAVIGATION_AND_UX_ARCHITECTURE.md`.

## Critical journeys

1. Owner opens Overview and sees whether an actual decision is required in under five seconds.
2. Owner opens an attention item and sees the evidence-backed blocker, automatic recovery status and safe next action.
3. Engineer opens a task and traverses every proof link without reading raw logs.
4. A network loss opens the last validated snapshot immediately, labels freshness, and reconnects with a resumable cursor.
5. Owner opens a dialogue or escalation, understands the question in plain language, sees the options and answers through the governed gateway; the app never sends a hidden instruction or irreversible action.

## Design foundation

- Semantic roles: `surface`, `elevatedSurface`, `primaryText`, `secondaryText`, `accent`, `success`, `warning`, `danger`, `unknown`, never bare color names.
- Density: comfortable and compact; touch targets are at least 44 pt/dp.
- Motion is explanatory, interruptible and disabled under Reduced Motion. Network work never blocks interaction.
- Light, dark and high-contrast themes are mandatory. Status is communicated by label, icon and color.
- Every platform exposes native accessibility names, headings, focus order and scalable type. VoiceOver, TalkBack and Windows screen-reader verification are acceptance work, not visual-test substitutes.

## Fixture states required before live integration

`healthy`, `attention_required`, `ci_pending`, `acceptance_sha_mismatch`, `deploy_unverified`, `offline_cached`, `event_gap_resync`, `sensitive_redacted`, `empty`, and `degraded`.

## Non-goals of Phase 0

No AIOS-core changes, no direct infrastructure access, no mutation commands, no release-signing credentials, and no claim of a production-ready application. Messages and replies are represented in fixture-first UI only until the separately accepted, auditable command and conversation contracts exist. The next executable scope is a polished fixture-first vertical slice in this worktree after independent ADR review.
