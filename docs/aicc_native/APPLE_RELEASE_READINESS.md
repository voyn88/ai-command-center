# AICC Native Apple release readiness
## Pipeline health increment (2026-10-30)
- Added the read-only **Ещё → Конвейер** screen on Mac and iPhone: status with
  text/icon/colour, queue counts and age, last-hour completions, active lanes,
  executor login warning and remediation, backlog counts, monitor findings,
  and DEFER_TO_USER escalations with ID, title, priority and snapshot-relative age.
- Reused CriticalEscalationBanner for stopped execution and owner escalations.
  These pipeline banners have no acknowledgement or command action and remain
  visible while the snapshot reports the condition.
- Added tolerant camelCase Codable models for the gateway's optional
  `pipelineHealth`, including missing/null/malformed field handling. Missing
  metrics remain unknown. No active lanes means down; auth failures, monitor
  failures (including `dead_letter_growth`) or escalations mean attention.
  A historical nonzero dead count alone does not prove growth. The optional
  monitor may be unavailable; its absence is explicitly shown on screen.
- Escalations sort by numeric priority, oldest update, then ID; unknown values
  sort last. Timestamps accept fractional seconds. The bundled sample mirrors
  `native_gateway/fixtures/projection.sample.json` in gateway DTO casing.
- Added Swift tests for compatibility, each optional field, state precedence,
  ordering, timestamp parsing and cache round trips. Fixed two missing closing
  braces and an incorrect ambiguous-evidence setup in the existing test suite.
- Added `.github/workflows/apple-native.yml`: macos-15, pinned checkout,
  contents-read permissions, concurrency, `swift build`, `swift test`, and an
  unsigned iOS Simulator build. Added a shared AICC scheme and registered all
  app sources in the Xcode project. If macos-15 is unavailable, macos-14 with
  Xcode 16 is a build-only fallback; running tests still requires macOS 15
  because that is the package deployment target.
- This increment was authored on Windows: no Swift build/test, Xcode simulator
  build, or visual/accessibility smoke has been executed locally. CI must supply
  that build/test evidence after publication; the earlier local evidence below
  predates this increment.
- Windows verification: static checks passed for DTO field-name parity, sample
  projection parity, workflow triggers/permissions/SHA pin, scheme XML, app source
  registration and navigation. `git diff --check` passed. The required
  `tests/test_release_gate_policy.py` run initially hit sandbox temp-directory
  permissions; rerunning with a workspace `--basetemp` produced 13 passes and
  two failures in existing Bash impact-selection tests because Windows-written
  CRLF input retains carriage returns in Bash. No Python files were modified.
- Contract follow-up: the checked-in snapshot JSON schema currently omits
  `pipelineHealth` despite its presence in `native_gateway/dto.py`. This client
  follows the DTO and projection sample; the gateway schema needs regeneration
  in its own change.

### Owner and release evidence still needed
- Supply/confirm the Apple Developer team identity, signing certificates and
  provisioning profiles for physical-device installation, signed archives and
  TestFlight. A team ID in the project is not evidence that those assets exist.
- Run the macOS CI and iPhone Simulator build, then device smoke with VoiceOver,
  Dynamic Type, keyboard navigation, light/dark appearance and stale/offline data.
- Confirm the production gateway endpoint, device token provisioning and live
  pipeline-health feed; complete the production-like HTTPS smoke below.

## Completed locally
- Native SwiftUI app target builds as a macOS app and an iPhone Simulator app.
- The iPhone app was installed and launched on iPhone 17 Pro Simulator.
- The Mac app bundle was built and launched locally.
- The client defaults to a safe fixture snapshot and rejects prohibited DTO content.
- The remote snapshot connector accepts only HTTPS and schema version 1.0.
- The release build can receive AICC_SERVER_URL without source changes.
## Server activation contract
- Server exposes GET {AICCServerURL}/v1/snapshot with a redacted version 1.0 snapshot DTO.
- Server session authentication, device registration and read scope require separate independent security acceptance.
- Client commands remain disabled until a versioned Command Gateway supplies authorization, confirmation, idempotency, policy and durable audit evidence.
## External release gates
- Apple Developer signing identity: not present on this Mac.
- Provisioning profile: not present on this Mac.
- Physical iPhone installation and TestFlight are blocked only by those Apple account assets.
- Production server endpoint and accepted device session flow are not provisioned yet.
## Required next evidence
- Signed archive for macOS and iPhone.
- Installed physical-iPhone smoke with VoiceOver and dynamic type.
- Production-like HTTPS gateway smoke: 200, 304, offline fallback, schema mismatch and unsafe DTO rejection.
- Independent review of exact head SHA before publication.
