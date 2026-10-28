import Darwin
import Foundation
import XCTest

@testable import AgentlineCore

final class BackendLaunchTests: XCTestCase {
    let loc = BackendLocation(
        python: URL(fileURLWithPath: "/App/Contents/Resources/backend/python/bin/python3"),
        web: URL(fileURLWithPath: "/App/Contents/Resources/backend/web"), isDev: false)

    func testAppPathsOverride() {
        let p = AppPaths(environment: ["AGENTLINE_APP_SUPPORT_OVERRIDE": "/tmp/scratch/al"])
        XCTAssertTrue(p.overridden)
        XCTAssertEqual(p.home.path, "/tmp/scratch/al")
        XCTAssertEqual(p.db.path, "/tmp/scratch/al/agentline.db")
        XCTAssertEqual(p.config.path, "/tmp/scratch/al/agentline.toml")
        XCTAssertEqual(p.defaultPipelinesDir.path, "/tmp/scratch/al/pipelines")
    }

    func testAppPathsDefault() {
        let p = AppPaths(environment: [:])
        XCTAssertFalse(p.overridden)
        XCTAssertTrue(p.home.path.hasSuffix("Library/Application Support/agentline"), p.home.path)
        XCTAssertTrue(p.defaultPipelinesDir.path.hasSuffix("Documents/agentline/pipelines"))
    }

    func testResolveBundledAndDev() throws {
        let res = URL(fileURLWithPath: "/App/Contents/Resources")
        let bundled = try BackendLocation.resolve(environment: [:], resources: res, fileExists: { _ in true })
        XCTAssertEqual(bundled.python.path, loc.python.path)
        XCTAssertEqual(bundled.web?.path, loc.web?.path)
        XCTAssertFalse(bundled.isDev)
        XCTAssertThrowsError(try BackendLocation.resolve(environment: [:], resources: res, fileExists: { _ in false }))
        XCTAssertThrowsError(try BackendLocation.resolve(environment: [:], resources: nil, fileExists: { _ in true }))

        let dev = try BackendLocation.resolve(
            environment: ["AGENTLINE_DEV_PYTHON": "/repo/.venv/bin/python", "AGENTLINE_DEV_WEB": "/repo/web/dist"],
            resources: nil, fileExists: { $0 == "/repo/.venv/bin/python" })
        XCTAssertTrue(dev.isDev)
        XCTAssertEqual(dev.python.path, "/repo/.venv/bin/python")
        XCTAssertEqual(dev.web?.path, "/repo/web/dist")
        XCTAssertThrowsError(try BackendLocation.resolve(
            environment: ["AGENTLINE_DEV_PYTHON": "/missing"], resources: res, fileExists: { _ in false }))
    }

    func testEnvironment() {
        let paths = AppPaths(environment: ["AGENTLINE_APP_SUPPORT_OVERRIDE": "/s"])
        let env = LaunchSpec.environment(
            base: ["PATH": "/usr/bin:/bin", "HOME": "/Users/x", "PYTHONPATH": "/evil", "OPENROUTER_API_KEY": "old",
                   "AGENTLINE_DEV_PYTHON": "/p", "AGENTLINE_APP_SUPPORT_OVERRIDE": "/s"],
            paths: paths, location: loc, token: "t0k", user: "Huu Lam",
            keys: ["OPENROUTER_API_KEY": "sk-new", "JIRA_EMAIL": "a@b.c"], homeDir: "/Users/x")
        XCTAssertEqual(env["AGENTLINE_HOME"], "/s")
        XCTAssertEqual(env["AGENTLINE_DB"], "/s/agentline.db")
        XCTAssertEqual(env["AGENTLINE_CONFIG"], "/s/agentline.toml")
        XCTAssertEqual(env["AGENTLINE_API_TOKEN"], "t0k")
        XCTAssertEqual(env["AGENTLINE_WEB_DIST"], "/App/Contents/Resources/backend/web")
        XCTAssertEqual(env["AGENTLINE_KEYS_MANAGED"], "file")
        XCTAssertEqual(env["AGENTLINE_USER"], "Huu Lam")
        XCTAssertEqual(env["OPENROUTER_API_KEY"], "sk-new")  // the Keychain wins
        XCTAssertEqual(env["JIRA_EMAIL"], "a@b.c")
        XCTAssertEqual(env["HOME"], "/Users/x")
        XCTAssertNil(env["PYTHONPATH"])
        XCTAssertNil(env["AGENTLINE_DEV_PYTHON"])
        XCTAssertNil(env["AGENTLINE_APP_SUPPORT_OVERRIDE"])
        XCTAssertEqual(
            env["PATH"],
            "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin:/Users/x/.local/bin:/Users/x/.npm-global/bin:/Users/x/.claude/local")
    }

