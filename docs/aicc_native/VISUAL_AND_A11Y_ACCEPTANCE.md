# Visual and Accessibility Acceptance — AICC Native

**Status:** acceptance gate for native macOS and mobile releases.
**Scope:** Dashboard, Tasks, Agents/Workers, PR/Acceptance and Activity Timeline;
their loading, offline, degraded, error and unknown-evidence states.
**Target:** a calm, high-information manager interface. It must make the next
decision understandable before it makes implementation detail visible.

This is an independent release gate. It does not redefine the design system or
the domain model. A feature is not accepted merely because it is visually
polished, manually usable by its author, or has a passing automated test.

## 1. Evidence required for acceptance

For each target platform and supported appearance, attach the following to the
release evidence:

1. Screenshots or a short recording of all five primary screens in light,
   dark, and system high-contrast appearance.
2. Keyboard traversal evidence on macOS (including an empty, loading, error,
   and disabled-control state).
3. A VoiceOver walkthrough on macOS and a TalkBack walkthrough on iPhone or
   Android-equivalent mobile target, covering the journeys in section 5.
4. Contrast results for every semantic text/status/focus pairing used by the
   release, including disabled and selected controls.
5. Reduced Motion evidence for navigation, refresh, loading and status change.
6. A defect list with severity, owner, waiver (if any), and a dated retest.

Automated checks are supporting evidence only. A human must complete the
keyboard and assistive-technology checks on the release candidate.

## 2. Visual acceptance criteria

### 2.1 Decision-first hierarchy

- [ ] Every primary screen states its purpose in a visible title and has one
  obvious primary region. A user can identify the current project/scope,
  freshness of data, and the most important issue within five seconds.
- [ ] The screen order is: owner-relevant summary; current blocker or next
  decision; supporting evidence; technical detail on demand. Raw identifiers,
  timestamps and logs never compete with a task title or blocker for emphasis.
- [ ] Status is communicated by a text label and icon as well as color. A
  label such as `Blocked: CI failed` is acceptable; color-only dots, vague
  `Attention`, and unexplained red/yellow/green states are not.
- [ ] A task cannot be presented as completed when any required evidence is
  unknown: commit/head SHA, PR, required CI, independent exact-SHA acceptance,
  merge SHA, deployed SHA, or canary. The UI says which evidence is missing.
- [ ] Dense tables preserve scanability: stable column names, aligned values,
  useful default sort order, no horizontal truncation of the primary task name
  or blocker. Secondary technical data can be collapsed or copied from detail.
- [ ] Empty screens explain why there is no data and offer one safe next step;
  they do not resemble a successful zero state.

### 2.2 Plain language and disclosure

- [ ] Every user-facing state has a short Russian and English label. Technical
  terms (for example, `exact-SHA`, `merge queue`, `circuit breaker`) have a
  one-sentence plain-language explanation available in context.
- [ ] Failure copy names the affected thing, what is known, what remains
  unknown, and whether user action is required. It never asks the user to
  inspect a terminal or infer a remediation from an error code.
- [ ] No screen exposes secrets, tokens, DSNs, raw prompt content, private
  paths, or unredacted logs. A redacted item states that details are protected
  without offering a misleading reconstruction path.
- [ ] Icons that trigger actions or convey state have a visible label or an
  accessible name. Decorative icons are ignored by assistive technology.

### 2.3 Appearance and contrast

- [ ] Light, dark and system high-contrast appearances render every primary,
  selected, hover, focused, disabled, warning, danger, success and offline
  state without hard-coded colors that defeat the selected appearance.
- [ ] Normal text and meaningful iconography meet WCAG 2.2 AA contrast (at
  least 4.5:1); large text meets 3:1; focus indicators and essential graphical
  objects meet 3:1 against adjacent colors.
- [ ] Focus, selection and disabled states remain distinguishable without
  relying on hue alone. High contrast may simplify decoration but may not hide
  status, current location, or critical actions.
- [ ] Text remains usable at the platform's largest supported accessibility
  text setting: no clipped label, overlapping control, inaccessible primary
  action, or content that requires a fixed zoom workaround.

### 2.4 Motion and perceived quality

- [ ] Motion clarifies a change of context or state; it never provides the
  only evidence that a refresh, navigation, warning, or failure occurred.
- [ ] With Reduce Motion enabled, nonessential animation is removed or reduced
  to an immediate state change. The resulting UI remains equally intelligible.
- [ ] Loading uses skeletons or clear progress language that preserves the
  page hierarchy. It does not use endless spinners without a scope or status.
- [ ] Refresh and background synchronization never block navigation or make
  existing readable content disappear without a replacement state.

## 3. Interaction and assistive-technology criteria

### 3.1 Keyboard (macOS)

- [ ] All read-only functionality, disclosure controls, filters, project
  switching, refresh and detail navigation work with keyboard only.
- [ ] Tab and Shift+Tab order follows visible reading order. Focus never
  disappears, lands on noninteractive decoration, or jumps behind an overlay.
- [ ] Focus is always visibly indicated at AA non-text contrast; it is not
  represented solely by a subtle shadow or color change.
- [ ] Enter/Space activate the focused control; Escape closes a transient
  dialog, menu or detail overlay without committing an action; arrow keys work
  in native lists and menus.
- [ ] When a dialog or menu opens, focus enters it; when it closes, focus
  returns to the invoking control. Keyboard access cannot become trapped.
- [ ] Disabled future features are not focusable and expose their reason to
  assistive technology. A disabled item never silently navigates or mutates.

