# AICC Native — Design System

Status: **product specification for the Apple-native read-only client.**  This
document defines a calm executive interface for macOS and iPhone.  It does not
change the existing Qt, Streamlit, prototype, control-plane, or command paths.

## 1. Product posture

The first screen answers three questions in this order:

1. Is autonomous delivery healthy?
2. What needs the owner's attention now?
3. Can the owner trust the displayed state?

The application is an overview-first control surface, not a log viewer. It
turns engineering evidence into clear operational meaning. Raw identifiers,
worker output, full CI logs, and transport details appear only after an
intentional drill-down. A healthy-looking card must never hide a missing
evidence link: unknown, delayed, partial, and blocked are first-class states.

### Design principles

- **Calm confidence.** One primary decision per surface; restrained colour,
  motion, and decoration.
- **Evidence before celebration.** "Completed" requires the complete accepted
  and deployed evidence chain; otherwise show the most accurate earlier stage.
- **Human language first.** Start with the consequence and a plain-language
  explanation. Preserve technical evidence as copyable secondary detail.
- **Progressive disclosure.** Overview → selected item → evidence detail. Do
  not place a dashboard, table, activity stream, and log on the same surface.
- **Safety visible by design.** Read-only is a product state. Future actions
  remain visually distinct from observations and never masquerade as a direct
  control over workers or infrastructure.

## 2. Semantic foundations

Tokens express meaning, not a specific framework or platform implementation.
Each colour token must have a light and dark value that meets the accessibility
rules in §8. Implementations may map these names to SwiftUI assets, dynamic
system colours, or test fixtures; screens must not hard-code literals.

### 2.1 Colour tokens

| Token | Meaning | Use |
|---|---|---|
| `surface.canvas` | Main application background | Window and scroll background |
| `surface.raised` | Grouped content | Cards, inspector panels, popovers |
| `surface.sunken` | Recessed or selected context | Sidebar selection, timeline tracks |
| `surface.overlay` | Modal layer | Sheets and critical confirmation context |
| `content.primary` | Highest-emphasis text | Titles, key values, owner alert |
| `content.secondary` | Supporting explanation | Metadata and timestamps |
| `content.tertiary` | De-emphasised supporting detail | Helper text; never required status |
| `content.onAccent` | Text on accent controls | Primary action labels |
| `border.subtle` | Quiet grouping boundary | Card and list separators |
| `border.strong` | Keyboard or selected boundary | Selection and focus-adjacent affordances |
| `accent.primary` | Navigation and positive forward progress | Selected destination, primary link |
| `accent.primaryPressed` | Pressed state | Active primary control |
| `state.healthy` | Verified operating normally | Healthy system, passed gate |
| `state.attention` | Owner attention soon | Degraded, waiting, cost threshold |
| `state.risk` | Blocked or failed | Failed gate, circuit open, policy denial |
| `state.neutral` | Informational or inactive | Queued, paused, read-only label |
| `state.unknown` | Insufficient or stale evidence | Contract pending, missing link, offline |

Status colour is always paired with a text label and an icon. Colour alone
never communicates state. `state.healthy` is never used for a task whose
acceptance or deployed SHA is unknown.

### 2.2 Typography and numbers

| Token | Role | Rules |
|---|---|---|
| `type.hero` | Single dashboard conclusion | One line where possible; not for decoration |
| `type.title` | Screen and inspector titles | Semibold; supports Dynamic Type scaling |
| `type.section` | Group title | Groups related decisions, not arbitrary cards |
| `type.body` | Explanation and task title | Readable at all accessibility sizes |
| `type.caption` | Provenance and compact metadata | Never the sole carrier of essential state |
| `type.mono` | SHA, run ID, request ID | Copyable; secondary until user asks for evidence |
| `number.metric` | Counts, duration, cost | Tabular figures; units and freshness shown nearby |

Use sentence case, concise Russian or English terms, and locale-aware dates,
durations, numbers, currencies, and pluralisation. Do not abbreviate a status
until it becomes ambiguous. SHA values may be shortened visually but must copy
as full exact values.

### 2.3 Shape, spacing, depth, and motion

