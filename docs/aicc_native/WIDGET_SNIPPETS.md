# Home screen widget snippets (VOYN-MIN-WIDGET-SNIP)

The acceptance this works toward: **80% of owners complete the basic cycle
through widgets**, for the three flows that already have a single next
action on iPhone — Work, Dialogues, Decisions (`docs/aicc_native/
NAVIGATION_AND_UX_ARCHITECTURE.md`). This slice adds the data contract a
WidgetKit extension will render from; it does not add the extension target
itself, which needs an Xcode-managed App Group and is a separate increment.

`AICCNativeCore.WidgetIntentSnippet` (`clients/aicc-native/apple/Sources/
AICCNativeCore/AICCNativeCore.swift`) is a one-status, one-action snapshot
for a single flow: `statusLine` (what to glance at), `actionTitle` and a
`WidgetDestination` (the exact deep-link target — a task, a dialogue, or a
flow's inbox). `Snapshot.widgetSnippets(dialogs:)` always returns exactly
three, one per `WidgetFlow` case, in a fixed order — a widget gallery never
offers a subset, and a flow with nothing pending still returns a calm,
deterministic inbox pointer instead of an empty slot.

## Why "1 action" is a deep link, not a mutation

`POST /v1/commands` is deliberately out of the v1 read-only contract
(`docs/aicc_native/contracts/v1/README.md`): a command needs authorization,
policy evaluation, confirmation and durable audit before it can execute.
Until that gateway exists, the only safe "1 action" a widget can offer is
opening the app straight to the exact item — never a background mutation
triggered from the lock screen or home screen.

## Why Decisions never fabricates a pending item

Work and Dialogues compute their snippet from real DTOs (`Task`,
`DialogSummary`) already carried by the v1 snapshot and collection routes.
Decisions has no such DTO yet — the Decisions tab is still a static fixture
card (`AICCNativeApp.DecisionsView`) — so its widget snippet stays a fixed,
honest "nothing pending" pointer into the Decisions tab rather than
inventing data it cannot back. It becomes data-driven once a decisions
collection route lands.

## Proof

`Tests/AICCNativeCoreTests/AICCNativeCoreTests.swift` covers: all three
flows are always present and in order; Work prefers a blocked task over an
active one and falls back to a calm inbox when nothing needs the owner;
Dialogues picks the most recently active conversation; Decisions never
claims a pending item it cannot back.
