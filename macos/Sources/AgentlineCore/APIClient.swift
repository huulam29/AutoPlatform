import Foundation

public enum APIError: Error, LocalizedError, Sendable, Equatable {
    /// The server answered with an error status; `detail` is FastAPI's `detail` when present.
    case http(status: Int, detail: String)
    /// No answer (engine stopped, crashed or still starting).
    case unreachable(String)
    case decoding(String)

    public var errorDescription: String? {
        switch self {
        case .http(let status, let detail): return detail.isEmpty ? "HTTP \(status)" : detail
        case .unreachable(let why): return "The engine isn't reachable (\(why))."
        case .decoding(let why): return "Unexpected answer from the engine: \(why)"
        }
    }

    public var status: Int? {
        if case .http(let s, _) = self { return s }
        return nil
    }

    /// The incident a 500 was recorded as (`{"detail": …, "incident": "<id>"}`), if any.
    public var incidentId: String? {
        guard case .http(_, let detail) = self, let r = detail.range(of: #"\[incident ([0-9a-f]{16})\]"#, options: .regularExpression)
        else { return nil }
        return String(detail[r].dropFirst(10).dropLast())
    }
}

/// The agentline HTTP API (`/api/*` with `Authorization: Bearer <token>`).
public final class APIClient: Sendable {
    public let baseURL: URL
    public let token: String?
    private let session: URLSession

    public init(baseURL: URL, token: String?, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.token = token
        if let session {
            self.session = session
        } else {
            let cfg = URLSessionConfiguration.ephemeral
            cfg.timeoutIntervalForRequest = 30
            cfg.waitsForConnectivity = false
            self.session = URLSession(configuration: cfg)
        }
    }

    // MARK: endpoints

    /// Any 200 from `GET /healthz` means healthy (the body is `{"status": "ok"}`).
    public func health() async throws -> Bool {
        let _: JSONValue = try await get("/healthz", timeout: 2)
        return true
    }

    public func status() async throws -> EngineStatus { try await get("/api/status") }

    public func runs(status: String? = nil, limit: Int = 100) async throws -> [RunSummary] {
        var q = [URLQueryItem(name: "limit", value: String(limit))]
        if let status, !status.isEmpty { q.append(URLQueryItem(name: "status", value: status)) }
        let r: RunsResponse = try await get("/api/runs", query: q)
        return r.runs
    }

    public func run(_ id: String) async throws -> RunDetail {
        try await get("/api/runs/\(Self.segment(id))")
    }

    public func approvals() async throws -> [Approval] { try await get("/api/approvals") }

    /// Retry the run's dead executions, or only `node` (dead or needs-review): a for_each node
    /// reruns only its failed tasks.
    public func retry(runId: String, node: String?) async throws {
        var b: [String: JSONValue] = [:]
        if let node { b["node"] = .string(node) }
        let _: JSONValue = try await send("POST", "/api/runs/\(Self.segment(runId))/retry", body: .object(b))
    }

    public func approve(runId: String, node: String?, comment: String?) async throws -> DecisionResult {
        try await send("POST", "/api/runs/\(Self.segment(runId))/approve", body: Self.decisionBody(node, comment))
    }

    public func reject(runId: String, node: String?, comment: String?) async throws -> DecisionResult {
        try await send("POST", "/api/runs/\(Self.segment(runId))/reject", body: Self.decisionBody(node, comment))
    }

    public func submit(pipeline: String, payload: JSONValue, dedupeKey: String?) async throws -> SubmitResult {
        var body: [String: JSONValue] = ["pipeline": .string(pipeline), "payload": payload]
        if let dedupeKey, !dedupeKey.isEmpty { body["dedupe_key"] = .string(dedupeKey) }
        return try await send("POST", "/api/runs", body: .object(body))
    }

    public func pipelines() async throws -> PipelinesResponse { try await get("/api/pipelines") }

    public func settings() async throws -> SettingsSnapshot { try await get("/api/settings") }

    public func putSetting(section: String, name: String, body: [String: JSONValue]) async throws -> SettingsSnapshot {
        try await send("PUT", "/api/settings/\(section)/\(Self.segment(name))", body: .object(body))
    }

    public func deleteSetting(section: String, name: String) async throws -> SettingsSnapshot {
        try await send("DELETE", "/api/settings/\(section)/\(Self.segment(name))", body: nil)
    }

    public func testProvider(_ name: String) async throws -> ProviderTestResult {
        try await send("POST", "/api/providers/\(Self.segment(name))/test", body: nil, timeout: 30)
    }

    /// Make the source's configured models exactly `ids` (adds missing, removes unchecked).
    public func chooseModels(provider: String, ids: [String]) async throws -> SettingsSnapshot {
        try await send(
            "PUT", "/api/providers/\(Self.segment(provider))/models",
            body: .object(["models": .array(ids.map { .string($0) })]))
    }