### 3.2 VoiceOver and TalkBack

- [ ] Screen-reader traversal follows the visual and decision hierarchy:
  screen title, data freshness/state, blocker summary, primary list, then
  supporting detail.
- [ ] Every actionable control has a unique accessible name, role and state;
  its name describes the outcome, not only the visual shape (for example,
  `Refresh task status`, not `Circular arrow`).
- [ ] A task row announces task title, project, priority, stage, blocker and
  completion-evidence state without requiring the user to open every cell.
- [ ] Status announcements include text equivalents: `CI failed`, `acceptance
  is pending for this SHA`, `offline — data is from 10:42`, or `deployment is
  unverified`; they are never announced merely as a color or glyph.
- [ ] Loading, successful refresh, offline transition, recoverable error and
  critical evidence change are announced once, politely, without repeated
  focus theft or excessive chatter.
- [ ] Custom visual components expose a native accessible role and do not
  flatten multiple actionable elements into one inaccessible container.

## 4. Mandatory state acceptance

| State | User must see | Accessibility and safety proof |
|---|---|---|
| Loading | What is loading, retained last-known data if available, and that navigation remains available | Announce loading once; no focus loss or blocked keyboard path |
| Offline/cache | Explicit offline label, age of cached snapshot, last successful refresh, and safe retry affordance | Do not label cache as live; announcement includes stale age |
| Degraded | Affected source/feature, unaffected information, and current safe behavior | Do not render a generic success indicator; disclose partial data |
| Error | Plain-language summary, correlation/reference ID if available, retry state, and non-destructive next step | Error is readable by VoiceOver/TalkBack and survives long enough to inspect |
| Unknown evidence | Exact missing proof and the consequence (for example, merge/deploy cannot be confirmed) | Never infer success from nearby data or use a success color |
| Empty | Why the list is empty, filters/scope involved, and one safe route forward | Visible and announced as an empty state, not a blank region |
| Redacted | That information is protected and what high-level state can still be shown | No secret, raw path, token, prompt or sensitive log becomes accessible through labels/copy |

## 5. Required manual journeys

Each journey must pass in light, dark and high-contrast appearance. Keyboard
and screen-reader legs are separate proofs, not one combined assumption.

1. **Find an owner-required blocker:** open Dashboard, identify its project,
   affected task and plain-language reason, then open safe supporting evidence.
2. **Verify non-completion:** open a task missing exact-SHA acceptance or
   verified deployment and confirm it cannot be read as completed.
3. **Inspect a PR gate:** find head SHA, required check state, reviewer/marker
   state and the precise reason merge remains prohibited.
4. **Recover from stale data:** start from cached offline content, discover its
   age, retry safely, and distinguish refreshed from still-stale content.
5. **Handle an infrastructure failure:** identify the affected worker/lane,
   degraded service and latest safe status without reading raw logs.
6. **Navigate without pointer:** complete journeys 1–5 with keyboard only on
   macOS, including opening/closing overlays and returning to the previous
   control.
7. **Navigate with assistive technology:** complete journeys 1–5 with
   VoiceOver and TalkBack, checking names, roles, state announcements and
   logical reading order.
8. **Reduce motion:** repeat navigation, refresh and an evidence-state change
   with Reduce Motion enabled; no essential information is motion-only.

## 6. UX risk register

| Priority | Risk | User impact | Preventive acceptance control | Escalation |
|---|---|---|---|---|
| P0 | A missing acceptance/deploy proof appears as success | Owner merges or trusts an unverified delivery | Unknown evidence is explicit; completed state requires the whole evidence chain | Block release and correct DTO/state mapping |
| P0 | Secret, prompt, path or sensitive log leaks via detail, copy, or accessibility text | Security/privacy breach | Server-side redacted DTO plus negative tests and screen-reader inspection | Block release; rotate/revoke if exposure is real |
| P0 | Keyboard or screen reader cannot reach a primary blocker/action | Operator is excluded from delivery control | Mandatory journeys 6–7 on release candidate | Block release |
| P1 | Color-only or low-contrast critical status | Warning/error is missed, especially in dark/high contrast modes | Contrast evidence and text-plus-icon semantic status | Fix before GA; no waiver for critical states |
| P1 | Offline cache is presented as live | Incorrect operational decision from stale data | Explicit freshness, stale age and retry state | Block affected screen from release |
| P1 | Motion or refresh steals focus / blocks navigation | Loss of orientation and task interruption | Reduced Motion and keyboard regression journeys | Fix before GA |
| P1 | Dense technical UI hides the actual blocker | Slow, error-prone executive decisions | Five-second hierarchy check and plain-language copy review | Rework hierarchy before acceptance |
| P2 | Empty/degraded state has no actionable explanation | Support burden and user confusion | Required-state table and content review | Track with owner and release target |
| P2 | Repeated announcements create screen-reader noise | Assistive-technology users lose context | Manual VoiceOver/TalkBack run with announcement log | Tune before next release |

## 7. Acceptance decision

The release owner records one of:

- **ACCEPT:** all mandatory criteria and journeys passed; evidence links and
  tested build identifier are recorded.
- **REJECT:** a P0/P1 criterion fails, evidence is absent, or an exception
  would cause an inaccessible or misleading operational state.
- **CONDITIONAL:** P2-only defects have dated owners and a documented rationale;
  this status may not waive any P0/P1 criterion.

This document evaluates user-facing quality and accessibility. It does not
replace security acceptance, API contract acceptance, functional tests, or the
independent exact-SHA delivery acceptance gate.