| Token | Standard |
|---|---|
| `space.1/2/3/4/6/8` | 4 / 8 / 12 / 16 / 24 / 32 pt logical spacing |
| `radius.control` | 10 pt for buttons, fields, and compact chips |
| `radius.card` | 16 pt for grouped executive content |
| `radius.sheet` | 20 pt for sheets and inspectors |
| `elevation.raised` | Subtle border first; shadow only when hierarchy requires it |
| `motion.quick` | 120–160 ms feedback or state transition |
| `motion.standard` | 180–240 ms navigation or disclosure transition |
| `motion.emphasis` | 280 ms maximum; reserved for owner-attention arrival |

Motion clarifies continuity; it never delays an operation or decorates a
stable state. Avoid automatic looping, bouncing, parallax, and pulsing alerts.

## 3. Semantic state model

Every operational item exposes a state label, explanation, freshness, and—if
available—an evidence link. The same semantic names are used in Dashboard,
Tasks, PR/Acceptance, Agents, and Timeline.

| State | Executive label | Required explanation | Presentation |
|---|---|---|---|
| `verified` | Verified | Exact evidence is current | `state.healthy`, check icon |
| `inProgress` | In progress | Current stage and elapsed time | `accent.primary`, progress affordance |
| `waiting` | Waiting | Dependency or expected external event | `state.neutral`, no false alarm |
| `attention` | Attention needed | What the owner should review and why | `state.attention`, priority ordering |
| `blocked` | Blocked | Blocking condition and safe next step | `state.risk`, persistent until resolved |
| `failed` | Failed | Failed boundary, latest retry, remediation owner | `state.risk`, never transient toast only |
| `degraded` | Degraded | Scope, impact, and last known good time | `state.attention` or `state.risk` by impact |
| `offline` | Offline | Cached snapshot time and retry state | `state.unknown`, cache content remains readable |
| `stale` | Data may be outdated | Freshness deadline and last refresh time | `state.unknown`, no implied live monitoring |
| `unknown` | Evidence unavailable | Missing contract or evidence link | `state.unknown`, explicit—not blank |
| `readOnly` | Read-only | Observation only; no control executed here | `state.neutral`, persistent scope label |

### Delivery-evidence language

The task progress rail always uses this order:

`Task → Attempt → Commit → PR → CI → Independent acceptance → Merge → Deploy`

Each step has `verified`, `inProgress`, `waiting`, `blocked`, `failed`, or
`unknown`; later stages may not visually imply success for a missing earlier
stage. A task receives **Completed** only when the product contract defines all
required links as verified. Until then, show the furthest verified stage, e.g.
"Acceptance received; merge not verified".

## 4. Information hierarchy

### 4.1 Dashboard

The Dashboard contains, in order:

1. **Autonomy summary** — one outcome sentence and freshness.
2. **Owner attention** — at most three ranked items, each with consequence,
   explanation, and a drill-down destination.
3. **Delivery flow** — compact counts by evidence stage; no decorative charts.
4. **Operational health** — agents, workers, and infrastructure only when they
   change the owner decision.
5. **Recent verified change** — a short timeline of meaningful transitions.

Empty healthy space is intentional. Hide a zero-value section rather than
filling it with empty charts or duplicated metrics. Counts always provide a
path to the filtered underlying items.

### 4.2 Detail and inspector

A list row answers identity, state, consequence, and recency. Selection opens
an inspector or destination page containing the evidence rail, plain-language
blocker, related timeline, and copyable technical facts. Logs are a final,
explicit disclosure level and must arrive redacted.

Use a single primary focus per screen. Nested tabs, simultaneously expandable
cards, and horizontally scrolling data tables are prohibited on iPhone.

## 5. Adaptive layout

The data model and semantic states remain identical across devices; placement
and disclosure vary by available space.

| Context | Navigation | Layout | Detail behavior |
|---|---|---|---|
| macOS wide | Sidebar + optional inspector | 2–3 calm columns; overview remains visible | Inspector may sit beside the list |
| macOS compact | Sidebar collapses to labels/icons as needed | One primary column plus inspector | Preserve keyboard hierarchy and shortcuts |
| iPhone compact | Tab bar for top-level destinations | Single column, prioritised sections | Push detail; evidence rail wraps vertically |
| iPhone large | Tab bar + contextual toolbar | One column with grouped metrics | Bottom sheet only for short, reversible context |

