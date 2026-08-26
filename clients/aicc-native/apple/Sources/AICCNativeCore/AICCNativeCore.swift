import Foundation
import Security

public enum EvidenceState: String, Codable, Sendable { case unknown, observed, verified, rejected, pending }
public enum Freshness: String, Codable, Sendable { case fresh, stale, offline, degraded }
public enum TaskStatus: String, Codable, Sendable { case inProgress = "in_progress", awaitingCI = "awaiting_ci", awaitingAcceptance = "awaiting_acceptance", completed, unknown }

public struct DeliveryEvidence: Codable, Equatable, Sendable {
    public let headSHA: String?
    public let pullRequest: String?
    public let ci: EvidenceState
    public let acceptance: EvidenceState
    public let mergedSHA: String?
    public let deployedSHA: String?

    public init(headSHA: String?, pullRequest: String?, ci: EvidenceState, acceptance: EvidenceState, mergedSHA: String?, deployedSHA: String?) {
        self.headSHA = headSHA; self.pullRequest = pullRequest; self.ci = ci; self.acceptance = acceptance; self.mergedSHA = mergedSHA; self.deployedSHA = deployedSHA
    }

    public var isCompleted: Bool {
        headSHA?.isEmpty == false && pullRequest?.isEmpty == false && ci == .verified && acceptance == .verified && mergedSHA?.isEmpty == false && deployedSHA?.isEmpty == false
    }

    public var derivedStatus: TaskStatus {
        if isCompleted { return .completed }
        if headSHA == nil || pullRequest == nil { return .inProgress }
        if ci != .verified { return .awaitingCI }
        if acceptance != .verified { return .awaitingAcceptance }
        return .unknown
    }
}

public struct Task: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let blocker: String?
    public let evidence: DeliveryEvidence
}

public struct AgentLane: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let state: String
    public let heartbeatAgeSeconds: Int
}

public struct TimelineEvent: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let occurredAt: Date
    public let summary: String
    public let correlationID: String
}

public struct Project: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let state: String
    public let activeTasks: Int
    public let needsAttention: Int

    public init(id: String, name: String, state: String, activeTasks: Int, needsAttention: Int) {
        self.id = id; self.name = name; self.state = state; self.activeTasks = activeTasks; self.needsAttention = needsAttention
    }
}

public struct Snapshot: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let revision: String
    public let generatedAt: Date
    public let freshness: Freshness
    public let tasks: [Task]
    public let lanes: [AgentLane]
    public let events: [TimelineEvent]
    // DTO 1.0 additive keys: absent in old fixtures, so they decode with
    // defaults instead of failing (the server always sends them).
    public let projects: [Project]

    public init(schemaVersion: String, revision: String, generatedAt: Date, freshness: Freshness, tasks: [Task], lanes: [AgentLane], events: [TimelineEvent], projects: [Project] = []) {
        self.schemaVersion = schemaVersion
        self.revision = revision
        self.generatedAt = generatedAt
        self.freshness = freshness
        self.tasks = tasks
        self.lanes = lanes
        self.events = events
        self.projects = projects
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decode(String.self, forKey: .schemaVersion)
        revision = try container.decode(String.self, forKey: .revision)
        generatedAt = try container.decode(Date.self, forKey: .generatedAt)
        freshness = try container.decode(Freshness.self, forKey: .freshness)
        tasks = try container.decode([Task].self, forKey: .tasks)
        lanes = try container.decode([AgentLane].self, forKey: .lanes)
        events = try container.decode([TimelineEvent].self, forKey: .events)
        projects = try container.decodeIfPresent([Project].self, forKey: .projects) ?? []
    }

    public var overview: OverviewModel {
        OverviewModel(freshness: freshness, activeTasks: tasks.filter { $0.evidence.derivedStatus != .completed }.count, needsAttention: tasks.filter { $0.blocker != nil || $0.evidence.derivedStatus == .unknown }.count)
    }
}

public struct OverviewModel: Equatable, Sendable { public let freshness: Freshness; public let activeTasks: Int; public let needsAttention: Int }
public struct TasksModel: Equatable, Sendable { public let tasks: [Task] }
public struct AgentsModel: Equatable, Sendable { public let lanes: [AgentLane] }
public struct PipelineModel: Equatable, Sendable { public let tasks: [Task] }
public struct ActivityModel: Equatable, Sendable { public let events: [TimelineEvent] }

public enum SnapshotDecoder {
    public static func decode(_ data: Data) throws -> Snapshot {
        let text = String(decoding: data, as: UTF8.self).lowercased()
        let prohibited = ["authorization", "bearer ", "password", "ssh-rsa", "postgres://", "private_key", "prompt"]
        guard !prohibited.contains(where: text.contains) else { throw AICCNativeError.unsafeDTO }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(Snapshot.self, from: data)
    }
}

