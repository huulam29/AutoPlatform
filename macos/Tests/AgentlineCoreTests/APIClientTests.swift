import Foundation
import XCTest

@testable import AgentlineCore

func fixture(_ name: String) throws -> Data {
    let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

/// Answers requests from a table and records them (headers, bodies).
final class StubProtocol: URLProtocol, @unchecked Sendable {
    struct Reply: Sendable {
        var status: Int
        var body: Data
    }

    nonisolated(unsafe) static var replies: [String: Reply] = [:]
    nonisolated(unsafe) static var seen: [URLRequest] = []
    static let lock = NSLock()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var req = request
        if req.httpBody == nil, let stream = req.httpBodyStream {
            stream.open()
            var data = Data()
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                data.append(buf, count: n)
            }
            stream.close()
            req.httpBody = data
        }
        let key = "\(req.httpMethod ?? "GET") \(req.url!.path)"
        Self.lock.lock()
        Self.seen.append(req)
        let reply = Self.replies[key] ?? Reply(status: 404, body: Data(#"{"detail":"no stub"}"#.utf8))
        Self.lock.unlock()
        let resp = HTTPURLResponse(url: req.url!, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: reply.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class APIClientTests: XCTestCase {
    var client: APIClient!

    override func setUp() {
        StubProtocol.replies = [:]
        StubProtocol.seen = []
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [StubProtocol.self]
        client = APIClient(baseURL: URL(string: "http://127.0.0.1:5555")!, token: "tok123", session: URLSession(configuration: cfg))
    }

    func stub(_ key: String, _ file: String, status: Int = 200) throws {
        StubProtocol.replies[key] = .init(status: status, body: try fixture(file))
    }

    func testRunsDecodeAndSendBearer() async throws {
        try stub("GET /api/runs", "runs")
        let runs = try await client.runs(status: "waiting", limit: 20)
        XCTAssertEqual(runs.map(\.id), ["r-2", "r-1"])
        XCTAssertEqual(runs[0].ticketKey, "PROJ-42")
        XCTAssertEqual(runs[1].ticketKey, "OPS-7")  // issue.key
        XCTAssertEqual(runs[0].budgetUsd, 5.0)
        XCTAssertEqual(runs[0].createdAt?.date.timeIntervalSince1970, 1760000000.5)
        XCTAssertEqual(runs[0].runStatus, .waiting)
        // payload keys are kept as they are (no snake-case conversion)
        XCTAssertEqual(runs[0].payload?["note_text"]?.stringValue, "x_y")
        let req = try XCTUnwrap(StubProtocol.seen.first)
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok123")
        let q = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
        XCTAssertTrue(q.contains(URLQueryItem(name: "status", value: "waiting")))
        XCTAssertTrue(q.contains(URLQueryItem(name: "limit", value: "20")))
    }

    func testRunDetail() async throws {
        try stub("GET /api/runs/r-2", "run_detail")
        let d = try await client.run("r-2")
        XCTAssertEqual(d.executions.count, 2)
        XCTAssertEqual(d.waitingGates.map(\.nodeId), ["approve_deploy"])
        XCTAssertEqual(d.waitingGates[0].gatePrompt, "Deploy PROJ-42 to staging?")
        XCTAssertEqual(d.waitingGates[0].approvers, ["Huu Lam"])
        XCTAssertEqual(d.executions[0].label, "plan#1")
        XCTAssertEqual(d.nextCommands.count, 2)
        XCTAssertNil(d.nextCommands[1].node)
        XCTAssertEqual(d.pipeline?.name, "ticket-to-eod.yaml")
    }

    func testApprovals() async throws {
        try stub("GET /api/approvals", "approvals")
        let a = try await client.approvals()
        XCTAssertEqual(a.count, 1)
        XCTAssertEqual(a[0].id, "x2")
        XCTAssertEqual(a[0].ticketKey, "PROJ-42")
        XCTAssertEqual(a[0].waitingSince?.date.timeIntervalSince1970, 1760000052.0)
    }

    func testStatusBothShapes() async throws {
        try stub("GET /api/status", "status")
        let s = try await client.status()
        XCTAssertEqual(s.keysManaged, "keychain")
        XCTAssertEqual(s.jiraPoll?.enabled, false)
        XCTAssertNil(s.jiraPoll?.lastPollAt)
        XCTAssertEqual(s.pipelines, ["mixed-agents"])
        let p = try APIClient.decode(EngineStatus.self, from: fixture("status_polling"))
        XCTAssertEqual(p.jiraPoll?.everyS, 300)
        XCTAssertEqual(p.jiraPoll?.lastPollAt?.date.timeIntervalSince1970, 1760000000.25)
        XCTAssertEqual(p.jiraPoll?.lastError, "HTTP 401 from Jira")
        XCTAssertNil(p.keysManaged)
    }

    func testApproveSendsBody() async throws {
        try stub("POST /api/runs/r-2/approve", "decision")
        let r = try await client.approve(runId: "r-2", node: "approve_deploy", comment: "lgtm")
        XCTAssertEqual(r.actor, "Huu Lam")
        let req = try XCTUnwrap(StubProtocol.seen.last)
        XCTAssertEqual(req.httpMethod, "POST")
        let body = try JSONDecoder().decode(JSONValue.self, from: req.httpBody ?? Data())
        XCTAssertEqual(body, .object(["node": .string("approve_deploy"), "comment": .string("lgtm")]))
    }

    func testRejectErrorsCarryDetail() async throws {
        StubProtocol.replies["POST /api/runs/r-2/reject"] = .init(
            status: 403, body: Data(#"{"detail":"Bob isn't an approver"}"#.utf8))
        do {
            _ = try await client.reject(runId: "r-2", node: nil, comment: nil)
            XCTFail("expected an error")
        } catch let e as APIError {
            XCTAssertEqual(e, .http(status: 403, detail: "Bob isn't an approver"))
            XCTAssertEqual(e.status, 403)
        }
        let body = try JSONDecoder().decode(JSONValue.self, from: StubProtocol.seen.last?.httpBody ?? Data())
        XCTAssertEqual(body, .object([:]))
    }

    func testSubmit() async throws {
        try stub("POST /api/runs", "submit")
        let r = try await client.submit(pipeline: "ticket-to-eod", payload: .object(["issue_key": .string("PROJ-1")]), dedupeKey: "k1")
        XCTAssertEqual(r.runId, "r-9")
        XCTAssertFalse(r.created)
        let body = try JSONDecoder().decode(JSONValue.self, from: StubProtocol.seen.last?.httpBody ?? Data())
        XCTAssertEqual(body["dedupe_key"], .string("k1"))
        XCTAssertEqual(body["payload"]?["issue_key"], .string("PROJ-1"))
    }

    func testSettingsAndProviderTest() async throws {
        try stub("GET /api/settings", "settings")
        let s = try await client.settings()
        XCTAssertEqual(s.providers.map(\.name), ["anthropic", "openrouter"])
        XCTAssertEqual(s.providers[1].headers?["X-Title"], "agentline")
        XCTAssertEqual(s.agents[1].env, ["FOO"])
        XCTAssertEqual(s.compatibleModels["small-tasks"], ["free-llama"])
        XCTAssertEqual(s.kinds["claude-code"]?.defaultBin, "claude")
        XCTAssertEqual(s.keys.first(where: { $0.name == "OPENROUTER_API_KEY" })?.set, true)
        // edit bodies drop empty values and never include name/implicit
        XCTAssertEqual(s.providers[1].body["base_url"], .string("https://openrouter.ai/api/v1"))
        XCTAssertNil(s.providers[1].body["name"])
        XCTAssertNil(s.providers[0].body["description"])
        XCTAssertEqual(s.agents[1].body, ["kind": .string("llm"), "model": .string("free-llama"), "env": .array([.string("FOO")]), "description": .string("cheap")])

        try stub("POST /api/providers/openrouter/test", "provider_test")
        let t = try await client.testProvider("openrouter")
        XCTAssertTrue(t.ok)
        XCTAssertEqual(t.models.filter { $0.free == true }.map(\.id), ["qwen/qwen3-8b:free"])
    }

    func testPipelinesAndHealth() async throws {
        try stub("GET /api/pipelines", "pipelines")
        let p = try await client.pipelines()
        XCTAssertEqual(p.pipelines.first?.flowName, "ticket-to-eod")
        try stub("GET /healthz", "healthz")
        let ok = try await client.health()
        XCTAssertTrue(ok)
    }

    func testUnreachable() async throws {
        let c = APIClient(baseURL: URL(string: "http://127.0.0.1:1")!, token: nil)
        do {
            _ = try await c.health()
            XCTFail("expected unreachable")
        } catch let e as APIError {
            guard case .unreachable = e else { return XCTFail("got \(e)") }
        }
    }

    func testPathSegmentsAreEscaped() {
        let r = client.request("GET", "/api/runs/\(APIClient.segment("a/b c"))")
        XCTAssertEqual(r.url?.absoluteString, "http://127.0.0.1:5555/api/runs/a%2Fb%20c")
    }
}

final class JSONValueTests: XCTestCase {
    func testParseAndText() throws {
        XCTAssertEqual(try JSONValue.parse(""), .object([:]))
        let v = try JSONValue.parse(#"{"b": 1, "a": [true, null, "x"]}"#)
        XCTAssertEqual(v.compactText, #"{"a":[true,null,"x"],"b":1}"#)
        XCTAssertThrowsError(try JSONValue.parse("{nope"))
    }

    func testTicketKeys() {
        XCTAssertEqual(JSONValue.object(["issue_key": .string("A-1")]).ticketKey, "A-1")
        XCTAssertEqual(JSONValue.object(["issue": .object(["key": .string("B-2")])]).ticketKey, "B-2")
        XCTAssertNil(JSONValue.object(["x": .number(1)]).ticketKey)
    }
}
