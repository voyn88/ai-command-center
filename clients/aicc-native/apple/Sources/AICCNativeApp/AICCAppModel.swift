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
    @Published private(set) var connection: ConnectionState = .fixture

    init() {
        snapshot = (try? Fixture.healthySnapshot()) ?? .preview
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
        let pin = environment["AICC_GATEWAY_PIN_FILE"]
            .flatMap { try? Data(contentsOf: URL(fileURLWithPath: $0)) }
            ?? Bundle.main.url(forResource: "AICCGatewayPin", withExtension: "der")
                .flatMap { try? Data(contentsOf: $0) }
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
        do {
            snapshot = try await SnapshotRemoteStore(configuration: configuration).fetchSnapshot(revision: snapshot.revision)
            connection = .live
        } catch GatewayError.notModified {
            connection = .live
        } catch GatewayError.unauthorized {
            connection = .unauthorized
        } catch {
            connection = .offline
        }
    }
}
