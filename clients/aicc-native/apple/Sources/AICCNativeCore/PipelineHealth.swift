import Foundation

// Gateway DTO 1.0 camelCase keys. Missing, null or malformed additive fields
// remain unknown; unknown future keys are ignored. No missing count becomes zero.
public struct QueueHealth: Codable, Equatable, Sendable {
    public let ready: Int?
    public let claimed: Int?
    public let dead: Int?
    public let succeededLastHour: Int?
    public let oldestReadyAgeSeconds: Int?

    private enum CodingKeys: String, CodingKey { case ready, claimed, dead, succeededLastHour, oldestReadyAgeSeconds }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        ready = try? values.decodeIfPresent(Int.self, forKey: .ready)
        claimed = try? values.decodeIfPresent(Int.self, forKey: .claimed)
        dead = try? values.decodeIfPresent(Int.self, forKey: .dead)
        succeededLastHour = try? values.decodeIfPresent(Int.self, forKey: .succeededLastHour)
        oldestReadyAgeSeconds = try? values.decodeIfPresent(Int.self, forKey: .oldestReadyAgeSeconds)
    }
}

public struct PipelineLane: Codable, Equatable, Sendable {
    public let role: String?
    public let attempts: Int?
    public let heartbeatAgeSeconds: Int?

    private enum CodingKeys: String, CodingKey { case role, attempts, heartbeatAgeSeconds }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        role = try? values.decodeIfPresent(String.self, forKey: .role)
        attempts = try? values.decodeIfPresent(Int.self, forKey: .attempts)
        heartbeatAgeSeconds = try? values.decodeIfPresent(Int.self, forKey: .heartbeatAgeSeconds)
    }
}

public struct OwnerEscalation: Codable, Equatable, Sendable {
    public let taskID: String?
    public let title: String?
    public let priority: String?
    public let updatedAt: String?

    private enum CodingKeys: String, CodingKey { case taskID, title, priority, updatedAt }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        taskID = try? values.decodeIfPresent(String.self, forKey: .taskID)
        title = try? values.decodeIfPresent(String.self, forKey: .title)
        priority = try? values.decodeIfPresent(String.self, forKey: .priority)
        updatedAt = try? values.decodeIfPresent(String.self, forKey: .updatedAt)
    }
}

public struct PipelineMonitor: Codable, Equatable, Sendable {
    public let verdict: String?
    public let openFindings: Int?
    public let failures: [String]?
    public let lastSeenAt: String?

    private enum CodingKeys: String, CodingKey { case verdict, openFindings, failures, lastSeenAt }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        verdict = try? values.decodeIfPresent(String.self, forKey: .verdict)
        openFindings = try? values.decodeIfPresent(Int.self, forKey: .openFindings)
        failures = try? values.decodeIfPresent([String].self, forKey: .failures)
        lastSeenAt = try? values.decodeIfPresent(String.self, forKey: .lastSeenAt)
    }
}

public struct PipelineHealth: Codable, Equatable, Sendable {
    public let observedAt: String?
    public let queue: QueueHealth?
    public let activeLanes: [PipelineLane]?
    public let executorAuthFailures30m: Int?
    public let backlogByStatus: [String: Int]?
    public let ownerEscalations: [OwnerEscalation]?
    public let monitor: PipelineMonitor?

    private enum CodingKeys: String, CodingKey { case observedAt, queue, activeLanes, executorAuthFailures30m, backlogByStatus, ownerEscalations, monitor }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        observedAt = try? values.decodeIfPresent(String.self, forKey: .observedAt)
        queue = try? values.decodeIfPresent(QueueHealth.self, forKey: .queue)
        activeLanes = try? values.decodeIfPresent([PipelineLane].self, forKey: .activeLanes)
        executorAuthFailures30m = try? values.decodeIfPresent(Int.self, forKey: .executorAuthFailures30m)
        backlogByStatus = try? values.decodeIfPresent([String: Int].self, forKey: .backlogByStatus)
        ownerEscalations = try? values.decodeIfPresent([OwnerEscalation].self, forKey: .ownerEscalations)
        monitor = try? values.decodeIfPresent(PipelineMonitor.self, forKey: .monitor)
    }
}

public enum PipelineHealthState: String, Equatable, Sendable { case ok, attention, down }

public struct PipelineHealthViewModel: Equatable, Sendable {
    public let health: PipelineHealth

    public init(health: PipelineHealth) { self.health = health }

    /// Absence of enough evidence is nil, never a fabricated healthy/down state.
    /// A nonzero dead count alone is not growth: the gateway monitor reports
    /// growth as `dead_letter_growth`; all monitor failures require attention.
    public var state: PipelineHealthState? {
        if health.activeLanes?.isEmpty == true { return .down }
        if (health.executorAuthFailures30m ?? 0) > 0
            || health.monitor?.verdict == "failing"
            || (health.monitor?.openFindings ?? 0) > 0
            || health.monitor?.failures?.isEmpty == false
            || health.ownerEscalations?.isEmpty == false
            || (health.backlogByStatus?["DEFER_TO_USER"] ?? 0) > 0 {
            return .attention
        }
        guard health.activeLanes != nil,
              health.executorAuthFailures30m != nil,
              health.ownerEscalations != nil else { return nil }
        return .ok
    }

    /// Most urgent priority first, then oldest update, then ID. Unknown values
    /// sort last; exact ties retain server order, including incomplete entries.
    public var escalations: [OwnerEscalation] {
        (health.ownerEscalations ?? []).enumerated().sorted { lhs, rhs in
            let l = lhs.element, r = rhs.element
            let lp = Self.priorityRank(l.priority), rp = Self.priorityRank(r.priority)
            if lp != rp { return lp < rp }
            let ld = Self.date(l.updatedAt) ?? .distantFuture
            let rd = Self.date(r.updatedAt) ?? .distantFuture
            if ld != rd { return ld < rd }
            if l.taskID != r.taskID { return (l.taskID ?? "~") < (r.taskID ?? "~") }
            return lhs.offset < rhs.offset
        }.map(\.element)
    }

    private static func priorityRank(_ priority: String?) -> Int {
        guard let priority, priority.hasPrefix("P"),
              let rank = Int(priority.dropFirst()), rank >= 0 else { return Int.max }
        return rank
    }

    /// Gateway timestamps may include fractional seconds. Keep their exact wire
    /// strings in Codable so cache round trips do not change the gateway shape.
    public static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}
