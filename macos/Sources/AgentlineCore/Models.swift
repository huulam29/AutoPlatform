import Foundation

// Wire models for the agentline HTTP API. Every key is spelled out (no snake-case strategy:
// it would also rename keys inside run payloads). Optional fields are lenient so an older or
// newer server still decodes.

/// A point in time sent as epoch seconds (the ledger) or an ISO-8601 string.
public struct Timestamp: Codable, Sendable, Hashable, Comparable {
    public let date: Date

    public init(_ date: Date) { self.date = date }

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let n = try? c.decode(Double.self) {
            date = Date(timeIntervalSince1970: n)
            return
        }
        let s = try c.decode(String.self)
        if let n = Double(s) {
            date = Date(timeIntervalSince1970: n)
            return
        }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = iso.date(from: s) {
            date = d
            return
        }
        iso.formatOptions = [.withInternetDateTime]
        if let d = iso.date(from: s) {
            date = d
            return
        }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "bad timestamp \(s)")
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(date.timeIntervalSince1970)
    }

    public static func < (a: Timestamp, b: Timestamp) -> Bool { a.date < b.date }
}

// MARK: - Runs

public enum RunStatus: String, CaseIterable, Sendable {
    case pending, running, waiting, failed
    case needsReview = "needs-review"
    case done, dead, cancelled

    public var isTerminal: Bool { self == .done || self == .dead || self == .cancelled }
}

public struct RunSummary: Codable, Sendable, Identifiable, Hashable {
    public let id: String
    public let flow: String
    public let status: String
    public let payload: JSONValue?
    public let costUsd: Double?
    public let budgetUsd: Double?
    public let error: String?
    public let dedupeKey: String?
    public let createdAt: Timestamp?
    public let updatedAt: Timestamp?
    public let terminal: Bool?

    enum CodingKeys: String, CodingKey {
        case id, flow, status, payload, error, terminal
        case costUsd = "cost_usd"
        case budgetUsd = "budget_usd"
        case dedupeKey = "dedupe_key"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public var ticketKey: String? { payload?.ticketKey }
    public var runStatus: RunStatus? { RunStatus(rawValue: status) }
}

public struct RunsResponse: Codable, Sendable {
    public let runs: [RunSummary]
}

public struct Execution: Codable, Sendable, Identifiable, Hashable {
    public let id: String
    public let nodeId: String
    public let iteration: Int
    public let key: String?
    public let status: String
    public let attempt: Int?
    public let reclaims: Int?
    public let costUsd: Double?
    public let reason: String?
    public let error: String?
    public let detail: JSONValue?
    public let createdAt: Timestamp?
    public let startedAt: Timestamp?
    public let finishedAt: Timestamp?

    enum CodingKeys: String, CodingKey {
        case id, iteration, key, status, attempt, reclaims, reason, error, detail
        case nodeId = "node_id"
        case costUsd = "cost_usd"
        case createdAt = "created_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
    }

    /// A gate parked for a human decision.
    public var isWaitingGate: Bool { status == "waiting" && detail?["kind"]?.stringValue == "gate" }
    public var gatePrompt: String? { detail?["prompt"]?.stringValue }
    public var approvers: [String] { detail?["approvers"]?.arrayValue?.compactMap(\.stringValue) ?? [] }
    public var label: String { "\(nodeId)#\(iteration)" }
}

public struct NextCommand: Codable, Sendable, Hashable {
    public let node: String?
    public let command: String
    public let description: String
}

public struct RunPipelineRef: Codable, Sendable, Hashable {
    public let name: String
}

public struct RunDetail: Codable, Sendable {
    public let run: RunSummary
    public let executions: [Execution]
    public let pipeline: RunPipelineRef?
    public let nextCommands: [NextCommand]
    /// Per node: tasks pending / running / done / failed (for_each nodes have several).
    public let nodeCounts: [String: TaskCounts]

    enum CodingKeys: String, CodingKey {
        case run, executions, pipeline
        case nextCommands = "next_commands"
        case nodeCounts = "node_counts"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        run = try c.decode(RunSummary.self, forKey: .run)
        executions = try c.decodeIfPresent([Execution].self, forKey: .executions) ?? []
        pipeline = try c.decodeIfPresent(RunPipelineRef.self, forKey: .pipeline)
        nextCommands = try c.decodeIfPresent([NextCommand].self, forKey: .nextCommands) ?? []
        nodeCounts = try c.decodeIfPresent([String: TaskCounts].self, forKey: .nodeCounts) ?? [:]
    }

