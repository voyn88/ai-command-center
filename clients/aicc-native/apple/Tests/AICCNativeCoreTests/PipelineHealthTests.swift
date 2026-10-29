import Foundation
import Testing
@testable import AICCNativeCore

private func health(_ json: String) throws -> PipelineHealth {
    try JSONDecoder().decode(PipelineHealth.self, from: Data(json.utf8))
}

@Test func pipelineFixtureMatchesGatewayWireFieldsAndCacheRoundTrips() throws {
    let snapshot = try Fixture.healthySnapshot()
    let value = try #require(snapshot.pipelineHealth)
    #expect(value.observedAt == "2026-08-26T00:00:00Z")
    #expect(value.queue?.ready == 3)
    #expect(value.queue?.claimed == 2)
    #expect(value.queue?.dead == 1)
    #expect(value.queue?.succeededLastHour == 7)
    #expect(value.queue?.oldestReadyAgeSeconds == 95)
    #expect(value.activeLanes?.first?.role == "aicc_worker")
    #expect(value.activeLanes?.first?.attempts == 2)
    #expect(value.activeLanes?.first?.heartbeatAgeSeconds == 14)
    #expect(value.executorAuthFailures30m == 0)
    #expect(value.backlogByStatus?["DONE"] == 40)
    #expect(value.ownerEscalations?.first?.taskID == "VOYN-EXAMPLE-002")
    #expect(value.ownerEscalations?.first?.title == "Blocked migration")
    #expect(value.ownerEscalations?.first?.priority == "P1")
    #expect(value.ownerEscalations?.first?.updatedAt == "2026-08-25T22:00:00Z")
    #expect(value.monitor?.verdict == "failing")
    #expect(value.monitor?.openFindings == 1)
    #expect(value.monitor?.failures == ["dead_letter_growth"])
    #expect(value.monitor?.lastSeenAt == "2026-08-25T23:58:00Z")
    #expect(PipelineHealthViewModel(health: value).state == .attention)
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    #expect(try SnapshotDecoder.decode(encoder.encode(snapshot)) == snapshot)
}

@Test func snapshotsTolerateAbsentNullMalformedAndFuturePipelineBlocks() throws {
    let snapshot = try Fixture.healthySnapshot()
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    var json = try #require(JSONSerialization.jsonObject(with: encoder.encode(snapshot)) as? [String: Any])
    json.removeValue(forKey: "pipelineHealth")
    #expect(try SnapshotDecoder.decode(JSONSerialization.data(withJSONObject: json)).pipelineHealth == nil)
    for block in [NSNull(), "invalid"] as [Any] {
        json["pipelineHealth"] = block
        #expect(try SnapshotDecoder.decode(JSONSerialization.data(withJSONObject: json)).pipelineHealth == nil)
    }
    json["pipelineHealth"] = ["futureField": true, "queue": ["ready": 2, "claimed": "bad"], "activeLanes": NSNull()] as [String: Any]
    let partial = try #require(SnapshotDecoder.decode(JSONSerialization.data(withJSONObject: json)).pipelineHealth)
    #expect(partial.queue?.ready == 2)
    #expect(partial.queue?.claimed == nil)
    #expect(partial.activeLanes == nil)
    #expect(PipelineHealthViewModel(health: partial).state == nil)
}

@Test func everyPipelineFieldToleratesMissingNullAndWrongType() throws {
    let sample = try #require(Fixture.healthySnapshot().pipelineHealth)
    let encoded = try JSONEncoder().encode(sample)
    let original = try #require(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
    // Exercise each field independently, including every nested DTO field.
    for key in original.keys {
        for replacement in [NSNull(), false] as [Any] {
            var object = original
            object[key] = replacement
            _ = try JSONDecoder().decode(PipelineHealth.self, from: JSONSerialization.data(withJSONObject: object))
        }
        var missing = original
        missing.removeValue(forKey: key)
        _ = try JSONDecoder().decode(PipelineHealth.self, from: JSONSerialization.data(withJSONObject: missing))
    }
    for key in ["queue", "monitor", "activeLanes", "ownerEscalations"] {
        let isArray = original[key] is [Any]
        let object = try #require((isArray ? (original[key] as? [[String: Any]])?.first : original[key]) as? [String: Any])
        for field in object.keys {
            for replacement in [NSNull(), false] as [Any] {
                var nested = object
                nested[field] = replacement
                var root = original
                root[key] = isArray ? [nested] as Any : nested as Any
                _ = try JSONDecoder().decode(PipelineHealth.self, from: JSONSerialization.data(withJSONObject: root))
            }
            var nested = object
            nested.removeValue(forKey: field)
            var root = original
            root[key] = isArray ? [nested] as Any : nested as Any
            _ = try JSONDecoder().decode(PipelineHealth.self, from: JSONSerialization.data(withJSONObject: root))
        }
    }
}

@Test func pipelineStateRulesAndDownPrecedence() throws {
    let base: [String: Any] = ["activeLanes": [["role": "worker"]], "executorAuthFailures30m": 0, "ownerEscalations": []]
    func state(_ changes: [String: Any]) throws -> PipelineHealthState? {
        let data = try JSONSerialization.data(withJSONObject: base.merging(changes) { _, new in new })
        return PipelineHealthViewModel(health: try JSONDecoder().decode(PipelineHealth.self, from: data)).state
    }
    #expect(try state([:]) == .ok)
    // Historical dead letters are not proof of current growth.
    #expect(try state(["queue": ["dead": 8]]) == .ok)
    let warnings: [[String: Any]] = [
        ["executorAuthFailures30m": 1],
        ["monitor": ["failures": ["dead_letter_growth"]]],
        ["monitor": ["failures": ["heartbeat_missing"]]],
        ["monitor": ["verdict": "failing"]],
        ["monitor": ["openFindings": 1]],
        ["ownerEscalations": [["taskID": "X"]]],
        ["backlogByStatus": ["DEFER_TO_USER": 2]],
    ]
    for warning in warnings {
        #expect(try state(warning) == .attention)
        #expect(try state(warning.merging(["activeLanes": []]) { _, new in new }) == .down)
    }
    #expect(try state(["activeLanes": []]) == .down)
    #expect(try state(["activeLanes": NSNull()]) == nil)
    #expect(try PipelineHealthViewModel(health: health("{}")).state == nil)
}

@Test func escalationsSortByPriorityThenOldestThenIDWithUnknownLast() throws {
    let value = try health(#"""
    {"ownerEscalations":[
      {"taskID":"unknown","priority":null},
      {"taskID":"p10","priority":"P10"},
      {"taskID":"b","priority":"P1","updatedAt":"2026-08-25T22:00:00.000Z"},
      {"taskID":"new","priority":"P1","updatedAt":"2026-08-26T00:00:00Z"},
      {"taskID":"p0","priority":"P0"},
      {"taskID":"a","priority":"P1","updatedAt":"2026-08-25T22:00:00Z"},
      {"taskID":"bad-date","priority":"P1","updatedAt":"invalid"},
      {"taskID":"p2","priority":"P2"}
    ]}
    """#)
    #expect(PipelineHealthViewModel(health: value).escalations.map(\.taskID) == ["p0", "a", "b", "new", "bad-date", "p2", "p10", "unknown"])
    #expect(value.ownerEscalations?.first?.taskID == "unknown")
    #expect(PipelineHealthViewModel.date("2026-08-25T22:00:00.000Z") == PipelineHealthViewModel.date("2026-08-25T22:00:00Z"))
    #expect(PipelineHealthViewModel.date("invalid") == nil)
}