    public func testMcp(_ name: String) async throws -> McpTestResult {
        try await send("POST", "/api/mcp/\(Self.segment(name))/test", body: nil, timeout: 40)
    }

    public func detectAgent(_ name: String) async throws -> AgentDetectResult {
        try await send("POST", "/api/agents/\(Self.segment(name))/detect", body: nil, timeout: 30)
    }

    // MARK: incidents (crash and failure reports; the same files the app reads when the engine is down)

    public func incidents(limit: Int = 500) async throws -> IncidentsResponse {
        try await get("/api/incidents", query: [URLQueryItem(name: "limit", value: String(limit))])
    }

    public func incident(_ id: String) async throws -> Incident {
        try await get("/api/incidents/\(Self.segment(id))")
    }

    public func markIncidentSeen(_ id: String) async throws {
        let _: JSONValue = try await send("POST", "/api/incidents/\(Self.segment(id))/seen", body: nil)
    }

    public func deleteIncident(_ id: String) async throws {
        let _: JSONValue = try await send("DELETE", "/api/incidents/\(Self.segment(id))", body: nil)
    }

    public func clearIncidents() async throws {
        let _: JSONValue = try await send("DELETE", "/api/incidents", body: nil)
    }

    // MARK: chat links (Telegram)

    public func chatStatus() async throws -> ChatStatus {
        try await get("/api/chat", timeout: 20)
    }

    public func chatPair(_ platform: String) async throws -> ChatStatus {
        try await send("POST", "/api/chat/\(Self.segment(platform))/pair", body: nil, timeout: 20)
    }

    public func chatTest(_ platform: String) async throws {
        let _: JSONValue = try await send("POST", "/api/chat/\(Self.segment(platform))/test", body: nil, timeout: 20)
    }

    public func chatUnlink(_ platform: String) async throws -> ChatStatus {
        try await send("DELETE", "/api/chat/\(Self.segment(platform))", body: nil)
    }

    public func chatNotify(_ n: ChatStatus.Notify) async throws -> ChatStatus {
        try await send("PUT", "/api/chat/notify", body: .object([
            "gates": .bool(n.gates), "failures": .bool(n.failures),
            "done": .bool(n.done), "questions": .bool(n.questions),
        ]))
    }

    // MARK: issues (one per problem, with a status)

    /// Syncs first: every incident and failed run gets an issue.
    public func issues() async throws -> IssuesResponse {
        try await get("/api/issues")
    }

    public func updateIssue(_ number: Int, status: String? = nil, note: String? = nil) async throws -> Issue {
        var b: [String: JSONValue] = [:]
        if let status { b["status"] = .string(status) }
        if let note, !note.isEmpty { b["note"] = .string(note) }
        return try await send("PATCH", "/api/issues/\(number)", body: .object(b))
    }

    // MARK: plumbing

    static func decisionBody(_ node: String?, _ comment: String?) -> JSONValue {
        var b: [String: JSONValue] = [:]
        if let node, !node.isEmpty { b["node"] = .string(node) }
        if let comment, !comment.isEmpty { b["comment"] = .string(comment) }
        return .object(b)
    }

    static func segment(_ s: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }

    public func request(_ method: String, _ path: String, query: [URLQueryItem] = [], body: JSONValue? = nil, timeout: TimeInterval = 15) -> URLRequest {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        comps.percentEncodedPath = path
        comps.queryItems = query.isEmpty ? nil : query
        var req = URLRequest(url: comps.url!, timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token, !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONEncoder().encode(body)
        }
        return req
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], timeout: TimeInterval = 15) async throws -> T {
        try await perform(request("GET", path, query: query, timeout: timeout))
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: JSONValue?, timeout: TimeInterval = 15) async throws -> T {
        try await perform(request(method, path, body: body, timeout: timeout))
    }

    private func perform<T: Decodable>(_ req: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError.unreachable((error as NSError).localizedDescription)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw APIError.http(status: status, detail: Self.detail(from: data))
        }
        return try Self.decode(T.self, from: data)
    }

    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }

    /// FastAPI errors: `{"detail": "..."}` or `{"detail": [{"msg": ...}]}`.
    static func detail(from data: Data) -> String {
        guard let v = try? JSONDecoder().decode(JSONValue.self, from: data) else {
            return String(decoding: data.prefix(300), as: UTF8.self)
        }
        if let s = v["detail"]?.stringValue {
            // A 500 the engine recorded: keep the incident id so the app can link to the report.
            if let id = v["incident"]?.stringValue, IncidentStore.isValidId(id) { return "\(s) [incident \(id)]" }
            return s
        }
        if let items = v["detail"]?.arrayValue {
            return items.compactMap { $0["msg"]?.stringValue }.joined(separator: "; ")
        }
        return v.compactText
    }
}
