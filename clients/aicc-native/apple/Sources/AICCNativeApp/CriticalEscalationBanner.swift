import SwiftUI
import AICCNativeCore

/// A crisis card sized for a single glance and a single action — the shape a
/// watch complication offers once a WatchKit/WidgetKit target exists
/// (docs/aicc_native/APPLE_RELEASE_READINESS.md tracks that gap). Tapping the
/// action never leaves the device: it records a local acknowledgement, not
/// an AIOS command.
struct CriticalEscalationBanner: View {
    let escalation: CriticalEscalation
    let onAcknowledge: () -> Void
    @State private var acknowledging = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("КРИТИЧНО", systemImage: "exclamationmark.triangle.fill")
                .font(.caption2.weight(.bold)).tracking(1.1).foregroundStyle(.red)
            Text(escalation.title).font(.title3.weight(.semibold))
            Text(escalation.summary).font(.subheadline).foregroundStyle(.secondary)
            Button {
                acknowledging = true
                onAcknowledge()
            } label: {
                Label(escalation.actionLabel, systemImage: "checkmark.circle.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
            .disabled(acknowledging)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 20))
        .overlay { RoundedRectangle(cornerRadius: 20).stroke(.red.opacity(0.35)) }
        .accessibilityElement(children: .combine)
    }
}

#Preview("Критическая эскалация") {
    CriticalEscalationBanner(
        escalation: CriticalEscalation(
            id: "ESC-9001",
            title: "Платежи: ошибки выросли до 42% за 5 минут",
            summary: "Похоже на сбой платёжного шлюза. Нужно подтвердить, что вы это видите — команда уже собирается.",
            occurredAt: .now,
            severity: .critical,
            actionLabel: "Вижу, подтверждаю"
        ),
        onAcknowledge: {}
    )
    .padding()
}