    public var waitingGates: [Execution] { executions.filter(\.isWaitingGate) }
}

public struct TaskCounts: Codable, Sendable, Hashable {
    public let total: Int
    public let pending: Int
    public let running: Int
    public let done: Int
    public let failed: Int

    /// "▶2 ✓5 ✗1 /8" for nodes with several tasks, nil for a plain node.
    public var label: String? {
        guard total > 1 else { return nil }
        var parts: [String] = []
        if running > 0 { parts.append("▶\(running)") }
        parts.append("✓\(done)")
        if failed > 0 { parts.append("✗\(failed)") }
        if pending > 0 { parts.append("·\(pending)") }
        return parts.joined(separator: " ") + " /\(total)"
    }
}

// MARK: - Approvals, submit, status

public struct Approval: Codable, Sendable, Identifiable, Hashable {
    public let runId: String
    public let pipeline: String
    public let nodeId: String
    public let executionId: String
    public let prompt: String?
    public let approvers: [String]?
    public let waitingSince: Timestamp?
    public let payload: JSONValue?

    public var id: String { executionId }
    public var ticketKey: String? { payload?.ticketKey }

    public init(
        runId: String, pipeline: String, nodeId: String, executionId: String, prompt: String?,
        approvers: [String]?, waitingSince: Timestamp?, payload: JSONValue?
    ) {
        self.runId = runId
        self.pipeline = pipeline
        self.nodeId = nodeId
        self.executionId = executionId
        self.prompt = prompt
        self.approvers = approvers
        self.waitingSince = waitingSince
        self.payload = payload
    }

    enum CodingKeys: String, CodingKey {
        case pipeline, prompt, approvers, payload
        case runId = "run_id"
        case nodeId = "node_id"
        case executionId = "execution_id"
        case waitingSince = "waiting_since"
    }
}

public struct DecisionResult: Codable, Sendable {
    public let runId: String
    public let nodeId: String?
    public let actor: String?

    enum CodingKeys: String, CodingKey {
        case actor
        case runId = "run_id"
        case nodeId = "node_id"
    }
}

public struct SubmitResult: Codable, Sendable {
    public let runId: String
    public let created: Bool

    enum CodingKeys: String, CodingKey {
        case created
        case runId = "run_id"
    }
}

public struct JiraPollStatus: Codable, Sendable, Hashable {
    public let enabled: Bool
    public let everyS: Double?
    public let lastPollAt: Timestamp?
    public let lastError: String?
    public let pipelines: [String]?

    enum CodingKeys: String, CodingKey {
        case enabled, pipelines
        case everyS = "every_s"
        case lastPollAt = "last_poll_at"
        case lastError = "last_error"
    }
}

public struct EngineStatus: Codable, Sendable, Hashable {
    public let version: String?
    public let pipelines: [String]?
    public let jiraPoll: JiraPollStatus?
    public let keysManaged: String?
    /// Settings changed since the engine started (e.g. from the chat link): restart to apply.
    public let restartNeeded: [String]?

    enum CodingKeys: String, CodingKey {
        case version, pipelines
        case jiraPoll = "jira_poll"
        case keysManaged = "keys_managed"
        case restartNeeded = "restart_needed"
    }
}

public struct PipelineSummary: Codable, Sendable, Identifiable, Hashable {
    public let name: String
    public let path: String?
    public let pipeline: String?
    public let nodes: Int?
    public let issues: Int?
    public var id: String { name }
    /// The name runs are submitted under (`name:` inside the file), else the file name.
    public var flowName: String { pipeline ?? name }
}

public struct PipelinesResponse: Codable, Sendable {
    public let dir: String?
    public let pipelines: [PipelineSummary]
}

// MARK: - Settings

public struct AgentKind: Codable, Sendable, Hashable {
    public let runner: String?
    public let providerTypes: [String]?
    public let needsModel: Bool?
    public let subscription: Bool?
    public let defaultBin: String?
    public let description: String?

    enum CodingKeys: String, CodingKey {
        case runner, subscription, description
        case providerTypes = "provider_types"
        case needsModel = "needs_model"
        case defaultBin = "default_bin"
    }
}

public struct ProviderInfo: Codable, Sendable, Identifiable, Hashable {
    public var name: String
    public var type: String
    public var baseUrl: String?
    public var apiKeyEnv: String?
    public var auth: String?
    public var headers: [String: String]?
    public var description: String?
    public var implicit: Bool?
    public var id: String { name }

