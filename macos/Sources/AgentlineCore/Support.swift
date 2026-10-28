import Darwin
import Foundation

public enum PortFinder {
    /// A TCP port on 127.0.0.1 that is free right now (the kernel picks it: bind to port 0).
    public static func freePort() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.EMFILE) }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EADDRINUSE) }
        var out = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let got = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        guard got == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EINVAL) }
        return Int(UInt16(bigEndian: out.sin_port))
    }
}

/// The last `capacity` lines of a stream of output chunks (partial lines are kept until their
/// newline arrives).
public struct LogBuffer: Sendable {
    public let capacity: Int
    public private(set) var lines: [String] = []
    private var partial = ""

    public init(capacity: Int = 200) { self.capacity = capacity }

    public mutating func append(_ chunk: String) {
        let text = partial + chunk
        var parts = text.components(separatedBy: "\n")
        partial = parts.removeLast()
        for p in parts { push(p) }
    }

    public mutating func appendLine(_ line: String) {
        if !partial.isEmpty {
            push(partial)
            partial = ""
        }
        push(line)
    }

    private mutating func push(_ line: String) {
        lines.append(line.hasSuffix("\r") ? String(line.dropLast()) : line)
        if lines.count > capacity { lines.removeFirst(lines.count - capacity) }
    }

    public var text: String { (lines + (partial.isEmpty ? [] : [partial])).joined(separator: "\n") }
}

/// A model source preset for Settings > Model sources.
public struct ProviderPreset: Sendable, Identifiable, Hashable {
    public let id: String
    public let title: String
    public let provider: ProviderInfo

    public static let all: [ProviderPreset] = [
        ProviderPreset(
            id: "openrouter", title: "OpenRouter",
            provider: ProviderInfo(
                name: "openrouter", type: "openai-compatible", baseUrl: "https://openrouter.ai/api/v1",
                apiKeyEnv: "OPENROUTER_API_KEY", headers: ["X-Title": "agentline"],
                description: "OpenRouter (many models, some free)")),
        ProviderPreset(
            id: "ollama", title: "Ollama (local)",
            provider: ProviderInfo(
                name: "ollama", type: "openai-compatible", baseUrl: "http://localhost:11434/v1",
                description: "Local models through Ollama; no key")),
        ProviderPreset(
            id: "openai", title: "OpenAI",
            provider: ProviderInfo(
                name: "openai", type: "openai-compatible", baseUrl: "https://api.openai.com/v1",
                apiKeyEnv: "OPENAI_API_KEY", description: "OpenAI API")),
        ProviderPreset(
            id: "anthropic", title: "Anthropic API key",
            provider: ProviderInfo(
                name: "anthropic", type: "anthropic", apiKeyEnv: "ANTHROPIC_API_KEY", auth: "api-key",
                description: "Anthropic API, billed per token")),
        ProviderPreset(
            id: "claude", title: "Claude subscription",
            provider: ProviderInfo(
                name: "claude", type: "anthropic", auth: "subscription",
                description: "Your Claude Pro/Max login (Claude Code only)")),
    ]
}
