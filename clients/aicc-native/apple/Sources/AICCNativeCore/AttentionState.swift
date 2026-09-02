import Foundation

/// How present the owner currently is with the app, inferred only from
/// passive signals (is the app in front of them, how long since they last
/// touched it) — never from anything they have to say or type. A silent
/// drop in engagement is itself the signal the nonverbal audit cares about.
public enum AttentionLevel: String, Codable, Hashable, Sendable {
    case engaged
    case reduced
    case minimal

    /// Anything short of full engagement steps the interface back to the
    /// safe, low-demand layout.
    public var requiresSafeMode: Bool { self != .engaged }
}

/// The passive signals the client can observe without asking the owner
/// anything.
public struct AttentionSignals: Equatable, Sendable {
    public let isForeground: Bool
    public let idleSeconds: TimeInterval

    public init(isForeground: Bool, idleSeconds: TimeInterval) {
        self.isForeground = isForeground
        self.idleSeconds = idleSeconds
    }
}

public enum AttentionEvaluator {
    /// Foreground idle time before the UI eases into the reduced layout.
    public static let reducedIdleThreshold: TimeInterval = 20
    /// Foreground idle time before the UI falls all the way back to the
    /// same safe minimal layout used while backgrounded.
    public static let minimalIdleThreshold: TimeInterval = 60

    public static func evaluate(_ signals: AttentionSignals) -> AttentionLevel {
        guard signals.isForeground else { return .minimal }
        if signals.idleSeconds >= minimalIdleThreshold { return .minimal }
        if signals.idleSeconds >= reducedIdleThreshold { return .reduced }
        return .engaged
    }
}