    public init(
        name: String, type: String, baseUrl: String? = nil, apiKeyEnv: String? = nil,
        auth: String? = nil, headers: [String: String]? = nil, description: String? = nil,
        implicit: Bool? = nil
    ) {
        self.name = name
        self.type = type
        self.baseUrl = baseUrl
        self.apiKeyEnv = apiKeyEnv
        self.auth = auth
        self.headers = headers
        self.description = description
        self.implicit = implicit
    }

    enum CodingKeys: String, CodingKey {
        case name, type, auth, headers, description, implicit
        case baseUrl = "base_url"
        case apiKeyEnv = "api_key_env"
    }

    /// The body for `PUT /api/settings/providers/<name>` (empty values are dropped server side).
    public var body: [String: JSONValue] {
        var b: [String: JSONValue] = ["type": .string(type)]
        if let baseUrl, !baseUrl.isEmpty { b["base_url"] = .string(baseUrl) }
        if let apiKeyEnv, !apiKeyEnv.isEmpty { b["api_key_env"] = .string(apiKeyEnv) }
        if type == "anthropic", let auth, !auth.isEmpty { b["auth"] = .string(auth) }
        if let headers, !headers.isEmpty { b["headers"] = .object(headers.mapValues { .string($0) }) }
        if let description, !description.isEmpty { b["description"] = .string(description) }
        return b
    }
}

public struct ModelInfo: Codable, Sendable, Identifiable, Hashable {
    public var name: String
    public var provider: String
    public var model: String
    public var inputUsdPerMtok: Double?
    public var outputUsdPerMtok: Double?
    public var maxTokens: Int?
    public var temperature: Double?
    public var description: String?
    /// Built in (not in agentline.toml); editing it saves an override, deleting isn't possible.
    public var implicit: Bool?
    public var id: String { name }

    public init(name: String, provider: String, model: String) {
        self.name = name
        self.provider = provider
        self.model = model
    }

    enum CodingKeys: String, CodingKey {
        case name, provider, model, temperature, description, implicit
        case inputUsdPerMtok = "input_usd_per_mtok"
        case outputUsdPerMtok = "output_usd_per_mtok"
        case maxTokens = "max_tokens"
    }

    public var body: [String: JSONValue] {
        var b: [String: JSONValue] = ["provider": .string(provider), "model": .string(model)]
        if let inputUsdPerMtok { b["input_usd_per_mtok"] = .number(inputUsdPerMtok) }
        if let outputUsdPerMtok { b["output_usd_per_mtok"] = .number(outputUsdPerMtok) }
        if let maxTokens { b["max_tokens"] = .number(Double(maxTokens)) }
        if let temperature { b["temperature"] = .number(temperature) }
        if let description, !description.isEmpty { b["description"] = .string(description) }
        return b
    }
}

public struct AgentInfo: Codable, Sendable, Identifiable, Hashable {
    public var name: String
    public var kind: String
    public var bin: String?
    public var model: String?
    public var extraArgs: [String]?
    public var env: [String]?
    public var description: String?
    public var implicit: Bool?
    public var id: String { name }

    public init(name: String, kind: String) {
        self.name = name
        self.kind = kind
    }

    enum CodingKeys: String, CodingKey {
        case name, kind, bin, model, env, description, implicit
        case extraArgs = "extra_args"
    }

    public var body: [String: JSONValue] {
        var b: [String: JSONValue] = ["kind": .string(kind)]
        if let bin, !bin.isEmpty { b["bin"] = .string(bin) }
        if let model, !model.isEmpty { b["model"] = .string(model) }
        if let extraArgs, !extraArgs.isEmpty { b["extra_args"] = .array(extraArgs.map { .string($0) }) }
        if let env, !env.isEmpty { b["env"] = .array(env.map { .string($0) }) }
        if let description, !description.isEmpty { b["description"] = .string(description) }
        return b
    }
}

public struct KeyStatus: Codable, Sendable, Hashable {
    public let name: String
    public let set: Bool
    public let source: String?
}

public struct SettingsSnapshot: Codable, Sendable {
    public let kinds: [String: AgentKind]
    public let providers: [ProviderInfo]
    public let models: [ModelInfo]
    public let agents: [AgentInfo]
    public let mcp: [McpInfo]
    public let compatibleModels: [String: [String]]
    public let keys: [KeyStatus]
    public let configPath: String?
    public let note: String?

