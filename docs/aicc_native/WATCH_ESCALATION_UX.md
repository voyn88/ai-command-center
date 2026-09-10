# AICC Native — Watch companion: escalation mode

**Backlog:** `VOYN-IOS-WEAR-ESCALATE` (wave P1, UX).
**Acceptance target:** a one-tap escalation with a minimal interface, reachable
from a watch complication.
**Status:** design-time specification. No watchOS target exists in
`clients/aicc-native/apple` yet (`Package.swift` declares `macOS` and `iOS`
only); this document fixes the screen, states, language and governance so a
future `watchOS` target and complication can be built against a settled
contract, in line with the design-time approach already used in
`NAVIGATION_AND_UX_ARCHITECTURE.md`.

## 1. Purpose and boundary

The watch companion is not a small iPhone app. It exposes exactly one
capability: raising an escalation — "someone needs to look at this now" — with
the least possible interface between the owner's wrist and a delivered,
audited request. It is a paging surface, not a dashboard.

Escalating is a communication action, not an infrastructure mutation. It never
approves, merges, deploys, or changes state on its own; it creates a governed
Dialogue/decision-request entry (see `NAVIGATION_AND_UX_ARCHITECTURE.md`,
"Conversations") that a human on the other end reads and acts on. This keeps
it inside the existing guardrail — the app "never sends a hidden instruction
or irreversible action" (`PRODUCT_BLUEPRINT.md`) — while still allowing a
single decisive tap, because the risk being confirmed is "notify now", not
"execute now".

## 2. Where it lives

- **Complication** (`.circular`, `.rectangular`, `.corner`, `.inline`
  families): shows only a glyph and a status color — no text, no counts. It
  mirrors the phone's calm/attention read: `state.unknown`/mint-equivalent
  "calm" glyph when nothing needs attention, warning-equivalent glyph when the
  synced snapshot already has an attention item. It never renders a badge
  count; a wrist glance is a state read, not a queue.
- **Watch app**: a single scene, no tab bar, no navigation stack. Opening the
  app (from the complication tap, from the dock, or from the app icon) lands
  directly on the Escalate screen. There is no home/overview screen to pass
  through first — that would violate the "minimal interface" acceptance
  criterion.
- The complication and app both read the same cached snapshot the iPhone app
  uses (`Snapshot.freshness`); the watch never opens its own network
  connection. If the paired iPhone is unreachable, the watch shows the last
  synced state and queues the escalation for delivery (§4).

## 3. One-tap path

1. Raise wrist / tap complication → watch app opens directly on the Escalate
   screen, pre-filled with context: the single highest-priority attention item
   from the synced snapshot (`snapshot.overview` equivalent used by the phone's
   `CalmStatus`/`WorkView`), or "General" if nothing is currently flagged.
2. One full-width primary button, "Эскалировать" fills the screen below the
   context line. This is the only actionable control on the screen.
3. A single tap on that button sends. There is no second confirmation sheet,
   no text entry, and no picker — those would turn one tap into several and
   defeat the acceptance criterion. Risk-proportionate friction (per
   `NAVIGATION_AND_UX_ARCHITECTURE.md`, "Shared interaction model") is
   satisfied instead by:
   - the button itself stating the consequence in its label context (§6), so
     the tap is informed, not blind;
   - a short, cancellable send window (§4, "Sending") rather than a modal
     confirmation — a deliberate substitution justified because the action is
     reversible-in-effect (a follow-up "false alarm" message) and time-boxed,
     not because it is exempt from confirmation altogether;
   - a per-session cooldown (§4, "Cooldown") that makes a second accidental
     tap inert instead of sending a duplicate page.
4. Long-press, Digital Crown and Force Touch are not used for anything. A
   single hardware-independent tap target is the entire interaction surface,
   so the flow works identically with VoiceOver and Switch Control.

## 4. Screens and states

| State | What is shown | Exit |
| --- | --- | --- |
| Idle / armed | Context line (inferred project/task or "Общая эскалация"), one primary button, small secondary line naming who receives it (e.g. "Дойдёт до дежурного и в Диалоги") | Tap sends → Sending |
| Sending | Button becomes a determinate 3–4 s countdown ring around a "Отменить" label; a light haptic taps once per second | Countdown ends → Sent; tap during countdown → Cancelled |
| Sent | Full-screen success glyph, one line ("Эскалация отправлена") and timestamp; a success haptic fires once | Auto-dismisses to Idle after ~4 s, or Digital Crown/tap-anywhere dismisses immediately |
| Cancelled | Confirms nothing was sent ("Отменено, ничего не отправлено"); returns to Idle | Auto-dismisses after ~2 s |
| Cooldown | Primary button is replaced by a disabled state with a visible countdown ("Повторно можно через 0:45") so a second real emergency is never silently swallowed — the countdown is always short (≤60 s) and always visible, never a silent lockout | Countdown reaches 0 → Idle |
| Offline / queued | Context line replaced with an explicit "Без связи с iPhone — отправим, как только подключимся" banner; the primary button still fires and queues locally rather than disabling, because a wrist emergency must never be blocked by a transient radio gap | Connectivity resumes → queued item flushes → Sent |
| Failed | Plain-language failure ("Не получилось отправить") with a retry button; never silently drops the request | Tap retry → Sending; auto-retry also runs in background |