    func testExtendedPathHasNoDuplicates() {
        XCTAssertEqual(
            LaunchSpec.extendedPath("/opt/homebrew/bin:/usr/bin", homeDir: "/h"),
            "/opt/homebrew/bin:/usr/bin:/usr/local/bin:/h/.local/bin:/h/.npm-global/bin:/h/.claude/local")
        XCTAssertTrue(LaunchSpec.extendedPath(nil, homeDir: "/h").hasPrefix("/usr/bin:/bin"))
    }

    func testServeAndSeedCommands() {
        let dir = URL(fileURLWithPath: "/Users/x/Documents/agentline/pipelines")
        let spec = LaunchSpec.serve(location: loc, pipelinesDir: dir, port: 54321, environment: ["A": "1"])
        XCTAssertEqual(spec.executable, loc.python)
        XCTAssertEqual(spec.arguments, [
            "-m", "agentline.cli", "serve", "--host", "127.0.0.1", "--port", "54321", "--concurrency", "4",
            "--pipelines", "/Users/x/Documents/agentline/pipelines",
        ])
        XCTAssertEqual(spec.environment, ["A": "1"])
        let seed = LaunchSpec.seed(location: loc, pipelinesDir: dir, environment: [:])
        XCTAssertEqual(seed.arguments, ["-m", "agentline.cli", "new", "ticket-to-eod", dir.path])
    }

    func testTokens() {
        let a = Tokens.random()
        XCTAssertEqual(a.count, 64)
        XCTAssertTrue(a.allSatisfy { "0123456789abcdef".contains($0) })
        XCTAssertNotEqual(a, Tokens.random())
    }
}

final class PortFinderTests: XCTestCase {
    func testFreePortIsBindable() throws {
        let port = try PortFinder.freePort()
        XCTAssertGreaterThan(port, 1023)
        XCTAssertLessThan(port, 65536)
        // bind it ourselves to prove it was free
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let r = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        XCTAssertEqual(r, 0)
    }

    func testPortsDiffer() throws {
        let ports = try (0..<5).map { _ in try PortFinder.freePort() }
        XCTAssertGreaterThan(Set(ports).count, 1)
    }
}

final class LogBufferTests: XCTestCase {
    func testKeepsLastLinesAndPartials() {
        var b = LogBuffer(capacity: 3)
        b.append("one\ntw")
        XCTAssertEqual(b.lines, ["one"])
        b.append("o\nthree\r\nfour\nfive\n")
        XCTAssertEqual(b.lines, ["three", "four", "five"])
        b.append("six")
        XCTAssertEqual(b.text, "three\nfour\nfive\nsix")
        b.appendLine("[app] x")
        XCTAssertEqual(b.lines, ["five", "six", "[app] x"])
    }
}

final class KeychainTests: XCTestCase {
    let store = KeychainStore(service: "dev.agentline.keys.tests.\(UUID().uuidString)")

    override func tearDown() {
        try? store.removeAll()
    }

    func testRoundTrip() throws {
        XCTAssertEqual(try store.names(), [])
        XCTAssertNil(try store.get("OPENROUTER_API_KEY"))
        try store.set("sk-one", for: "OPENROUTER_API_KEY")
        try store.set("x@y.z", for: "JIRA_EMAIL")
        XCTAssertEqual(try store.get("OPENROUTER_API_KEY"), "sk-one")
        try store.set("sk-two", for: "OPENROUTER_API_KEY")  // update in place
        XCTAssertEqual(try store.get("OPENROUTER_API_KEY"), "sk-two")
        XCTAssertEqual(try store.names(), ["JIRA_EMAIL", "OPENROUTER_API_KEY"])
        XCTAssertEqual(try store.all(), ["JIRA_EMAIL": "x@y.z", "OPENROUTER_API_KEY": "sk-two"])
        XCTAssertTrue(try store.delete("JIRA_EMAIL"))
        XCTAssertFalse(try store.delete("JIRA_EMAIL"))
        XCTAssertEqual(try store.names(), ["OPENROUTER_API_KEY"])
        // the real service is untouched by tests
        XCTAssertNotEqual(store.service, KeychainStore.defaultService)
    }

