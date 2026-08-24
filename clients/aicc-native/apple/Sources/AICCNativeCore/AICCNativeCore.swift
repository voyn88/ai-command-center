import Foundation

public enum EvidenceState: String, Codable, Sendable { case unknown, observed, verified, rejected, pending }
public enum Freshness: String, Codable, Sendable { case fresh, stale, offline, degraded }
public enum TaskStatus: String, Codable, Sendable { case inProgress = "in_progress", awaitingCI = "awaiting_ci", awaitingAcceptance = "awaiting_acceptance", completed, unknown }

public struct DeliveryEvidence: Codable, Equatable, Sendable {
    public let headSHA: String?
    public let pullRequest: String?
    public let ci: EvidenceState
    public let acceptance: EvidenceState
    public let mergedSHA: String?
    public let deployedSHA: String?

    public init(headSHA: String?, pullRequest: String?, ci: EvidenceState, acceptance: EvidenceState, mergedSHA: String?, deployedSHA: String?) {
        self.headSHA = headSHA; self.pullRequest = pullRequest; self.ci = ci; self.acceptance = acceptance; self.mergedSHA = mergedSHA; self.deployedSHA = deployedSHA
    }

    public var isCompleted: Bool {
        headSHA?.isEmpty == false && pullRequest?.isEmpty == false && ci == .verified && acceptance == .verified && mergedSHA?.isEmpty == false && deployedSHA?.isEmpty == false
    }

    public var derivedStatus: TaskStatus {
        if isCompleted { return .completed }
        if headSHA == nil || pullRequest == nil { return .inProgress }
        if ci != .verified { return .awaitingCI }
        if acceptance != .verified { return .awaitingAcceptance }
        return .unknown
    }
}

public struct Task: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let blocker: String?
    public let evidence: DeliveryEvidence
}

public struct AgentLane: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let state: String
    public let heartbeatAgeSeconds: Int
}

public struct TimelineEvent: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let occurredAt: Date
    public let summary: String
    public let correlationID: String
}

public struct Snapshot: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let revision: String
    public let generatedAt: Date
    public let freshness: Freshness
    public let tasks: [Task]
    public let lanes: [AgentLane]
    public let events: [TimelineEvent]

    public init(schemaVersion: String, revision: String, generatedAt: Date, freshness: Freshness, tasks: [Task], lanes: [AgentLane], events: [TimelineEvent]) {
        self.schemaVersion = schemaVersion
        self.revision = revision
        self.generatedAt = generatedAt
        self.freshness = freshness
        self.tasks = tasks
        self.lanes = lanes
        self.events = events
    }

    public var overview: OverviewModel {
        OverviewModel(freshness: freshness, activeTasks: tasks.filter { $0.evidence.derivedStatus != .completed }.count, needsAttention: tasks.filter { $0.blocker != nil || $0.evidence.derivedStatus == .unknown }.count)
    }
}

public struct OverviewModel: Equatable, Sendable { public let freshness: Freshness; public let activeTasks: Int; public let needsAttention: Int }
public struct TasksModel: Equatable, Sendable { public let tasks: [Task] }
public struct AgentsModel: Equatable, Sendable { public let lanes: [AgentLane] }
public struct PipelineModel: Equatable, Sendable { public let tasks: [Task] }
public struct ActivityModel: Equatable, Sendable { public let events: [TimelineEvent] }

public enum SnapshotDecoder {
    public static func decode(_ data: Data) throws -> Snapshot {
        let text = String(decoding: data, as: UTF8.self).lowercased()
        let prohibited = ["authorization", "bearer ", "password", "ssh-rsa", "postgres://", "private_key", "prompt"]
        guard !prohibited.contains(where: text.contains) else { throw AICCNativeError.unsafeDTO }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(Snapshot.self, from: data)
    }
}

public enum AICCNativeError: Error, Equatable { case unsafeDTO }

public enum Fixture {
    public static func healthySnapshot() throws -> Snapshot {
        let url = try Bundle.module.url(forResource: "healthy-snapshot", withExtension: "json").unwrap()
        return try SnapshotDecoder.decode(Data(contentsOf: url))
    }
}

private extension Optional where Wrapped == URL {
    func unwrap() throws -> URL {
        guard let self else { throw CocoaError(.fileNoSuchFile) }
        return self
    }
}
