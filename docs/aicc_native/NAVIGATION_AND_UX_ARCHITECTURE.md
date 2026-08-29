# AICC Native — navigation and UX architecture

## Product promise

The application is a single calm place to understand the business, communicate with its AI team and make governed decisions. The owner should always find four things before technical detail: **what is happening, why it matters, what can be done, and what happens next**.

This is the design-time architecture for the whole product. A screen may initially use fixtures or be marked “coming soon”, but its place, language, states and navigation are fixed now so later capabilities do not become disconnected windows.

## Full desktop navigation

| Group | Sections | Purpose |
| --- | --- | --- |
| Today | Overview, Briefings | current picture, morning digest and board-ready summaries |
| Work | Tasks, Projects, Plan | work portfolio, dependencies, priorities and playbooks |
| Conversations | Dialogues | chat, voice request, escalation reply and linked context |
| Decisions | Decisions, Council | options, approvals, votes, decision record and follow-up effect |
| Assurance | Checks, Incidents | quality, safety, delivery confidence and recovery without raw logs |
| Intelligence | Assistants, Memory | AI team, skills, explanations and reusable precedents |
| Ecosystem | Marketplace, Network | approved modules, integrations and cross-center collaboration |
| Personal | Settings | profile, notifications, access and personal preferences |

These map directly to the owner-approved 13-section backlog reference: Overview, Tasks, Decisions, Assistants, Plan, Checks, Briefings, Incidents, Memory, Council, Marketplace, Network and Settings. Projects and Dialogues are first-class cross-cutting views rather than hidden features: a project collects work, decisions and conversations; a dialogue always links back to its project, task or decision.

## iPhone navigation

Five bottom tabs keep the product approachable while retaining the complete map:

1. **Today** — brief, priority attention and immediate next step.
2. **Work** — projects, tasks and plan.
3. **Dialogues** — chats, voice input, requests for a decision and messages.
4. **Decisions** — decision cards, approvals, council and incident choices.
5. **More** — checks, incidents, assistants, memory, briefings, marketplace, network and settings.

Each tab has a direct deep link to any represented object. The iPhone never presents a desktop table squeezed onto a small screen; it presents cards and a focused detail path. Widgets and push open the relevant decision, conversation or incident, never a generic home screen.

## Shared interaction model

- A global **Ask AICC** entry point is present everywhere. It accepts text or voice and offers three explicit intentions: ask a question, create a proposed task, or prepare a decision. Nothing is launched or changed without a visible confirmation.
- A dialogue is object-linked. The header shows the related project/task/decision, an AI-generated summary, sources and current state; the thread remains readable without opening technical logs.
- Every decision uses one card: situation, why now, options, recommendation, risk, owner, deadline and later measured outcome. “More detail” reveals evidence progressively.
- Every operational object has the same path: overview → object detail → related dialogue/decision → activity and proof. This prevents parallel screens with duplicate status.
- States are designed before data arrives: calm, needs attention, loading, offline snapshot, waiting for someone, restricted, empty, recovered and unavailable.
- High-risk requests add friction proportionate to risk: clear consequence, explicit confirmation and, on iPhone, biometric confirmation when the approved command gateway requires it.

## What is deliberately deferred

The app can visually represent all sections now, but live chat, task creation, reprioritization, audit launch, approvals and voice processing remain disabled until their independently accepted gateway contracts exist. Fixture screens must state this plainly rather than imitate a completed capability.

## Design guardrails

- The default view is the owner’s day, not infrastructure telemetry.
- Conversation is not an unbounded AI chat: it is accountable communication attached to work and decisions.
- Notifications are reserved for a required owner decision, a material change or a requested completion; routine movement remains quiet.
- Technical identifiers, raw prompts and raw logs are never primary UI. Evidence is available on demand, redacted and explained in ordinary language.