    func testRejectsBadNames() {
        XCTAssertThrowsError(try store.set("v", for: "1BAD"))
        XCTAssertThrowsError(try store.set("v", for: "has space"))
        XCTAssertThrowsError(try store.set("v", for: ""))
        XCTAssertTrue(KeychainStore.isValidName("_OK_1"))
    }
}

final class NotificationPlannerTests: XCTestCase {
    func approval(_ exec: String, run: String = "r1") -> Approval {
        Approval(runId: run, pipeline: "ticket-to-eod", nodeId: "gate", executionId: exec, prompt: "Ship it?",
                 approvers: [], waitingSince: nil, payload: .object(["issue_key": .string("PROJ-1")]))
    }

    func run(_ id: String, _ status: String) -> RunSummary {
        try! JSONDecoder().decode(RunSummary.self, from: Data(#"{"id":"\#(id)","flow":"p","status":"\#(status)","payload":{}}"#.utf8))
    }

    func testGatesNotifiedOnce() {
        var p = NotificationPlanner()
        let first = p.events(approvals: [approval("x1")], runs: [])
        XCTAssertEqual(first.count, 1)
        XCTAssertEqual(first[0].title, "Approval needed: ticket-to-eod PROJ-1")
        XCTAssertEqual(first[0].body, "Ship it?")
        XCTAssertEqual(p.events(approvals: [approval("x1")], runs: []), [])
        // a new iteration of the same gate is a new execution: notify again
        XCTAssertEqual(p.events(approvals: [approval("x1"), approval("x2")], runs: []).map(\.dedupeKey), ["gate:x2"])
        // a failed poll (nil) changes nothing
        XCTAssertEqual(p.events(approvals: nil, runs: nil), [])
        XCTAssertEqual(p.events(approvals: [approval("x2")], runs: nil), [])
    }

    func testRunsBaselineThenTransitions() {
        var p = NotificationPlanner()
        // first poll: old finished runs are only a baseline
        XCTAssertEqual(p.events(approvals: [], runs: [run("a", "done"), run("b", "running")]), [])
        XCTAssertEqual(p.events(approvals: [], runs: [run("a", "done"), run("b", "running")]), [])
        let e = p.events(approvals: [], runs: [run("a", "done"), run("b", "needs-review")])
        XCTAssertEqual(e.map(\.dedupeKey), ["run:b:needs-review"])
        XCTAssertEqual(e[0].title, "Run needs review: p")
        // resolved, then dead: a different status is a different event
        XCTAssertEqual(p.events(approvals: [], runs: [run("b", "running")]), [])
        XCTAssertEqual(p.events(approvals: [], runs: [run("b", "dead")]).map(\.dedupeKey), ["run:b:dead"])
        // a new run seen already done after the baseline
        XCTAssertEqual(p.events(approvals: [], runs: [run("c", "done")]).map(\.dedupeKey), ["run:c:done"])
        // flapping back to a status already announced doesn't repeat it
        _ = p.events(approvals: [], runs: [run("b", "running")])
        XCTAssertEqual(p.events(approvals: [], runs: [run("b", "dead")]), [])
        XCTAssertEqual(p.events(approvals: [], runs: [run("d", "waiting")]), [])
    }
}

final class PresetTests: XCTestCase {
    func testPresets() {
        let ids = ProviderPreset.all.map(\.id)
        XCTAssertEqual(ids, ["openrouter", "ollama", "openai", "anthropic", "claude"])
        let claude = ProviderPreset.all.last!.provider
        XCTAssertEqual(claude.body, ["type": .string("anthropic"), "auth": .string("subscription"),
                                      "description": .string("Your Claude Pro/Max login (Claude Code only)")])
        XCTAssertNil(ProviderPreset.all[1].provider.body["api_key_env"])  // Ollama: no key
    }
}