    enum CodingKeys: String, CodingKey {
        case kinds, providers, models, agents, keys, note, mcp
        case compatibleModels = "compatible_models"
        case configPath = "config_path"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kinds = try c.decodeIfPresent([String: AgentKind].self, forKey: .kinds) ?? [:]
        providers = try c.decodeIfPresent([ProviderInfo].self, forKey: .providers) ?? []
        models = try c.decodeIfPresent([ModelInfo].self, forKey: .models) ?? []
        agents = try c.decodeIfPresent([AgentInfo].self, forKey: .agents) ?? []
        mcp = try c.decodeIfPresent([McpInfo].self, forKey: .mcp) ?? []
        compatibleModels =
            try c.decodeIfPresent([String: [String]].self, forKey: .compatibleModels) ?? [:]
        keys = try c.decodeIfPresent([KeyStatus].self, forKey: .keys) ?? []
        configPath = try c.decodeIfPresent(String.self, forKey: .configPath)
        note = try c.decodeIfPresent(String.self, forKey: .note)
    }
}

/// `[mcp.<name>]`: an MCP server nodes can allow (`mcp: [name]`). Secrets are `${KEY}`
/// placeholders filled from Keys at run time.
public struct McpInfo: Codable, Sendable, Identifiable, Hashable {
    public var name: String
    public var transport: String
    public var command: String?
    public var args: [String]?
    public var env: [String: String]?
    public var url: String?
    public var headers: [String: String]?
    public var description: String?
    /// The `${KEY}` names it needs (engine-computed).
    public var keys: [String]?
    public var id: String { name }

    public init(name: String, transport: String = "stdio") {
        self.name = name
        self.transport = transport
    }

    public var body: [String: JSONValue] {
        var b: [String: JSONValue] = ["transport": .string(transport)]
        if transport == "stdio" {
            if let command, !command.isEmpty { b["command"] = .string(command) }
            if let args, !args.isEmpty { b["args"] = .array(args.map { .string($0) }) }
            if let env, !env.isEmpty { b["env"] = .object(env.mapValues { .string($0) }) }
        } else {
            if let url, !url.isEmpty { b["url"] = .string(url) }
            if let headers, !headers.isEmpty { b["headers"] = .object(headers.mapValues { .string($0) }) }
        }
        if let description, !description.isEmpty { b["description"] = .string(description) }
        return b
    }

    /// `KEY=value` lines <-> a table (blank and malformed lines are dropped).
    public static func parseLines(_ text: String) -> [String: String] {
        var out: [String: String] = [:]
        for line in text.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            if parts.count == 2, !parts[0].isEmpty { out[parts[0]] = parts[1] }
        }
        return out
    }

    public static func lines(_ table: [String: String]?) -> String {
        (table ?? [:]).sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: "\n")
    }
}

public struct McpTestResult: Codable, Sendable {
    public struct Tool: Codable, Sendable, Hashable {
        public let name: String
        public let description: String?
    }

    public let ok: Bool
    public let error: String?
    public let tools: [Tool]?
    public let note: String?

    public init(ok: Bool, error: String? = nil, tools: [Tool]? = nil, note: String? = nil) {
        self.ok = ok
        self.error = error
        self.tools = tools
        self.note = note
    }
}

public struct ProviderModel: Codable, Sendable, Identifiable, Hashable {
    public let id: String
    public let name: String?
    public let free: Bool?
    public let contextLength: Int?

    public init(id: String, name: String?, free: Bool?, contextLength: Int?) {
        self.id = id
        self.name = name
        self.free = free
        self.contextLength = contextLength
    }

    enum CodingKeys: String, CodingKey {
        case id, name, free
        case contextLength = "context_length"
    }
}

public struct ProviderTestResult: Codable, Sendable {
    public let ok: Bool
    public let error: String?
    public let count: Int?
    public let models: [ProviderModel]
    /// Why it failed, when the user can fix it: no_key, auth, unreachable, http, bad_response.
    public let problem: String?
    /// The key env var involved (no_key, auth).
    public let key: String?
    /// The CLI that's missing (no_cli).
    public let bin: String?

    public init(ok: Bool, error: String? = nil, count: Int? = nil, models: [ProviderModel] = [],
                problem: String? = nil, key: String? = nil, bin: String? = nil) {
        self.ok = ok
        self.error = error
        self.count = count
        self.models = models
        self.problem = problem
        self.key = key
        self.bin = bin
    }
}

public struct AgentDetectResult: Codable, Sendable {
    public let found: Bool
    public let bin: String?
    public let version: String?
    public let error: String?
    public let note: String?
    /// no_cli: the agent's program isn't installed / on PATH.
    public let problem: String?
}
