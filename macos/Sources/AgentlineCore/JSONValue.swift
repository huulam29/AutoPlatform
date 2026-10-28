import Foundation

/// Any JSON value (run payloads, execution details). Sendable, so it can cross actors.
public enum JSONValue: Codable, Sendable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else {
            self = .object(try c.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    public subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    public var stringValue: String? {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(n)
        default: return nil
        }
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    /// Compact JSON text, keys sorted.
    public var compactText: String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        guard let data = try? enc.encode(self) else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    /// Indented JSON text, keys sorted.
    public var prettyText: String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes]
        guard let data = try? enc.encode(self) else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    /// Parse JSON text typed by a person (an object, array or scalar).
    public static func parse(_ text: String) throws -> JSONValue {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return .object([:]) }
        return try JSONDecoder().decode(JSONValue.self, from: Data(trimmed.utf8))
    }
}

extension JSONValue {
    /// The Jira/GitHub ticket key a run payload carries, if any (`issue_key`, `ticket`, `key`,
    /// or `issue.key` as Jira webhooks send it).
    public var ticketKey: String? {
        for k in ["issue_key", "ticket", "ticket_key"] {
            if let s = self[k]?.stringValue, !s.isEmpty { return s }
        }
        if let s = self["issue"]?["key"]?.stringValue, !s.isEmpty { return s }
        if let s = self["key"]?.stringValue, !s.isEmpty { return s }
        return nil
    }
}
