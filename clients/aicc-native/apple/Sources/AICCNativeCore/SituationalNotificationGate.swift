import Foundation

// MARK: - VOYN-IOS-CONTEXT-APP
//
// Accounts for the owner's location / time-of-day / activity when deciding
// whether to actually deliver a push notification. The goal (per the
// backlog acceptance criteria) is to cut routine/noisy push volume by ~30%
// without ever silencing a push the owner still needs to act on.
//
// Per "не на всех этапах" ("not at every stage"), situational filtering is
// intentionally scoped: it only ever applies to `.routine` notifications
// that opt in via `contextAware`. `.important` and `.critical` pushes — the
// ones acceptance conversion is measured against — always bypass the gate.

/// The owner's inferred physical activity. This module never touches raw
/// CoreMotion/CoreLocation sensor data — it only consumes the on-device
/// classification, which keeps the policy host-agnostic and unit-testable.
public enum UserActivityState: String, Codable, Sendable {
    case stationary, walking, driving, sleeping, unknown
}

/// Coarse relevance of the owner's current location to a notification's
/// subject matter (e.g. "at the relevant place" vs. "away"). Never carries
/// raw coordinates — only the classification an on-device geofencing layer
/// already produced.
public enum LocationRelevance: String, Codable, Sendable {
    case atRelevantPlace, awayFromRelevantPlace, unknown
}

/// A momentary read of location + activity + time-of-day, used to decide
/// whether a routine notification should be delivered now or suppressed.
public struct SituationalContext: Equatable, Sendable {
    public let timestamp: Date
    public let activity: UserActivityState
    public let location: LocationRelevance
    /// Local quiet-hours window, hour-of-day, inclusive start / exclusive
    /// end (e.g. 22...8 means 22:00 through 07:59). Equal bounds disable
    /// the quiet-hours check entirely.
    public let quietHoursStart: Int
    public let quietHoursEnd: Int
    public let calendar: Calendar

    public init(
        timestamp: Date,
        activity: UserActivityState,
        location: LocationRelevance,
        quietHoursStart: Int = 22,
        quietHoursEnd: Int = 8,
        calendar: Calendar = .current
    ) {
        self.timestamp = timestamp
        self.activity = activity
        self.location = location
        self.quietHoursStart = quietHoursStart
        self.quietHoursEnd = quietHoursEnd
        self.calendar = calendar
    }

    public var isQuietHours: Bool {
        guard quietHoursStart != quietHoursEnd else { return false }
        let hour = calendar.component(.hour, from: timestamp)
        if quietHoursStart < quietHoursEnd {
            return hour >= quietHoursStart && hour < quietHoursEnd
        }
        // Window wraps past midnight (e.g. 22 -> 8).
        return hour >= quietHoursStart || hour < quietHoursEnd
    }
}

/// How much the owner's outcome depends on this notification. Only
/// `.routine` is ever eligible for situational suppression.
public enum NotificationImportance: String, Codable, Sendable {
    /// Safety/security/account-critical — always delivered.
    case critical
    /// Requires owner action to keep a task/process moving — always
    /// delivered so conversion on important actions never regresses.
    case important
    /// Informational nudge — the only tier context filtering applies to.
    case routine
}

/// A push candidate evaluated by the gate before dispatch.
public struct NotificationCandidate: Equatable, Sendable {
    public let id: String
    public let importance: NotificationImportance
    /// Opts this specific notification kind into situational filtering.
    /// Defaults to `true` for `.routine`; kinds that must always fire
    /// (e.g. a one-time digest) can set this to `false` to bypass the gate
    /// even while `.routine` — this is the "not at every stage" knob.
    public let contextAware: Bool

    public init(id: String, importance: NotificationImportance, contextAware: Bool = true) {
        self.id = id
        self.importance = importance
        self.contextAware = contextAware
    }
}

public enum NotificationDecision: Equatable, Sendable {
    case deliver
    case suppress(reason: String)

    public var isDelivered: Bool { if case .deliver = self { return true }; return false }
}

/// Decides whether to deliver a notification now, using location/time/
/// activity context. `.important` and `.critical` notifications always
/// bypass this gate; only `.routine` + `contextAware` candidates are
/// actually filtered.
public enum SituationalNotificationGate {
    public static func decide(_ candidate: NotificationCandidate, context: SituationalContext) -> NotificationDecision {
        guard candidate.importance == .routine, candidate.contextAware else { return .deliver }
        if context.activity == .sleeping { return .suppress(reason: "user asleep") }
        if context.activity == .driving { return .suppress(reason: "user driving") }
        if context.isQuietHours { return .suppress(reason: "quiet hours") }
        if context.location == .awayFromRelevantPlace { return .suppress(reason: "away from relevant place") }
        return .deliver
    }

    /// Evaluates a batch and returns per-candidate decisions alongside the
    /// aggregate stats used to prove the acceptance criteria.
    public static func evaluate(
        _ candidates: [NotificationCandidate], context: SituationalContext
    ) -> (decisions: [NotificationDecision], stats: PushDeliveryStats) {
        var stats = PushDeliveryStats()
        var decisions: [NotificationDecision] = []
        decisions.reserveCapacity(candidates.count)
        for candidate in candidates {
            let decision = decide(candidate, context: context)
            stats.record(candidate, decision: decision)
            decisions.append(decision)
        }
        return (decisions, stats)
    }
}

/// Aggregate counters exposed for the acceptance metric: ≥30% fewer routine
/// pushes suppressed by context, with zero drop in delivered important-or-
/// above actions (conversion).
public struct PushDeliveryStats: Equatable, Sendable {
    public private(set) var delivered: Int = 0
    public private(set) var suppressed: Int = 0
    public private(set) var deliveredImportantOrAbove: Int = 0
    public private(set) var totalImportantOrAbove: Int = 0

    public init() {}

    public mutating func record(_ candidate: NotificationCandidate, decision: NotificationDecision) {
        if candidate.importance != .routine {
            totalImportantOrAbove += 1
            if decision.isDelivered { deliveredImportantOrAbove += 1 }
        }
        if decision.isDelivered { delivered += 1 } else { suppressed += 1 }
    }

    /// Fraction of all evaluated traffic suppressed by situational filtering.
    public var suppressionRate: Double {
        let total = delivered + suppressed
        return total > 0 ? Double(suppressed) / Double(total) : 0
    }

    /// Conversion proxy: share of important-or-above notifications that
    /// still reached the owner despite context filtering. Must stay 1.0.
    public var importantConversionRate: Double {
        totalImportantOrAbove > 0 ? Double(deliveredImportantOrAbove) / Double(totalImportantOrAbove) : 1.0
    }
}
