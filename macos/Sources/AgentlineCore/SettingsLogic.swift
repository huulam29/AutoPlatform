import Foundation

/// What to tell the user when a model source's test fails, by the engine's `problem` code.
public struct ProviderHelp: Equatable, Sendable {
    /// Ways to install Claude Code (copied into Terminal by the user).
    public static let claudeInstallCommands: [(label: String, command: String)] = [
        ("Installer", "curl -fsSL https://claude.ai/install.sh | bash"),
        ("npm", "npm install -g @anthropic-ai/claude-code"),
        ("Then log in", "claude"),
    ]

    public enum Fix: String, Sendable { case setKey, editURL, edit, installCLI }

    public let title: String
    public let steps: [String]
    /// The main fix the help sheet offers.
    public let fix: Fix
    /// The key to set (setKey).
    public let key: String?

    public static func advice(for result: ProviderTestResult, provider: ProviderInfo) -> ProviderHelp {
        let key = result.key ?? provider.apiKeyEnv
        let local = (provider.baseUrl ?? "").contains("localhost") || (provider.baseUrl ?? "").contains("127.0.0.1")
        switch result.problem {
        case "no_cli":
            let bin = result.bin ?? "claude"
            return ProviderHelp(
                title: "Claude Code isn't installed",
                steps: [
                    "A Claude subscription works through the Claude Code app (`\(bin)`), which isn't on this Mac yet.",
                    "Install it: copy one of the commands below and run it in Terminal.",
                    "Run `claude` once in Terminal and log in with your Claude account (Pro or Max).",
                    "Click Test Again. Installed somewhere unusual? Choose the claude program instead.",
                ],
                fix: .installCLI, key: nil)
        case "no_key":
            return ProviderHelp(
                title: "\(provider.name) needs an API key",
                steps: [
                    "Get a key from \(Self.keyPage(provider) ?? "the provider's dashboard").",
                    "Paste it below. It goes to the macOS Keychain, never into agentline.toml.",
                    "The engine restarts so it can use the key, then the test runs again.",
                ],
                fix: .setKey, key: key)
        case "auth":
            return ProviderHelp(
                title: "\(provider.name) refused the key",
                steps: [
                    "The key in \(key ?? "the key variable") is wrong, expired or has no credit.",
                    "Create a new key at \(Self.keyPage(provider) ?? "the provider's dashboard") and paste it below.",
                ],
                fix: key == nil ? .edit : .setKey, key: key)
        case "unreachable":
            return ProviderHelp(
                title: "Can't reach \(provider.name)",
                steps: local
                    ? [
                        "Nothing answers at \(provider.baseUrl ?? "the base URL").",
                        provider.name == "ollama" || (provider.baseUrl ?? "").contains("11434")
                            ? "Start Ollama (open the Ollama app, or run `ollama serve`), then test again."
                            : "Start the local model server, or fix the address below.",
                    ]
                    : [
                        "Check your network connection.",
                        "Check the base URL below; OpenAI-compatible URLs usually end in /v1.",
                    ],
                fix: .editURL, key: nil)
        default:
            return ProviderHelp(
                title: "\(provider.name) didn't return a model list",
                steps: [
                    result.error ?? "The source answered with something unexpected.",
                    "Check the base URL (OpenAI-compatible URLs usually end in /v1) and the type.",
                ],
                fix: .editURL, key: nil)
        }
    }

    static func keyPage(_ p: ProviderInfo) -> String? {
        let url = p.baseUrl ?? ""
        if url.contains("openrouter.ai") { return "openrouter.ai/settings/keys" }
        if url.contains("openai.com") { return "platform.openai.com/api-keys" }
        if p.type == "anthropic" { return "console.anthropic.com/settings/keys" }
        return nil
    }
}

/// The Models tab's search and filters.
public struct ModelFilter: Equatable, Sendable {
    public enum Origin: String, CaseIterable, Sendable { case all, builtIn, custom }

    public var search = ""
    public var source = ""  // "" = every source
    public var origin = Origin.all

    public init(search: String = "", source: String = "", origin: Origin = .all) {
        self.search = search
        self.source = source
        self.origin = origin
    }

    public func apply(_ models: [ModelInfo]) -> [ModelInfo] {
        let q = search.trimmingCharacters(in: .whitespaces)
        return models.filter { m in
            (source.isEmpty || m.provider == source)
                && (origin == .all || (origin == .builtIn) == (m.implicit == true))
                && (q.isEmpty || [m.name, m.model, m.provider, m.description ?? ""]
                    .contains { $0.localizedCaseInsensitiveContains(q) })
        }
    }
}

/// The source's Models… checklist: which listed models are on, and which are shown.
public struct ModelChecklist: Equatable, Sendable {
    public var checked: Set<String>
    /// Built-in models on this source: always there, shown checked and locked.
    public let locked: Set<String>
    public let initial: Set<String>

    public init(provider: String, configured: [ModelInfo]) {
        let mine = configured.filter { $0.provider == provider }
        checked = Set(mine.map(\.model))
        locked = Set(mine.filter { $0.implicit == true }.map(\.model))
        initial = checked
    }

    public var changed: Bool { checked != initial }

    public mutating func toggle(_ id: String) {
        guard !locked.contains(id) else { return }
        if checked.contains(id) { checked.remove(id) } else { checked.insert(id) }
    }

    /// What to send: the checked custom models (built-in ones stay regardless).
    public var selection: [String] { checked.subtracting(locked).sorted() }

    public func shown(_ listing: [ProviderModel], search: String, freeOnly: Bool, checkedOnly: Bool) -> [ProviderModel] {
        let q = search.trimmingCharacters(in: .whitespaces)
        return listing.filter { m in
            (!freeOnly || m.free == true)
                && (!checkedOnly || checked.contains(m.id))
                && (q.isEmpty || m.id.localizedCaseInsensitiveContains(q)
                    || (m.name ?? "").localizedCaseInsensitiveContains(q))
        }
    }
}
