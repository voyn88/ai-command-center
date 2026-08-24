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

        var title: String {
            switch self {
            case .fixture: "Готово к подключению"
            case .connecting: "Обновляю картину"
            case .live: "Данные с сервера актуальны"
            case .offline: "Показана последняя доступная картина"
            }
        }
    }

    @Published private(set) var snapshot: Snapshot
    @Published private(set) var connection: ConnectionState = .fixture

    init() {
        snapshot = (try? Fixture.healthySnapshot()) ?? .preview
    }

    func refresh() async {
        guard
            let rawURL = Bundle.main.object(forInfoDictionaryKey: "AICCServerURL") as? String,
            let url = URL(string: rawURL),
            let configuration = try? GatewayConfiguration(baseURL: url)
        else { return }

        connection = .connecting
        do {
            snapshot = try await SnapshotRemoteStore(configuration: configuration).fetchSnapshot(revision: snapshot.revision)
            connection = .live
        } catch GatewayError.notModified {
            connection = .live
        } catch {
            connection = .offline
        }
    }
}
