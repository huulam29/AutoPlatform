import XCTest

@testable import AgentlineCore

final class SettingsLogicTests: XCTestCase {
    let openrouter = ProviderInfo(
        name: "openrouter", type: "openai-compatible", baseUrl: "https://openrouter.ai/api/v1",
        apiKeyEnv: "OPENROUTER_API_KEY")

    func testHelpForMissingKeyOffersToSetIt() {
        let r = ProviderTestResult(ok: false, error: "OPENROUTER_API_KEY is not set", problem: "no_key", key: "OPENROUTER_API_KEY")
        let h = ProviderHelp.advice(for: r, provider: openrouter)
        XCTAssertEqual(h.fix, .setKey)
        XCTAssertEqual(h.key, "OPENROUTER_API_KEY")
        XCTAssertTrue(h.steps[0].contains("openrouter.ai/settings/keys"))
    }

    func testHelpForMissingClaudeCLIOffersInstall() {
        let claude = ProviderInfo(name: "claude", type: "anthropic", auth: "subscription")
        let r = ProviderTestResult(ok: false, error: "'claude' isn't installed", problem: "no_cli", bin: "claude")
        let h = ProviderHelp.advice(for: r, provider: claude)
        XCTAssertEqual(h.fix, .installCLI)
        XCTAssertTrue(h.steps.joined().contains("log in"))
        XCTAssertTrue(ProviderHelp.claudeInstallCommands.contains { $0.command.contains("@anthropic-ai/claude-code") })
    }

    func testHelpForUnreachableOllamaSaysStartIt() {
        let ollama = ProviderInfo(name: "ollama", type: "openai-compatible", baseUrl: "http://localhost:11434/v1")
        let h = ProviderHelp.advice(for: ProviderTestResult(ok: false, error: "refused", problem: "unreachable"), provider: ollama)
        XCTAssertEqual(h.fix, .editURL)
        XCTAssertTrue(h.steps.joined().contains("ollama serve"))
    }

    func testModelFilterSearchSourceOrigin() {
        var a = ModelInfo(name: "opus", provider: "claude", model: "claude-opus-5-5")
        a.implicit = true
        let b = ModelInfo(name: "qwen3", provider: "openrouter", model: "qwen/qwen3")
        XCTAssertEqual(ModelFilter(search: "QWEN").apply([a, b]).map(\.name), ["qwen3"])
        XCTAssertEqual(ModelFilter(source: "claude").apply([a, b]).map(\.name), ["opus"])
        XCTAssertEqual(ModelFilter(origin: .custom).apply([a, b]).map(\.name), ["qwen3"])
        XCTAssertEqual(ModelFilter(origin: .builtIn).apply([a, b]).map(\.name), ["opus"])
    }

    func testChecklistStartsWithConfiguredOnlyAndLocksBuiltIns() {
        var builtin = ModelInfo(name: "free", provider: "openrouter", model: "openrouter/free")
        builtin.implicit = true
        let mine = ModelInfo(name: "qwen3", provider: "openrouter", model: "qwen/qwen3")
        let other = ModelInfo(name: "gpt", provider: "openai", model: "gpt-6")
        var c = ModelChecklist(provider: "openrouter", configured: [builtin, mine, other])
        XCTAssertEqual(c.checked, ["openrouter/free", "qwen/qwen3"])
        XCTAssertFalse(c.changed)
        c.toggle("openrouter/free")  // locked
        XCTAssertFalse(c.changed)
        c.toggle("meta/llama:free")
        c.toggle("qwen/qwen3")
        XCTAssertEqual(c.selection, ["meta/llama:free"])
        let listing = [
            ProviderModel(id: "meta/llama:free", name: "Llama", free: true, contextLength: 8000),
            ProviderModel(id: "x/big", name: "Big", free: false, contextLength: nil),
        ]
        XCTAssertEqual(c.shown(listing, search: "", freeOnly: true, checkedOnly: false).map(\.id), ["meta/llama:free"])
        XCTAssertEqual(c.shown(listing, search: "big", freeOnly: false, checkedOnly: false).map(\.id), ["x/big"])
        XCTAssertEqual(c.shown(listing, search: "", freeOnly: false, checkedOnly: true).map(\.id), ["meta/llama:free"])
    }
}

final class McpInfoTests: XCTestCase {
    func testBodyAndLines() {
        var m = McpInfo(name: "gh")
        m.command = "npx"
        m.args = ["-y", "srv"]
        m.env = McpInfo.parseLines("T=${GH_TOKEN}\n\nbad line\n A = b ")
        m.url = "ignored for stdio"
        XCTAssertEqual(m.env, ["T": "${GH_TOKEN}", "A": "b"])
        XCTAssertEqual(m.body["command"], .string("npx"))
        XCTAssertNil(m.body["url"])
        XCTAssertEqual(McpInfo.lines(m.env), "A=b\nT=${GH_TOKEN}")
        let json = #"{"name":"lin","transport":"http","url":"https://x/mcp","keys":["K"]}"#
        let d = try? JSONDecoder().decode(McpInfo.self, from: Data(json.utf8))
        XCTAssertEqual(d?.keys, ["K"])
    }
}
