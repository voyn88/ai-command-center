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

    init() {
        // Start from the owner's last real picture when we have one; the
        // demo fixture is only the very-first-launch fallback.
        if let cached = SnapshotCache.load() {
            snapshot = cached
            connection = .offline
        } else {
            snapshot = (try? Fixture.healthySnapshot()) ?? .preview
        }
    }

    /// Whether any device credential is available (env override or Keychain).
    var hasCredential: Bool {
        ProcessInfo.processInfo.environment["AICC_DEVICE_TOKEN"] != nil
            || DeviceTokenStore.load() != nil
    }

    /// Store the operator-issued device token in the Keychain and reconnect.
    /// The token text itself never touches UserDefaults, files or logs.
    func pair(token: String) async {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        DeviceTokenStore.save(trimmed)
        await refresh()
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
