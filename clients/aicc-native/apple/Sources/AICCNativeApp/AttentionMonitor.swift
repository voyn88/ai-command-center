import Foundation
import Combine
import AICCNativeCore

/// Bridges the app's live foreground/idle state into an `AttentionLevel` the
/// shell can react to. Reads only passive signals — never anything the owner
/// says or types.
@MainActor
final class AttentionMonitor: ObservableObject {
    @Published private(set) var level: AttentionLevel = .engaged

    private var lastInteractionAt = Date()
    private var isForeground = true
    private var ticker: AnyCancellable?

    init() {
        // Idle time only grows between interactions, so a periodic recompute
        // is enough to notice the threshold being crossed while nothing else
        // in the UI is changing.
        ticker = Timer.publish(every: 5, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in self?.recompute() }
    }

    var requiresSafeMode: Bool { level.requiresSafeMode }

    /// A tap, a tab change, or the explicit "resume" action all count as the
    /// owner being present again.
    func recordInteraction() {
        lastInteractionAt = Date()
        recompute()
    }

    func setForeground(_ foreground: Bool) {
        isForeground = foreground
        if foreground { lastInteractionAt = Date() }
        recompute()
    }

    private func recompute() {
        level = AttentionEvaluator.evaluate(
            AttentionSignals(isForeground: isForeground, idleSeconds: Date().timeIntervalSince(lastInteractionAt))
        )
    }
}
