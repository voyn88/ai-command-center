# AICC Native Apple release readiness
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
