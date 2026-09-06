# ADR-0001: AICC Native client architecture

- Status: accepted for Phase 0 / subject to prototype gates
- Scope: `NEW-1293`, client-only; AIOS is unchanged

## Context

The current AICC desktop client is a local-first Python/PySide6 application. Its packaged macOS and Windows builds bundle Python, have no authenticated remote gateway, and have no iOS or Android targets. It remains a useful local-operator client and source of redacted read-model semantics, but it cannot be exposed as the AICC Native remote-control product.

AIOS remains the authority for task, attempt, worker, acceptance, merge and deploy evidence. AICC Native consumes only server-redacted, versioned DTOs and never accesses PostgreSQL, SSH, systemd, GitHub credentials, worker files, or raw log payloads.

## Decision

Adopt a contract-first multi-platform client:

1. `aicc-native-core` is a Kotlin Multiplatform package for immutable DTOs, API/event decoding, cursor/revision handling, encrypted-cache interfaces, fixtures, localization keys and interaction semantics.
2. macOS and iPhone/iPad use platform-native SwiftUI presentation targets. They call the shared core through a narrow, generated interoperability boundary and use Apple accessibility, keyboard, menu, Keychain and lifecycle APIs directly.
3. Windows and Android use Compose Multiplatform presentation targets over the same core. Windows uses native installer/update integration; Android uses system navigation, TalkBack and Keystore APIs.
4. The first live capability is strictly read-only. Future mutations are a separate Command Gateway contract and require authorization, idempotency, policy, risk confirmation, audit and observable outcome.

### Current delivery slice

The first product release is deliberately Apple-only: **macOS and iPhone**. It uses SwiftUI screens and a small native Swift domain module that reads the approved v1 fixtures. iPad, Windows and Android remain future options; their implementation is not part of this slice and must not delay the first manager-ready Apple experience.

The Phase-0 prototype must prove SwiftUI-to-core cancellation, event decoding, list virtualization and theme/accessibility semantics before client implementation starts. Failure of that proof reopens this ADR; no compatibility layer is silently retained.

## Alternatives considered

| Option | Result |
| --- | --- |
| Preserve PySide6/PyInstaller | Rejected: retains Python in the release bundle; no iOS/Android target or remote security boundary. Retained only during coexistence for its local workflows. |
| Flutter | Rejected for the premium primary path: high code sharing, but Apple desktop/mobile interaction and accessibility would depend on non-native adapters. Reconsider only if the prototype gate proves cross-platform delivery cost dominates and parity is demonstrated. |
| Compose Multiplatform UI everywhere | Rejected for Apple presentation: strong Windows/Android fit, but SwiftUI provides the most reliable platform-native Apple behavior, assistive technology and App Store tooling. |
| Separate SwiftUI, WinUI and Android UIs with no shared core | Rejected: duplicates transport, event and cache correctness across three ecosystems. |

## Consequences

- Product semantics and visual tokens are shared; presentation code deliberately is not.
- Existing PySide6 screens are reference material and continue unchanged during coexistence; no big-bang rewrite or runtime bridge is allowed.
- Release artifacts contain no Python runtime or local web server.
- Phase 1 cannot use a live endpoint until the v1 contract and server-side redaction receive separate security acceptance.

## Acceptance gates

1. Core fixture decoding and compatibility tests pass on all targets.
2. The Apple spike demonstrates cancellation, offline read, reduced motion and a virtualized 10,000-row list without main-thread I/O. Compose platform spikes are deferred until the Apple release is accepted.
3. No public DTO contains secret, raw path, raw prompt or privileged credential material.
4. Platform release, signing and store credentials remain a human gate; unsigned development builds are allowed only for local/simulator testing.
