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

@Test func deviceCredentialAuditLogRecordsEveryAccessWithoutTheSecret() throws {
    let url = FileManager.default.temporaryDirectory
        .appending(path: "aicc-test-\(UUID().uuidString)/device-credential-audit.jsonl")
    defer { DeviceCredentialAuditLog.clear(at: url) }

    DeviceCredentialAuditLog.record(action: .save, outcome: .success, to: url)
    DeviceCredentialAuditLog.record(action: .load, outcome: .success, to: url)
    DeviceCredentialAuditLog.record(action: .delete, outcome: .success, to: url)
    DeviceCredentialAuditLog.record(action: .load, outcome: .absent, to: url)

    let entries = DeviceCredentialAuditLog.readAll(from: url)
    #expect(entries.map(\.action) == [.save, .load, .delete, .load])
    #expect(entries.map(\.outcome) == [.success, .success, .success, .absent])

    // The audit trail must never contain the credential value itself.
    let raw = try String(contentsOf: url, encoding: .utf8)
    #expect(!raw.contains("round-trip-token"))

    #expect(DeviceCredentialAuditLog.clear(at: url))
    #expect(DeviceCredentialAuditLog.readAll(from: url).isEmpty)
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

@Test func widgetSnippetsCoverAllThreeFlowsInOrderEveryTime() throws {
    let snapshot = try Fixture.healthySnapshot()
    let snippets = snapshot.widgetSnippets()
    #expect(snippets.map(\.flow) == [.work, .dialogues, .decisions])
}

@Test func workSnippetSurfacesTheBlockedTaskBeforeAnyActiveTask() throws {
    let snapshot = try Fixture.healthySnapshot()
    let work = snapshot.widgetSnippets().first { $0.flow == .work }
    #expect(work?.destination == .task(id: "VOYN-EXAMPLE-002"))
    #expect(work?.statusLine == "Canary has not been verified")
}

@Test func workSnippetFallsBackToACalmInboxWhenNothingNeedsTheOwner() {
    let empty = Snapshot(schemaVersion: "1.0", revision: "r", generatedAt: .now, freshness: .fresh, tasks: [], lanes: [], events: [])
    let work = empty.widgetSnippets().first { $0.flow == .work }
    #expect(work?.destination == .flowInbox(.work))
}

@Test func dialoguesSnippetPicksTheMostRecentlyActiveDialogue() {
    let older = DialogSummary(id: "d1", title: "Older", state: "open", lastActivityAt: Date(timeIntervalSince1970: 0), messageCount: 2, lastSummary: nil)
    let newer = DialogSummary(id: "d2", title: "Newer", state: "open", lastActivityAt: Date(timeIntervalSince1970: 1000), messageCount: 5, lastSummary: nil)
    let empty = Snapshot(schemaVersion: "1.0", revision: "r", generatedAt: .now, freshness: .fresh, tasks: [], lanes: [], events: [])
    let dialogues = empty.widgetSnippets(dialogs: [older, newer]).first { $0.flow == .dialogues }
    #expect(dialogues?.destination == .dialogue(id: "d2"))
    #expect(dialogues?.statusLine == "Newer · сообщений: 5")
}

@Test func decisionsSnippetStaysHonestAboutMissingBackingData() {
    let empty = Snapshot(schemaVersion: "1.0", revision: "r", generatedAt: .now, freshness: .fresh, tasks: [], lanes: [], events: [])
    let decisions = empty.widgetSnippets().first { $0.flow == .decisions }
    #expect(decisions?.destination == .flowInbox(.decisions))

@Test func impactStoryDecodesTimelineChainAndRiskFromFixture() throws {
    let snapshot = try Fixture.healthySnapshot()
    let withStory = try #require(snapshot.tasks.first { $0.id == "VOYN-EXAMPLE-002" })
    let story = try #require(withStory.story)
    #expect(story.timeline.count == 3)
    #expect(story.causeChain.count == 2)
    #expect(story.risk == .medium)
    #expect(snapshot.tasks.first { $0.id == "VOYN-EXAMPLE-001" }?.story == nil)
}

@Test func impactStoryNarrativeOrdersTimelineBeforeCauseChain() {
    let story = ImpactStory(
        timeline: [
            ImpactTimelineStep(id: "b", occurredAt: Date(timeIntervalSince1970: 200), headline: "Second"),
            ImpactTimelineStep(id: "a", occurredAt: Date(timeIntervalSince1970: 100), headline: "First")
        ],
        causeChain: [ImpactCauseLink(cause: "X failed", effect: "Y is blocked")],
        risk: .high,
        riskExplanation: "Customers may notice a delay."
    )
    #expect(story.narrative == ["First", "Second", "Because X failed, Y is blocked."])
}

@Test func impactRiskLevelOrdersFromLowToCritical() {
    #expect(ImpactRiskLevel.low < .medium)
    #expect(ImpactRiskLevel.medium < .high)
    #expect(ImpactRiskLevel.high < .critical)
}

@Test func taskToleratesMissingOrMalformedStoryWithoutFailing() throws {
    let missing = Data("""
    {"id":"X","title":"T","blocker":null,"evidence":{"headSHA":null,"pullRequest":null,"ci":"unknown","acceptance":"unknown","mergedSHA":null,"deployedSHA":null}}
    """.utf8)
    let taskWithoutStory = try JSONDecoder().decode(AICCNativeCore.Task.self, from: missing)
    #expect(taskWithoutStory.story == nil)

    let malformed = Data("""
    {"id":"X","title":"T","blocker":null,"evidence":{"headSHA":null,"pullRequest":null,"ci":"unknown","acceptance":"unknown","mergedSHA":null,"deployedSHA":null},"story":{"risk":"unheard-of"}}
    """.utf8)
    let taskWithBadStory = try JSONDecoder().decode(AICCNativeCore.Task.self, from: malformed)
    #expect(taskWithBadStory.story == nil)
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

@Test func taskCriticalityRanksBlockerAboveAmbiguousAboveRoutine() {
    let evidence = DeliveryEvidence(headSHA: nil, pullRequest: nil, ci: .unknown, acceptance: .unknown, mergedSHA: nil, deployedSHA: nil)
    let blocked = AICCNativeCore.Task(id: "1", title: "T", blocker: "Waiting on owner", evidence: evidence)
    #expect(blocked.criticality == .critical)

    let ambiguous = AICCNativeCore.Task(id: "2", title: "T", blocker: nil, evidence: evidence)
    #expect(ambiguous.evidence.derivedStatus == .unknown)
    #expect(ambiguous.criticality == .high)

    let awaitingAcceptance = DeliveryEvidence(headSHA: "abc", pullRequest: "#1", ci: .verified, acceptance: .pending, mergedSHA: nil, deployedSHA: nil)
    #expect(awaitingAcceptance.derivedStatus == .awaitingAcceptance)
    let pendingReview = AICCNativeCore.Task(id: "3", title: "T", blocker: nil, evidence: awaitingAcceptance)
    #expect(pendingReview.criticality == .medium)

    let routine = DeliveryEvidence(headSHA: "abc", pullRequest: "#1", ci: .verified, acceptance: .verified, mergedSHA: "def", deployedSHA: "fed")
    #expect(routine.derivedStatus == .completed)
    let done = AICCNativeCore.Task(id: "4", title: "T", blocker: nil, evidence: routine)
    #expect(done.criticality == .low)
}

@Test func hapticPatternsAreDistinctAndEscalateWithCriticality() {
    let patterns = Criticality.allCases.map(HapticSignal.pattern(for:))
    // Every level maps to a pattern nobody else shares — pulse count and/or
    // style differ, so the signal survives even if one dimension is missed.
    for i in patterns.indices {
        for j in patterns.indices where i != j {
            #expect(patterns[i] != patterns[j])
        }
    }
    // Longer or heavier as criticality rises: critical is never shorter than
    // low, and it is the only level that carries the sharp `.error` pulse.
    #expect(HapticSignal.pattern(for: .critical).pulses.count >= HapticSignal.pattern(for: .low).pulses.count)
    #expect(HapticSignal.pattern(for: .critical).pulses.contains(.error))
    #expect(!HapticSignal.pattern(for: .low).pulses.contains(.error))
}

@Test func criticalityOrdersLowToCritical() {
    #expect(Criticality.low < .medium)
    #expect(Criticality.medium < .high)
    #expect(Criticality.high < .critical)
}
