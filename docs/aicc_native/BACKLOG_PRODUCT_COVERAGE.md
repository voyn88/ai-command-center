# AICC Native — backlog product coverage

This document turns the canonical VOYN backlog into a product map for the Apple app. It does **not** change the backlog, reorder its waves or claim that a capability is implemented. A feature moves into a live client only after its AIOS contract, authorization and acceptance evidence exist.

## Product surfaces to preserve

| AICC Native surface | What a non-technical owner receives | Backlog source |
| --- | --- | --- |
| Today / Executive overview | morning brief, priority queue, risks and a clear next step | `VOYN-W1-AD`, `VOYN-W1-DIG`, `VOYN-W1-UI`, `VOYN-W4-SCENARIO`, `VOYN-IOS-AUTO-HOME` |
| Attention and decisions | conflicts, options, owner, deadline, impact and a traceable decision | `VOYN-W2-CONFLICT`, `VOYN-W3-COUNCIL`, `VOYN-MIN-EXEC`, `VOYN-MIN-COMP`, `VOYN-MIN-POST` |
| Projects and tasks | project health, dependency-aware task plan, priorities and progress | `VOYN-W2-TASKS`, `VOYN-COMMON-INTELLIGENT-QUEUE` |
| Dialogues | AI and team conversations, escalation requests, compact summaries and governed replies | `VOYN-W4-SCENARIO` (messages), `VOYN-OPS-DEFER-TO-USER-MOBILE-PUSH` (reply from phone), `VOYN-COMMON-CHAIN-COMM` (signed cross-center messages), `VOYN-COMMON-VOICE` |
| Delivery and quality | simple explanation of what is waiting, what changed and whether it is proven | `VOYN-W2-AUD`, `VOYN-COMMON-PULSE`, existing AICC delivery gates |
| Agents and skills | who is working, proven strengths, skills, cost and safe routing | `VOYN-W2-AGENT`, `VOYN-W0-AICC-SKILL-ACQUISITION`, `VOYN-W0-AICC-AGENT-MARKETPLACE`, `VOYN-MIN-TRUST-LATTICE` |
| Board view | one-page executive/investor narrative and material risks | `VOYN-MIN-BOARD-LAUNCH`, `VOYN-MIN-COMPANY-MODEL` |
| iPhone companion | offline start-of-day view, useful push, haptics, voice explanations and protected critical confirmations | `VOYN-IOS-LOCK`, `VOYN-IOS-HAPTIC`, `VOYN-IOS-AUTO-HOME`, `VOYN-IOS-SPEECH-COACH`, `VOYN-IOS-CRYPTO-KEY`, `VOYN-IOS-VISUAL-DSL` |
| Watch / complication crisis surface | a single critical escalation, resolved with one tap, without opening the phone | `VOYN-MIN-WATCH-CRISIS` |

| Watch companion | one-tap escalation from a complication or a single minimal-interface screen, governed the same as a phone-initiated escalation | `VOYN-IOS-WEAR-ESCALATE` |

## Dialogues are a first-class capability

The app will contain a **Dialogues** section, not merely a notification list. Its eventual live model needs: a conversation, participants, linked project/task/decision, readable summary, attachments represented by safe references, message delivery state, and an audit-linked reply or approval.

Chat is not a bypass around governance. The first fixture UI can show realistic conversations, but the live version requires a versioned conversation API; redaction; permissions; server-side prompt-injection handling; streaming state; rate limits; retained audit; and explicit confirmation for any action. Raw agent prompts, secrets, SSH details and infrastructure logs never belong in a conversation payload.

## Apple release sequence

1. **Foundation:** Mac and iPhone navigation, accessible design system, offline validated snapshot and Overview/Attention/Projects/Tasks/Activity fixtures.
2. **Owner communication:** Dialogues and decision-request fixtures, then the read-only conversation feed once the gateway contract is accepted.
3. **Governed response:** replies, approvals and voice-assisted requests only through a separately accepted command gateway; high-risk actions require biometric confirmation on iPhone.
4. **Differentiators:** agent/skill map, board view, decision memory, visual causality and carefully scoped push/haptics/widgets (`VOYN-MIN-WIDGET-SNIP`: one-status, one-action snippets for Work, Dialogues and Decisions — see `docs/aicc_native/WIDGET_SNIPPETS.md`).

4. **Differentiators:** agent/skill map, board view, decision memory, visual causality, carefully scoped push/haptics/widgets, and the watch companion's one-tap escalation.

Every screen must remain usable for a top manager: explain the situation, why it matters, options and next step first; technical proof can be opened only when wanted.

## Watch / complication crisis surface

`VOYN-MIN-WATCH-CRISIS` asks for a critical escalation to resolve with one tap on the smallest surface available. `AICCNativeCore` now carries the shared, testable piece of that: a `CriticalEscalation` model, a `Snapshot.openCriticalEscalations` query, and an on-device `EscalationAcknowledgementStore` that records the tap. Today it renders as a crisis banner at the top of the iPhone/Mac Overview — the same glanceable, single-action shape a watch complication needs — because no WatchKit/WidgetKit target exists yet in this package. The one-tap action is deliberately local-only: it records that the owner saw and handled the alert, and never calls AIOS or infrastructure, consistent with the Phase 0 non-goal of no mutation commands (`PRODUCT_BLUEPRINT.md`) and the disabled command gateway (`APPLE_RELEASE_READINESS.md`). Adding an actual watchOS app/complication extension is Xcode-project work for a follow-up once this interaction shape is reviewed.
