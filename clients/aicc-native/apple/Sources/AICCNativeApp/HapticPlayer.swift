import AICCNativeCore

#if os(iOS)
import UIKit

/// Plays the ambient haptic cues `HapticAdvisor` computes. Severity maps to
/// UIKit's system feedback vocabulary so critical incidents feel distinct
/// from routine workflow motion without borrowing custom (and easily
/// over-tuned) vibration patterns.
@MainActor
enum HapticPlayer {
    static func play(_ severity: IncidentSeverity) {
        switch severity {
        case .critical:
            UINotificationFeedbackGenerator().notificationOccurred(.error)
        case .warning:
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
        case .notice:
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        case .info:
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
        }
    }
}
#else
/// Non-iOS targets (macOS) have no Taptic Engine; ambient haptics are a no-op
/// there rather than an error, so the rest of the app stays platform-neutral.
@MainActor
enum HapticPlayer {
    static func play(_ severity: IncidentSeverity) {}
}
#endif
