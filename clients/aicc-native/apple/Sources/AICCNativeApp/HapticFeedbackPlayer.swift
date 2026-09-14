import Foundation
import AICCNativeCore

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// Plays a `HapticPattern` as a short burst of platform feedback, so a
/// critical attention item is felt as a distinctly longer, heavier signal
/// than a routine one instead of a single generic buzz.
@MainActor
final class HapticFeedbackPlayer {
    static let shared = HapticFeedbackPlayer()

    private init() {}

    func play(_ pattern: HapticPattern) {
        _Concurrency.Task { @MainActor in
            for (index, pulse) in pattern.pulses.enumerated() {
                if index > 0, pattern.gapSeconds > 0 {
                    try? await _Concurrency.Task.sleep(nanoseconds: UInt64(pattern.gapSeconds * 1_000_000_000))
                }
                Self.fire(pulse)
            }
        }
    }

    private static func fire(_ pulse: HapticPulse) {
        #if os(iOS)
        switch pulse {
        case .light: UIImpactFeedbackGenerator(style: .light).impactOccurred()
        case .medium: UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        case .heavy: UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
        case .error: UINotificationFeedbackGenerator().notificationOccurred(.error)
        }
        #elseif os(macOS)
        // Trackpad haptics only: the closest match per pulse weight since
        // macOS has no impact/notification vocabulary of its own.
        let performer = NSHapticFeedbackManager.defaultPerformer
        switch pulse {
        case .light: performer.perform(.alignment, performanceTime: .default)
        case .medium: performer.perform(.generic, performanceTime: .default)
        case .heavy, .error: performer.perform(.levelChange, performanceTime: .default)
        }
        #endif
    }
}
