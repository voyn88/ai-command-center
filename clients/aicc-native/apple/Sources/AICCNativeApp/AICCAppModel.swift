import Foundation
import SwiftUI
import AICCNativeCore

@MainActor
final class AICCAppModel: ObservableObject {
    enum ConnectionState: Equatable {
        case fixture
        case connecting
        case live
        case offline
        case unauthorized

        var title: String {
            switch self {
            case .fixture: "Готово к подключению"
            case .connecting: "Обновляю картину"
            case .live: "Данные с сервера актуальны"
            case .offline: "Показана последняя доступная картина"
            case .unauthorized: "Требуется вход: добавьте токен устройства"
            }
        }
    }

    @Published private(set) var snapshot: Snapshot
    @Published private(set) var dialogs: [DialogSummary] = []
    @Published private(set) var connection: ConnectionState = .fixture
    /// Owner-facing flag: optional hardware binding for credential
    /// operations. Persisted locally (not sensitive) so the choice survives
    /// relaunch; the actual secret is never stored here.
    @Published private(set) var biometricLockEnabled: Bool
    /// Set when a critical action (pairing/unpairing) was refused because
    /// biometric authorization failed or was unavailable, so the UI can
    /// surface it without a crash or silent no-op.
    @Published private(set) var lastCriticalActionDenied = false

    /// Gates pairing/unpairing behind Face ID / Touch ID + a live confirmed
    /// session when `biometricLockEnabled` is on. Disabled by default so the
    /// biometric requirement is strictly opt-in, per the task.
    private let criticalActionGuard: CriticalActionGuard
    private static let biometricLockDefaultsKey = "AICCBiometricLockEnabled"

    init(biometricAuthenticator: BiometricAuthenticating = DeviceBiometricAuthenticator()) {
        // Start from the owner's last real picture when we have one; the
        // demo fixture is only the very-first-launch fallback.
        if let cached = SnapshotCache.load() {
            snapshot = cached
            connection = .offline
        } else {
            snapshot = (try? Fixture.healthySnapshot()) ?? .preview
        }
        let enabled = UserDefaults.standard.bool(forKey: Self.biometricLockDefaultsKey)
        biometricLockEnabled = enabled
        criticalActionGuard = CriticalActionGuard(
            authenticator: biometricAuthenticator,
            policy: enabled ? .required : .disabled
        )
    }

    /// Whether any device credential is available (env override or Keychain).
    var hasCredential: Bool {
        ProcessInfo.processInfo.environment["AICC_DEVICE_TOKEN"] != nil
            || DeviceTokenStore.load() != nil
    }

    /// Turns the optional Secure Enclave / Face ID binding on or off for
    /// critical, credential-affecting actions. Re-saves any existing token
    /// under the matching Keychain protection so the hardware binding
    /// actually covers the stored secret, not just the in-app flow.
    func setBiometricLock(enabled: Bool) async {
        biometricLockEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: Self.biometricLockDefaultsKey)
        await criticalActionGuard.setPolicy(enabled ? .required : .disabled)
        if let existing = DeviceTokenStore.load() {
            DeviceTokenStore.save(existing, protectedByBiometrics: enabled)
        }
    }

    /// Store the operator-issued device token in the Keychain and reconnect.
    /// The token text itself never touches UserDefaults, files or logs. When
    /// the owner opted into the biometric lock, this is a critical action:
    /// it requires a fresh Face ID / Touch ID confirmation (or a still-live
    /// confirmed session) before the Keychain write happens.
    func pair(token: String) async {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        lastCriticalActionDenied = false
        do {
            try await criticalActionGuard.perform(reason: "Подтвердите подключение устройства") {
                DeviceTokenStore.save(trimmed, protectedByBiometrics: self.biometricLockEnabled)
            }
        } catch {
            lastCriticalActionDenied = true
            return
        }
        await refresh()
    }

    /// Removes the stored device credential and cached snapshot. Critical
    /// action: same biometric + session-confirmation gate as pairing.
    func unpair() async {
        lastCriticalActionDenied = false
        do {
            try await criticalActionGuard.perform(reason: "Подтвердите удаление устройства") {
                DeviceTokenStore.delete()
                SnapshotCache.clear()
            }
        } catch {
            lastCriticalActionDenied = true
            return
        }
        connection = .unauthorized
    }

    func refresh() async {
        // Server URL is injected at release time (AICC_SERVER_URL). The device
        // token is provisioned by the operator and lives in the Keychain; the
        // environment override exists for development runs only. An optional
        // bundled DER pin (AICCGatewayPin.der) locks TLS to the gateway's
        // exact certificate — required for self-signed development gateways.
        let environment = ProcessInfo.processInfo.environment
        let rawURL = environment["AICC_SERVER_URL"]
            ?? (Bundle.main.object(forInfoDictionaryKey: "AICCServerURL") as? String)
        let token = environment["AICC_DEVICE_TOKEN"] ?? DeviceTokenStore.load()
        // Pin sources, most explicit first: dev env path, bundled resource,
        // then a base64 Info.plist value injected at build time
        // (AICC_GATEWAY_PIN_B64) — the path that works on a real device.
        let pin = environment["AICC_GATEWAY_PIN_FILE"]
            .flatMap { try? Data(contentsOf: URL(fileURLWithPath: $0)) }
            ?? Bundle.main.url(forResource: "AICCGatewayPin", withExtension: "der")
                .flatMap { try? Data(contentsOf: $0) }
            ?? (Bundle.main.object(forInfoDictionaryKey: "AICCGatewayPinB64") as? String)
                .flatMap { Data(base64Encoded: $0) }
        guard
            let rawURL,
            let url = URL(string: rawURL),
            let configuration = try? GatewayConfiguration(
                baseURL: url,
                deviceToken: token,
                pinnedServerCertificates: pin.map { [$0] } ?? []
            )
        else { return }

        connection = .connecting
        let store = SnapshotRemoteStore(configuration: configuration)
        do {
            snapshot = try await store.fetchSnapshot(revision: snapshot.revision)
            connection = .live
            SnapshotCache.save(snapshot)
        } catch GatewayError.notModified {
            connection = .live
        } catch GatewayError.unauthorized {
            connection = .unauthorized
        } catch {
            connection = .offline
        }
        // Secondary, best-effort: dialog summaries. Their absence must never
        // degrade the primary snapshot state.
        if connection == .live {
            dialogs = (try? await store.fetchDialogs()) ?? dialogs
        }
    }
}