Every state above is reachable with the display already in Always-On mode:
Always-On dims the button and countdown ring rather than hiding them, so the
owner can always tell whether an escalation is in flight without re-raising
the wrist.

## 5. What "escalate" produces

An escalation is not a fire-and-forget push. It creates one governed record,
delivered through the same command gateway boundary already defined for
Dialogues (`PRODUCT_BLUEPRINT.md`, "Owner opens a dialogue or escalation..."):

- a Dialogue entry tagged `source: watch`, with the inferred project/task link
  when one exists, or `scope: general` when none does;
- a timestamp, the initiating device, and the resolved recipient (on-call
  owner/team) — the same fields a phone-initiated escalation would carry, so
  the audit trail cannot distinguish "urgent" from "informal";
  it is available to the audit trail identically either way;
- delivery to the phone's Dialogues tab and to whatever paging channel the
  server resolves for the current on-call context; the watch itself has no
  knowledge of paging configuration and makes no routing decision.

The watch never composes free text and never edits the escalation's target;
those remain phone/desktop capabilities. This keeps the watch's one screen
honest about what one tap can and cannot do.

## 6. Copy (Russian, primary locale)

| Element | Copy |
| --- | --- |
| Context line (attention item present) | "Эскалация: «{название задачи}»" |
| Context line (nothing flagged) | "Общая эскалация" |
| Recipient line | "Дойдёт до дежурного и в Диалоги" |
| Primary button | "Эскалировать" |
| Sending / cancel label | "Отменить" |
| Sent | "Эскалация отправлена" |
| Cancelled | "Отменено, ничего не отправлено" |
| Cooldown | "Повторно можно через {mm:ss}" |
| Offline banner | "Без связи с iPhone — отправим, как только подключимся" |
| Failed | "Не получилось отправить" / "Повторить" |

Every string has an English equivalent for locale parity with the rest of the
product; none of these strings ever include a task ID, SHA, or raw log
fragment — consistent with `VISUAL_AND_A11Y_ACCEPTANCE.md` §2.2.

## 7. Accessibility

- The primary button's accessible label states the outcome, not the glyph:
  "Эскалировать. Отправит срочный сигнал дежурному." — never just
  "Escalate button".
- VoiceOver announces state transitions once, politely: entering Sending,
  reaching Sent/Cancelled/Failed, and entering/leaving Cooldown — matching the
  single-announcement rule in `VISUAL_AND_A11Y_ACCEPTANCE.md` §3.2.
- The countdown ring is never the only signal of remaining time; the numeric
  label and haptic cadence carry the same information for VoiceOver users and
  Reduced Motion.
- With Reduce Motion enabled, the countdown ring is replaced by a static
  numeric countdown; haptics remain the primary non-visual cue.
- Minimum touch target for the primary button is the full lower two-thirds of
  the screen — there is no small tap target on a watch-sized display for the
  one action that matters.

## 8. Risk register

| Priority | Risk | Mitigation |
| --- | --- | --- |
| P0 | Accidental complication tap pages someone with no owner intent | Complication tap only opens the app to an armed-but-unsent screen; sending still requires the explicit primary-button tap described in §3 |
| P0 | Escalation is silently dropped when the watch has no connectivity | Offline state always queues and states so explicitly (§4); it never disables the button |
| P1 | Cooldown reads as a lockout during a real repeated emergency | Cooldown is capped at 60 s, always shows a visible countdown, and never blocks the underlying phone/desktop escalation path |
| P1 | Owner cannot tell whether a queued escalation actually sent | Sent/Failed are terminal, explicit, haptic-confirmed states; nothing exits silently to Idle without one of Sent, Cancelled or Failed being shown first |
| P2 | Complication shows a stale calm/attention read after a long offline period | Complication inherits `Snapshot.freshness`; a stale complication is out of scope for this document and tracked against the shared freshness model used by the phone app |

## 9. Acceptance checklist

- [ ] Raising the wrist or tapping the complication reaches the Escalate
  screen in one action, with no intermediate list, tab bar or overview.
- [ ] Sending the escalation is one tap on one full-width control; no text
  entry, picker, or second confirmation sheet exists on this screen.
- [ ] Every state in §4 is implemented, including Cancelled, Cooldown, Offline
  and Failed — a build that only implements the happy path (Idle → Sending →
  Sent) does not meet this specification.
- [ ] The escalation record created is indistinguishable in the phone's
  Dialogues tab from a phone-initiated escalation, other than its `source`
  field.
- [ ] VoiceOver and Reduce Motion pass the checks in §7 on-device (simulator
  motion/haptic behavior is not sufficient evidence).
- [ ] No screen in this flow ever renders a task ID, SHA, or raw evidence
  string (§6).

## 10. Non-goals

No watchOS Xcode target, complication timeline provider, or push-delivery
implementation is added by this document. No new server contract is defined
here beyond reusing the existing Dialogue/escalation shape referenced in
`PRODUCT_BLUEPRINT.md` and `BACKLOG_PRODUCT_COVERAGE.md`. Building the actual
watchOS target is follow-up engineering scope, gated on this UX spec the same
way `NAVIGATION_AND_UX_ARCHITECTURE.md` gates the rest of the product.
