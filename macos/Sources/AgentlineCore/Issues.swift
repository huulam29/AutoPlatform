import Foundation

/// One tracked issue per problem (a crash/error report or a failed run). The engine keeps them
/// in `<home>/issues/<number>.json` and creates them (GET /api/issues syncs); the app changes
/// them through the API and reads the files when the engine is down.
public struct Issue: Codable, Sendable, Identifiable, Hashable {
    public struct Source: Codable, Sendable, Hashable {
        public let kind: String  // incident | run
        public let ref: String
    }

    public struct Note: Codable, Sendable, Hashable {
        public let at: Double
        public let text: String
    }

    public struct Change: Codable, Sendable, Hashable {
        public let at: Double
        public let from: String?
        public let to: String
        public let note: String?
    }

    public let number: Int
    public let title: String
    public let status: String
    public let source: Source
    public let detail: String?
    public let createdAt: Double
    public let updatedAt: Double
    public let occurrences: Int?
    public let notes: [Note]
    public let history: [Change]

    public var id: Int { number }
    public var isClosed: Bool { IssueStatus.closed.contains(status) }
    /// The Problems list tag of the problem this issue tracks.
    public var problemTag: String { (source.kind == "run" ? "r:" : "i:") + source.ref }

    enum CodingKeys: String, CodingKey {
        case number, title, status, source, detail, occurrences, notes, history
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        number = try c.decode(Int.self, forKey: .number)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "open"
        source = try c.decode(Source.self, forKey: .source)
        detail = try c.decodeIfPresent(String.self, forKey: .detail)
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        updatedAt = try c.decodeIfPresent(Double.self, forKey: .updatedAt) ?? 0
        occurrences = try c.decodeIfPresent(Int.self, forKey: .occurrences)
        notes = try c.decodeIfPresent([Note].self, forKey: .notes) ?? []
        history = try c.decodeIfPresent([Change].self, forKey: .history) ?? []
    }
}

public struct IssuesResponse: Codable, Sendable {
    public let issues: [Issue]
    public let counts: [String: Int]?
}

public enum IssueStatus {
    public static let all = ["open", "in_progress", "fixed", "wont_fix"]
    public static let closed: Set<String> = ["fixed", "wont_fix"]

    public static func label(_ s: String) -> String {
        switch s {
        case "open": "Open"
        case "in_progress": "In progress"
        case "fixed": "Fixed"
        case "wont_fix": "Won't fix"
        default: s
        }
    }
}

/// The Problems screen's filter by issue status.
public enum IssueFilter: String, CaseIterable, Sendable {
    case active, closed, all

    public var label: String {
        switch self {
        case .active: "Open"
        case .closed: "Closed"
        case .all: "All"
        }
    }

    /// A problem without an issue yet counts as open.
    public func shows(_ issue: Issue?) -> Bool {
        switch self {
        case .all: true
        case .active: !(issue?.isClosed ?? false)
        case .closed: issue?.isClosed ?? false
        }
    }
}

/// Read-only access to the issue files, for when the engine is down.
public struct IssueFiles: Sendable {
    public let dir: URL

    public init(home: URL) { dir = home.appendingPathComponent("issues", isDirectory: true) }

    public func list() -> [Issue] {
        let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" }
            .compactMap { try? JSONDecoder().decode(Issue.self, from: Data(contentsOf: $0)) }
            .sorted { $0.updatedAt > $1.updatedAt }
    }
}
