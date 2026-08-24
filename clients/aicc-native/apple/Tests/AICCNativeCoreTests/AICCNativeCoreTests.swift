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