public enum AICCNativeError: Error, Equatable { case unsafeDTO }

public struct GatewayConfiguration: Equatable, Sendable {
    public let baseURL: URL
    public let expectedSchemaVersion: String
    /// Bearer device token minted by the gateway operator (`native_gateway.provision`).
    /// Stored in the Keychain on device (`DeviceTokenStore`); never in Info.plist.
    public let deviceToken: String?
    /// Optional exact DER pins for the gateway's server certificate. When
    /// non-empty, TLS succeeds only for a matching leaf certificate — this is
    /// what makes a self-signed development certificate usable without ever
    /// weakening trust to "accept anything".
    public let pinnedServerCertificates: [Data]

    public init(
        baseURL: URL,
        expectedSchemaVersion: String = "1.0",
        deviceToken: String? = nil,
        pinnedServerCertificates: [Data] = []
    ) throws {
        guard baseURL.scheme?.lowercased() == "https", baseURL.host != nil else { throw GatewayError.insecureEndpoint }
        self.baseURL = baseURL
        self.expectedSchemaVersion = expectedSchemaVersion
        self.deviceToken = deviceToken
        self.pinnedServerCertificates = pinnedServerCertificates
    }
}

public enum GatewayError: Error, Equatable {
    case insecureEndpoint, invalidResponse, unexpectedStatus(Int), schemaMismatch, notModified, unauthorized
}

/// Accepts a server trust only when its leaf certificate byte-matches one of
/// the configured DER pins; with no pins configured it defers to system trust.
final class PinnedCertificateSessionDelegate: NSObject, URLSessionDelegate {
    private let pins: [Data]

    init(pins: [Data]) { self.pins = pins }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust
        else { return completionHandler(.performDefaultHandling, nil) }
        guard !pins.isEmpty else { return completionHandler(.performDefaultHandling, nil) }
        let chain = (SecTrustCopyCertificateChain(trust) as? [SecCertificate]) ?? []
        guard let leaf = chain.first else { return completionHandler(.cancelAuthenticationChallenge, nil) }
        let leafData = SecCertificateCopyData(leaf) as Data
        if pins.contains(leafData) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

public struct SnapshotRemoteStore: Sendable {
    private let configuration: GatewayConfiguration
    private let session: URLSession

    public init(configuration: GatewayConfiguration, session: URLSession? = nil) {
        self.configuration = configuration
        if let session {
            self.session = session
        } else if configuration.pinnedServerCertificates.isEmpty {
            self.session = .shared
        } else {
            self.session = URLSession(
                configuration: .ephemeral,
                delegate: PinnedCertificateSessionDelegate(pins: configuration.pinnedServerCertificates),
                delegateQueue: nil
            )
        }
    }

    public static func request(configuration: GatewayConfiguration, revision: String? = nil) -> URLRequest {
        let endpoint = configuration.baseURL.appending(path: "v1/snapshot")
        var request = URLRequest(url: endpoint)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("1.0", forHTTPHeaderField: "X-AICC-Client-Version")
        if let token = configuration.deviceToken, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let revision, !revision.isEmpty { request.setValue(revision, forHTTPHeaderField: "If-None-Match") }
        return request
    }

    public func fetchSnapshot(revision: String? = nil) async throws -> Snapshot {
        let request = Self.request(configuration: configuration, revision: revision)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw GatewayError.invalidResponse }
        if http.statusCode == 304 { throw GatewayError.notModified }
        if http.statusCode == 401 { throw GatewayError.unauthorized }
        guard (200...299).contains(http.statusCode) else { throw GatewayError.unexpectedStatus(http.statusCode) }
        let snapshot = try SnapshotDecoder.decode(data)
        guard snapshot.schemaVersion == configuration.expectedSchemaVersion else { throw GatewayError.schemaMismatch }
        return snapshot
    }
}

/// Keychain-backed storage for the device token: the token is provisioned
/// once by the operator and lives only in the device Keychain (never in
/// Info.plist, UserDefaults or source control).
public enum DeviceTokenStore {
    static let service = "aicc.native.gateway"
    static let account = "device-token"

    public static func load() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    public static func save(_ token: String) -> Bool {
        let attributes: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: Data(token.utf8),
        ]
        SecItemDelete(attributes as CFDictionary)
        return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
    }

    @discardableResult
    public static func delete() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        return SecItemDelete(query as CFDictionary) == errSecSuccess
    }
}

public enum Fixture {
    public static func healthySnapshot() throws -> Snapshot {
        let url = try Bundle.module.url(forResource: "healthy-snapshot", withExtension: "json").unwrap()
        return try SnapshotDecoder.decode(Data(contentsOf: url))
    }
}

private extension Optional where Wrapped == URL {
    func unwrap() throws -> URL {
        guard let self else { throw CocoaError(.fileNoSuchFile) }
        return self
    }
}
