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

@Test func snapshotCacheRoundTripsThroughTheSameGuardAsNetwork() throws {
    let snapshot = try Fixture.healthySnapshot()
    let url = FileManager.default.temporaryDirectory
        .appending(path: "aicc-test-\(UUID().uuidString)/snap.json")
    #expect(SnapshotCache.save(snapshot, to: url))
    let loaded = SnapshotCache.load(from: url)
    #expect(loaded == snapshot)
    #expect(SnapshotCache.clear(at: url))
    #expect(SnapshotCache.load(from: url) == nil)
}

// MARK: - VOYN-IOS-CONTEXT-APP: situational notification gate

@Test func criticalAndImportantAlwaysBypassSituationalFiltering() {
    let calendar = Calendar(identifier: .gregorian)
    let noon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: Date())!
    // Worst-case context: asleep, driving-equivalent, away, quiet hours all set to cover noon.
    let hostileContext = SituationalContext(timestamp: noon, activity: .sleeping, location: .awayFromRelevantPlace, quietHoursStart: 0, quietHoursEnd: 24, calendar: calendar)

    let critical = NotificationCandidate(id: "security-alert", importance: .critical)
    let important = NotificationCandidate(id: "approval-needed", importance: .important)
    #expect(SituationalNotificationGate.decide(critical, context: hostileContext) == .deliver)
    #expect(SituationalNotificationGate.decide(important, context: hostileContext) == .deliver)
}

@Test func routineNotificationsAreSuppressedWhileAsleepDrivingOrAwayOrQuiet() {
    let calendar = Calendar(identifier: .gregorian)
    let midnight = calendar.date(bySettingHour: 23, minute: 0, second: 0, of: Date())!
    let routine = NotificationCandidate(id: "daily-nudge", importance: .routine)

    let asleep = SituationalContext(timestamp: midnight, activity: .sleeping, location: .atRelevantPlace, quietHoursStart: 0, quietHoursEnd: 0)
    #expect(SituationalNotificationGate.decide(routine, context: asleep) == .suppress(reason: "user asleep"))

    let driving = SituationalContext(timestamp: midnight, activity: .driving, location: .atRelevantPlace, quietHoursStart: 0, quietHoursEnd: 0)
    #expect(SituationalNotificationGate.decide(routine, context: driving) == .suppress(reason: "user driving"))

    let quiet = SituationalContext(timestamp: midnight, activity: .stationary, location: .atRelevantPlace, quietHoursStart: 22, quietHoursEnd: 8)
    #expect(SituationalNotificationGate.decide(routine, context: quiet) == .suppress(reason: "quiet hours"))

    let noon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: Date())!
    let away = SituationalContext(timestamp: noon, activity: .stationary, location: .awayFromRelevantPlace, quietHoursStart: 22, quietHoursEnd: 8)
    #expect(SituationalNotificationGate.decide(routine, context: away) == .suppress(reason: "away from relevant place"))
}

@Test func favorableContextDeliversRoutineNotifications() {
    let calendar = Calendar(identifier: .gregorian)
    let noon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: Date())!
    let favorable = SituationalContext(timestamp: noon, activity: .stationary, location: .atRelevantPlace, quietHoursStart: 22, quietHoursEnd: 8, calendar: calendar)
    let routine = NotificationCandidate(id: "daily-nudge", importance: .routine)
    #expect(SituationalNotificationGate.decide(routine, context: favorable) == .deliver)
}

@Test func nonContextAwareRoutineNotificationsBypassTheGate() {
    let calendar = Calendar(identifier: .gregorian)
    let midnight = calendar.date(bySettingHour: 3, minute: 0, second: 0, of: Date())!
    let hostileContext = SituationalContext(timestamp: midnight, activity: .sleeping, location: .awayFromRelevantPlace, quietHoursStart: 22, quietHoursEnd: 8, calendar: calendar)
    // "не на всех этапах": a specific routine kind can opt out of filtering.
    let alwaysFire = NotificationCandidate(id: "one-time-digest", importance: .routine, contextAware: false)
    #expect(SituationalNotificationGate.decide(alwaysFire, context: hostileContext) == .deliver)
}

@Test func batchEvaluationCutsRoutinePushBy30PercentWithFullImportantConversion() {
    let calendar = Calendar(identifier: .gregorian)
    // A context where routine pushes are suppressed (away from the relevant place).
    let awayAtNoon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: Date())!
    let context = SituationalContext(timestamp: awayAtNoon, activity: .stationary, location: .awayFromRelevantPlace, quietHoursStart: 22, quietHoursEnd: 8, calendar: calendar)

    // 10 routine nudges (all suppressed away from the relevant place) plus
    // important/critical actions that must always still land.
    let routines = (0..<10).map { NotificationCandidate(id: "routine-\($0)", importance: .routine) }
    let important = (0..<5).map { NotificationCandidate(id: "important-\($0)", importance: .important) }
    let critical = [NotificationCandidate(id: "critical-0", importance: .critical)]

    let (_, stats) = SituationalNotificationGate.evaluate(routines + important + critical, context: context)

    #expect(stats.suppressionRate >= 0.3)
    #expect(stats.importantConversionRate == 1.0)
    #expect(stats.deliveredImportantOrAbove == 6)
    #expect(stats.suppressed == 10)
}

@Test func taskStateDecodesKnownAndTolatesUnknown() throws {
    let known = Data("""
    {"id":"X","title":"T","blocker":null,"state":"deferred","evidence":{"headSHA":null,"pullRequest":null,"ci":"unknown","acceptance":"unknown","mergedSHA":null,"deployedSHA":null}}
    """.utf8)
    let task = try JSONDecoder().decode(AICCNativeCore.Task.self, from: known)
    #expect(task.state == .deferred)

    let future = Data("""
    {"id":"X","title":"T","blocker":null,"state":"paused","evidence":{"headSHA":null,"pullRequest":null,"ci":"unknown","acceptance":"unknown","mergedSHA":null,"deployedSHA":null}}
    """.utf8)
    let tolerant = try JSONDecoder().decode(AICCNativeCore.Task.self, from: future)
    #expect(tolerant.state == nil)
}
