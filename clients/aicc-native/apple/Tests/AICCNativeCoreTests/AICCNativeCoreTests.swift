import Foundation
import Testing
@testable import AICCNativeCore

@Test func fixtureDecodesAndExposesFiveScreenModels() throws {
    let snapshot = try Fixture.healthySnapshot()
    #expect(snapshot.overview.activeTasks == 2)
    #expect(TasksModel(tasks: snapshot.tasks).tasks.count == 2)
    #expect(AgentsModel(lanes: snapshot.lanes).lanes.first?.state == "healthy")
    #expect(PipelineModel(tasks: snapshot.tasks).tasks[0].evidence.derivedStatus == .awaitingAcceptance)
    #expect(ActivityModel(events: snapshot.events).events.first?.correlationID == "VOYN-EXAMPLE-001")
}

@Test func completedRequiresEveryProofLink() {
    let incomplete = DeliveryEvidence(headSHA: "abc", pullRequest: "#1", ci: .verified, acceptance: .verified, mergedSHA: "def", deployedSHA: nil)
    #expect(!incomplete.isCompleted)
    #expect(incomplete.derivedStatus == .unknown)
    let complete = DeliveryEvidence(headSHA: "abc", pullRequest: "#1", ci: .verified, acceptance: .verified, mergedSHA: "def", deployedSHA: "fed")
    #expect(complete.isCompleted)
    #expect(complete.derivedStatus == .completed)
}

@Test func unsafeDTOIsRejectedBeforeDecoding() {
    #expect(throws: AICCNativeError.unsafeDTO) { try SnapshotDecoder.decode(Data("{\"authorization\":\"Bearer secret\"}".utf8)) }
}

@Test func gatewayAcceptsOnlyHTTPSAndPinsSchemaVersion() throws {
    #expect(throws: GatewayError.insecureEndpoint) { try GatewayConfiguration(baseURL: URL(string: "http://control.example")!) }
    let configuration = try GatewayConfiguration(baseURL: URL(string: "https://control.example/aicc")!)
    #expect(configuration.baseURL.absoluteString == "https://control.example/aicc")
    #expect(configuration.expectedSchemaVersion == "1.0")
}

@Test func requestCarriesBearerTokenOnlyWhenConfigured() throws {
    let bare = try GatewayConfiguration(baseURL: URL(string: "https://control.example")!)
    #expect(SnapshotRemoteStore.request(configuration: bare).value(forHTTPHeaderField: "Authorization") == nil)

    let authed = try GatewayConfiguration(baseURL: URL(string: "https://control.example")!, deviceToken: "tok-123")
    let request = SnapshotRemoteStore.request(configuration: authed, revision: "r-1")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok-123")
    #expect(request.value(forHTTPHeaderField: "X-AICC-Client-Version") == "1.0")
    #expect(request.value(forHTTPHeaderField: "If-None-Match") == "r-1")
    #expect(request.url?.absoluteString == "https://control.example/v1/snapshot")
}

@Test func deviceTokenStoreRoundTrips() {
    defer { DeviceTokenStore.delete() }
    guard DeviceTokenStore.save("round-trip-token") else { return }  // keychain may be unavailable in CI sandboxes
    #expect(DeviceTokenStore.load() == "round-trip-token")
    #expect(DeviceTokenStore.delete())
    #expect(DeviceTokenStore.load() == nil)
}

/// Live end-to-end proof against a running Gateway v1 (opt-in via environment):
/// AICC_ITEST_URL, AICC_ITEST_TOKEN, AICC_ITEST_PIN (path to the DER pin).
@Test func liveGatewayConnectsOverHTTPSWithTokenAndPin() async throws {
    let env = ProcessInfo.processInfo.environment
    guard let rawURL = env["AICC_ITEST_URL"], let token = env["AICC_ITEST_TOKEN"], let pinPath = env["AICC_ITEST_PIN"] else { return }
    let pin = try Data(contentsOf: URL(fileURLWithPath: pinPath))
    let configuration = try GatewayConfiguration(baseURL: URL(string: rawURL)!, deviceToken: token, pinnedServerCertificates: [pin])
    let store = SnapshotRemoteStore(configuration: configuration)
    let snapshot = try await store.fetchSnapshot()
    #expect(snapshot.schemaVersion == "1.0")
    #expect(!snapshot.tasks.isEmpty)

    // Same revision → 304 Not Modified surfaces as .notModified.
    await #expect(throws: GatewayError.notModified) { _ = try await store.fetchSnapshot(revision: snapshot.revision) }

    // A wrong token must be rejected as unauthorized.
    let badConfiguration = try GatewayConfiguration(baseURL: URL(string: rawURL)!, deviceToken: "wrong", pinnedServerCertificates: [pin])
    await #expect(throws: GatewayError.unauthorized) { _ = try await SnapshotRemoteStore(configuration: badConfiguration).fetchSnapshot() }

    // Without the pin the self-signed chain must NOT be trusted.
    let unpinned = try GatewayConfiguration(baseURL: URL(string: rawURL)!, deviceToken: token)
    await #expect(throws: (any Error).self) { _ = try await SnapshotRemoteStore(configuration: unpinned).fetchSnapshot() }
}