Breakpoints are driven by available container width, Dynamic Type size, and
input modality—not device model names. A narrow window must reflow content;
it must not scale text down, truncate a state explanation, or force essential
horizontal scrolling.

## 6. Component rules

### Status badge

A badge contains icon + localized label. It may be compact in a dense list,
but its accessible label includes the explanation where space does not. It is
not a button unless it has a visible disclosure affordance.

### Metric card

One metric, plain-language label, comparison or freshness where meaningful,
and a drill-down. A metric card never creates a dashboard action on its own;
if a decision is needed, it appears in Owner attention.

### Attention card

Contains severity, consequence, one recommended owner decision, and a clear
destination. No alert may be dismissed permanently while its underlying
condition remains true.

### Task row

Shows title/project, priority when it changes order, furthest verified delivery
stage, one blocker/explanation, and update time. Exact SHA and worktree remain
secondary copyable metadata.

### Evidence rail

Shows the ordered delivery chain from §3. A selected step exposes evidence
references, timestamp, and exact SHA when applicable. It does not simulate
missing evidence with a green connector.

### Loading, empty, error, offline

- **Loading:** retain prior verified content with a small refresh indication;
  use skeletons only before first content.
- **Empty:** state why no data exists and offer a safe navigation/filter reset.
- **Error:** name what failed, what remains available, and a retry option.
- **Offline:** show cached data, timestamp, and retry state; never show it as
  live.

## 7. Interaction rules

- A tap/click opens detail; it never performs a mutating operation from an
  observation card.
- Refresh is explicit, discoverable, cancellable, and preserves selection.
- Filtering is reversible and visibly active. "Clear filters" is always
  available when a filter affects results.
- Use optimistic visual updates only after a future command API returns an
  accepted command receipt; never infer success from a pressed button.
- Destructive or guarded future actions require a named consequence, explicit
  confirmation, auth/policy result, idempotency reference, and auditable final
  state. These controls are out of scope for the current read-only product.
- Notifications are reserved for owner-attention transitions, not routine
  progress. They must deep-link to the relevant evidence.
- Preserve context when navigating back: selected project, filters, sort, and
  scroll position when platform conventions permit.

## 8. Accessibility and inclusion

- Text and essential icon contrast meet WCAG 2.2 AA: at least 4.5:1 for normal
  text, 3:1 for large text, and 3:1 for interactive boundaries and focus.
- Support Dynamic Type without clipping, overlap, or loss of task-state text.
  At accessibility sizes, metrics wrap and secondary visualisation yields to
  explanatory text.
- Every control has a concise accessible name, role, value, and state. Status
  updates use appropriately scoped live announcements; do not announce routine
  refresh churn.
- Focus order follows visual reading order. macOS supports Tab/Shift-Tab,
  Space/Enter activation, Escape dismissal, and standard shortcuts; shortcuts
  are discoverable and never the only path.
- Focus is always visible with a non-colour-only indicator. Opening and closing
  an inspector or sheet restores focus to its originating control.
- Respect system Reduce Motion: replace animated transitions, count-ups, and
  attention emphasis with immediate state changes. Respect increased contrast,
  bold text, and differentiate-without-colour preferences.
- Minimum hit target is 44 × 44 pt on touch interfaces. macOS compact controls
  may be visually smaller only when their interactive hit area and keyboard
  access remain at least equivalent.
- Language changes preserve the current destination and state; all labels,
  dates, plural forms, and screen-reader strings are localized together.

## 9. Quality gates

A screen is ready only when it proves all of the following:

1. Every semantic state in §3 has a loading/empty/error/offline counterpart
   where applicable.
2. A missing acceptance or deployed SHA cannot render as Completed.
3. Keyboard-only macOS and VoiceOver iOS/macOS journeys complete the primary
   overview → task → evidence path.
4. Light, dark, increased-contrast, Dynamic Type, and Reduce Motion variants
   retain hierarchy and clarity.
5. Desktop and mobile present the same evidence semantics without raw-log
   dependency or platform-specific reinterpretation.
6. UI tests use deterministic fixtures for verified, blocked, failed, offline,
   stale, and unknown evidence states.
