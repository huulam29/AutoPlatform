import Foundation

/// Removes secrets from text before it is written into an incident report or an export.
///
/// Two layers: the exact values of known secrets (every Keychain key, the engine's API token),
/// longest first, then patterns for anything that looks like a token (`sk-…`, `ghp_…`,
/// `xox…-…`, `Bearer …`, JWTs, AWS access key ids, and long hex/base64 values after
/// `token=` / `key=` / `password=` / `secret=`, also in `"name": "value"` form).
public struct Redactor: Sendable, Equatable {
    public static let mask = "[REDACTED]"
    /// Shorter values are skipped: replacing every "abc" would shred the report.
    public static let minimumSecretLength = 4

    public private(set) var secrets: [String]

    public init(secrets: some Sequence<String> = [String]()) {
        self.secrets = Self.normalized(secrets)
    }

    /// This redactor plus more exact values.
    public func adding(_ more: some Sequence<String>) -> Redactor {
        var r = self
        r.secrets = Self.normalized(secrets + Array(more))
        return r
    }

    private static func normalized(_ values: some Sequence<String>) -> [String] {
        Array(Set(values.filter { $0.count >= minimumSecretLength && $0 != mask }))
            .sorted { $0.count != $1.count ? $0.count > $1.count : $0 < $1 }
    }

    public func redact(_ text: String) -> String {
        guard !text.isEmpty else { return text }
        var out = text
        for s in secrets where out.contains(s) {
            out = out.replacingOccurrences(of: s, with: Self.mask)
        }
        for (re, template) in Self.patterns {
            let range = NSRange(out.startIndex..., in: out)
            out = re.stringByReplacingMatches(in: out, options: [], range: range, withTemplate: template)
        }
        return out
    }

    public func redact(_ lines: [String]) -> [String] { lines.map { redact($0) } }

    /// Every string inside a JSON value (object keys are left alone).
    public func redact(_ value: JSONValue) -> JSONValue {
        switch value {
        case .string(let s): return .string(redact(s))
        case .array(let a): return .array(a.map { redact($0) })
        case .object(let o): return .object(o.mapValues { redact($0) })
        default: return value
        }
    }

    // NSRegularExpression is immutable and documented thread-safe.
    private static let patterns: [(NSRegularExpression, String)] = {
        let m = NSRegularExpression.escapedTemplate(for: mask)
        let specs: [(String, String)] = [
            // Authorization headers.
            (#"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"#, "Bearer \(m)"),
            // OpenAI / Anthropic / OpenRouter style keys.
            (#"\bsk-[A-Za-z0-9_\-]{8,}"#, m),
            // GitHub tokens.
            (#"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"#, m),
            (#"\bgithub_pat_[A-Za-z0-9_]{20,}"#, m),
            // Slack tokens.
            (#"\bxox[abposr]-[A-Za-z0-9\-]{8,}"#, m),
            // AWS access key ids.
            (#"\bAKIA[0-9A-Z]{16}\b"#, m),
            // JWTs.
            (#"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"#, m),
            // name=value / "name": "value" where the name says token/key/password/secret and the
            // value is a long hex/base64-ish string with at least one digit (so env var names
            // like OPENROUTER_API_KEY in `api_key_env = "…"` survive).
            (
                #"(?i)\b([A-Za-z0-9_\-]*(?:token|key|password|passwd|secret))(["']?\s*[=:]\s*["']?)(?=[A-Za-z0-9+/_\-.~=]{0,512}[0-9])[A-Za-z0-9+/_\-.~=]{12,}"#,
                "$1$2\(m)"
            ),
        ]
        return specs.map { (try! NSRegularExpression(pattern: $0.0), $0.1) }
    }()
}
