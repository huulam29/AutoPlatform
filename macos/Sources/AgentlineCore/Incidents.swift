import CryptoKit
import Darwin
import Foundation

// Crash and failure reports ("incidents"), shared with the engine: one `<id>.json` per distinct
// problem in `$AGENTLINE_HOME/incidents/`, plus an optional `<id>/` folder of attachments.
// Stored locally only; nothing here ever sends a report anywhere.

public enum IncidentKind {
    public static let appCrash = "app_crash"
    public static let appException = "app_exception"
    public static let appUncleanExit = "app_unclean_exit"
    public static let engineExit = "engine_exit"
    public static let engineException = "engine_exception"
    public static let apiError = "api_error"
    public static let workerError = "worker_error"

    public static let all = [appCrash, appException, appUncleanExit, engineExit, engineException, apiError, workerError]

    /// Kinds that mean "the app itself died last time" (the launch banner).
    public static let appDeaths: Set<String> = [appCrash, appException, appUncleanExit]

    public static func label(_ kind: String) -> String {
        switch kind {
        case appCrash: return "App crash"
        case appException: return "App exception"
        case appUncleanExit: return "Unexpected quit"
        case engineExit: return "Engine exit"
        case engineException: return "Engine exception"
        case apiError: return "API error"
        case workerError: return "Worker error"
        default: return kind
        }
    }
}

/// One incident file. Decoding is lenient: the engine's list endpoint sends summaries without
/// `traceback` / `context`, and an older or newer writer may add or drop fields.
public struct Incident: Codable, Sendable, Identifiable, Hashable {
    public var id: String
    public var fingerprint: String
    public var source: String
    public var kind: String
    public var title: String
    public var message: String
    public var exceptionType: String?
    public var traceback: String
    public var context: JSONValue
    public var firstSeen: Double
    public var lastSeen: Double
    public var count: Int
    public var version: String
    public var platform: String
    public var status: String
    public var attachments: [String]

    public static let statusNew = "new"
    public static let statusSeen = "seen"

    public var isNew: Bool { status == Self.statusNew }
    public var lastSeenDate: Date { Date(timeIntervalSince1970: lastSeen) }
    public var firstSeenDate: Date { Date(timeIntervalSince1970: firstSeen) }

    enum CodingKeys: String, CodingKey {
        case id, fingerprint, source, kind, title, message, traceback, context, count, version, platform, status,
            attachments
        case exceptionType = "exception_type"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }

    public init(
        id: String, fingerprint: String, source: String, kind: String, title: String, message: String,
        exceptionType: String?, traceback: String, context: JSONValue, firstSeen: Double, lastSeen: Double,
        count: Int, version: String, platform: String, status: String, attachments: [String]
    ) {
        self.id = id
        self.fingerprint = fingerprint
        self.source = source
        self.kind = kind
        self.title = title
        self.message = message
        self.exceptionType = exceptionType
        self.traceback = traceback
        self.context = context
        self.firstSeen = firstSeen
        self.lastSeen = lastSeen
        self.count = count
        self.version = version
        self.platform = platform
        self.status = status
        self.attachments = attachments
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fingerprint = try c.decodeIfPresent(String.self, forKey: .fingerprint) ?? ""
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? String(fingerprint.prefix(16))
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? "engine"
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
        exceptionType = try c.decodeIfPresent(String.self, forKey: .exceptionType)
        traceback = try c.decodeIfPresent(String.self, forKey: .traceback) ?? ""
        context = try c.decodeIfPresent(JSONValue.self, forKey: .context) ?? .object([:])
        firstSeen = try c.decodeIfPresent(Double.self, forKey: .firstSeen) ?? 0
        lastSeen = try c.decodeIfPresent(Double.self, forKey: .lastSeen) ?? firstSeen
        count = try c.decodeIfPresent(Int.self, forKey: .count) ?? 1
        version = try c.decodeIfPresent(String.self, forKey: .version) ?? ""
        platform = try c.decodeIfPresent(String.self, forKey: .platform) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? Self.statusNew
        attachments = try c.decodeIfPresent([String].self, forKey: .attachments) ?? []
    }

    /// Every field, `exception_type` as an explicit null when absent.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(fingerprint, forKey: .fingerprint)
        try c.encode(source, forKey: .source)
        try c.encode(kind, forKey: .kind)
        try c.encode(title, forKey: .title)
        try c.encode(message, forKey: .message)
        if let exceptionType {
            try c.encode(exceptionType, forKey: .exceptionType)
        } else {
            try c.encodeNil(forKey: .exceptionType)
        }
        try c.encode(traceback, forKey: .traceback)
        try c.encode(context, forKey: .context)
        try c.encode(firstSeen, forKey: .firstSeen)
        try c.encode(lastSeen, forKey: .lastSeen)
        try c.encode(count, forKey: .count)
        try c.encode(version, forKey: .version)
        try c.encode(platform, forKey: .platform)
        try c.encode(status, forKey: .status)
        try c.encode(attachments, forKey: .attachments)
    }

    /// `context.log_tail` as lines, when present.
    public var logTail: [String] {
        context["log_tail"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }
}

public struct IncidentsResponse: Codable, Sendable {
    public let incidents: [Incident]
    public let dir: String?
}

/// What a writer knows about a problem before it becomes (or merges into) an incident file.
public struct IncidentDraft: Sendable, Equatable {
    public var source: String
    public var kind: String
    public var title: String
    public var message: String
    public var exceptionType: String?
    public var traceback: String
    public var context: [String: JSONValue]
    /// The top frames that identify the problem (part of the fingerprint, not stored as such).
    public var frames: [String]
    public var version: String
    public var platform: String

    public init(
        source: String = "app", kind: String, title: String, message: String, exceptionType: String? = nil,
        traceback: String = "", context: [String: JSONValue] = [:], frames: [String] = [], version: String,
        platform: String
    ) {
        self.source = source
        self.kind = kind
        self.title = title
        self.message = message
        self.exceptionType = exceptionType
        self.traceback = traceback
        self.context = context
        self.frames = frames
        self.version = version
        self.platform = platform
    }

    public var fingerprint: String {
        Fingerprint.compute(
            source: source, kind: kind, exceptionType: exceptionType, message: message,
            frames: Fingerprint.topFrames(frames))
    }

    public var id: String { String(fingerprint.prefix(16)) }

    /// The engine's limits: title 300 characters, message 20 000, the last 20 000 of a traceback.
    public static let maxTitle = 300
    public static let maxText = 20_000

    /// Every text field passed through `redactor` and cut to the limits (the store does this
    /// before writing, so the fingerprint never depends on a secret).
    public func redacted(_ redactor: Redactor) -> IncidentDraft {
        var d = self
        d.title = String(redactor.redact(title).prefix(Self.maxTitle))
        d.message = String(redactor.redact(message).prefix(Self.maxText))
        d.exceptionType = exceptionType.map { redactor.redact($0) }
        d.traceback = String(redactor.redact(traceback).suffix(Self.maxText))
        d.context = context.mapValues { redactor.redact($0) }
        d.frames = frames.map { redactor.redact($0) }
        return d
    }
}

/// `sha256(source|kind|exception_type|normalized message|top frames)`, hex: the same rule as
/// the engine's `agentline.incidents.fingerprint`, so both writers agree on ids.
///
/// The message is normalized as the engine does it: first line only, trimmed, at most 300
/// characters, then hex addresses -> `<hex>`, long hex ids -> `<id>`, digit runs -> `N`.
/// `exception_type` is empty when absent; frames are joined by `,` as given (callers pass the
/// result of `topFrames`: at most `frameCount`, stripped of index, address and offset).
public enum Fingerprint {
    public static let frameCount = 3

    public static func compute(
        source: String, kind: String, exceptionType: String?, message: String, frames: [String]
    ) -> String {
        let material = [source, kind, exceptionType ?? "", normalize(message), frames.joined(separator: ",")]
            .joined(separator: "|")
        return SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private static let rules: [(NSRegularExpression, String)] = [
        (try! NSRegularExpression(pattern: "0x[0-9a-fA-F]+"), "<hex>"),
        (try! NSRegularExpression(pattern: "\\b[0-9a-f]{12,}\\b"), "<id>"),
        (try! NSRegularExpression(pattern: "\\b(?:r|x)[0-9a-f]{16,}\\b"), "<id>"),
        (try! NSRegularExpression(pattern: "\\d+"), "N"),
    ]
    private static let frameIndex = try! NSRegularExpression(pattern: "^\\s*[0-9]+\\s+")
    private static let address = try! NSRegularExpression(pattern: "\\s*0x[0-9a-fA-F]+\\s*")
    private static let offset = try! NSRegularExpression(pattern: "\\s*\\+\\s*[0-9]+\\s*$")
    private static let spaces = try! NSRegularExpression(pattern: "\\s+")

    private static func replace(_ re: NSRegularExpression, in s: String, with t: String) -> String {
        re.stringByReplacingMatches(in: s, options: [], range: NSRange(s.startIndex..., in: s), withTemplate: t)
    }

    public static func normalize(_ message: String) -> String {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let first = trimmed.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline).first ?? ""
        var out = String(first.prefix(300))
        for (re, t) in rules { out = replace(re, in: out, with: t) }
        return out
    }

    /// `"3   Agentline   0x1000abc  AppModel.launch() + 120"` -> `"Agentline AppModel.launch()"`.
    public static func normalizeFrame(_ frame: String) -> String {
        var s = replace(frameIndex, in: frame, with: "")
        s = replace(offset, in: s, with: "")
        s = replace(address, in: s, with: " ")
        s = replace(spaces, in: s, with: " ")
        return s.trimmingCharacters(in: .whitespaces)
    }

    /// The first `frameCount` frames, normalized, empty ones dropped.
    public static func topFrames(_ frames: [String]) -> [String] {
        Array(frames.map(normalizeFrame).filter { !$0.isEmpty }.prefix(frameCount))
    }
}

/// Reads and writes the incidents directory. Every write is redacted first and atomic
/// (a dot-prefixed temp file in the same directory, then `rename`).
public struct IncidentStore: Sendable {
    public let dir: URL
    public var redactor: Redactor

    public init(dir: URL, redactor: Redactor = Redactor()) {
        self.dir = dir
        self.redactor = redactor
    }

    public init(paths: AppPaths, redactor: Redactor = Redactor()) {
        self.init(dir: paths.home.appending(path: "incidents", directoryHint: .isDirectory), redactor: redactor)
    }

    /// Ids are 16 lowercase hex characters; anything else is refused (no path tricks).
    public static func isValidId(_ id: String) -> Bool {
        id.count == 16 && id.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }

    public func file(for id: String) -> URL { dir.appending(path: "\(id).json") }
    public func attachmentsDir(for id: String) -> URL { dir.appending(path: id, directoryHint: .isDirectory) }

    /// A relative attachment path as a file URL, or nil if it would leave the incidents dir.
    public func url(forAttachment relative: String) -> URL? {
        guard Self.isSafeRelative(relative) else { return nil }
        return dir.appending(path: relative)
    }

    static func isSafeRelative(_ p: String) -> Bool {
        !p.isEmpty && !p.hasPrefix("/") && !p.split(separator: "/").contains("..")
    }

    // MARK: writing

    /// Write `draft` as a new incident, or merge it into the one with the same fingerprint:
    /// `count + 1`, new `last_seen`, title, message, traceback and context, `status` back to
    /// "new", `first_seen` kept. Each file in `attach` is copied into `<id>/` (text reports are
    /// redacted on the way) and listed in `attachments`.
    @discardableResult
    public func record(_ draft: IncidentDraft, attach: [URL] = [], now: Date = Date()) throws -> Incident {
        let d = draft.redacted(redactor)
        return try locked { try recordUnlocked(d, attach: attach, now: now) }
    }

    private func recordUnlocked(_ d: IncidentDraft, attach: [URL], now: Date) throws -> Incident {
        let fp = d.fingerprint
        let id = String(fp.prefix(16))
        let t = now.timeIntervalSince1970
        var incident: Incident
        if var old = get(id), old.fingerprint == fp {
            old.count += 1
            old.lastSeen = t
            old.title = d.title
            old.message = d.message
            old.exceptionType = d.exceptionType
            old.traceback = d.traceback
            old.context = .object(d.context)
            old.version = d.version
            old.platform = d.platform
            old.status = Incident.statusNew
            incident = old
        } else {
            incident = Incident(
                id: id, fingerprint: fp, source: d.source, kind: d.kind, title: d.title, message: d.message,
                exceptionType: d.exceptionType, traceback: d.traceback, context: .object(d.context), firstSeen: t,
                lastSeen: t, count: 1, version: d.version, platform: d.platform, status: Incident.statusNew,
                attachments: [])
        }
        for src in attach {
            if let rel = try? copyAttachment(src, id: id), !incident.attachments.contains(rel) {
                incident.attachments.append(rel)
            }
        }
        try save(incident)
        return incident
    }

    /// Add a file to an existing incident without counting a new occurrence.
    @discardableResult
    public func attach(_ src: URL, to id: String) throws -> Incident? {
        try locked {
            guard var incident = get(id) else { return nil }
            let rel = try copyAttachment(src, id: id)
            if !incident.attachments.contains(rel) { incident.attachments.append(rel) }
            try save(incident)
            return incident
        }
    }

    /// Merge extra context keys into an existing incident (no new occurrence).
    public func updateContext(_ id: String, _ extra: [String: JSONValue]) throws {
        try locked {
            guard var incident = get(id) else { return }
            var ctx: [String: JSONValue] = [:]
            if case .object(let o) = incident.context { ctx = o }
            for (k, v) in extra { ctx[k] = redactor.redact(v) }
            incident.context = .object(ctx)
            try save(incident)
        }
    }

    /// Runs `body` holding `flock(LOCK_EX)` on `<dir>/.lock`: the lock the engine's
    /// `IncidentStore` takes, so the app and the engine never clobber each other's merges.
    /// Not re-entrant (flock is per open file): never call a locking method from `body`.
    public func locked<T>(_ body: () throws -> T) throws -> T {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let fd = open(dir.appending(path: ".lock").path, O_CREAT | O_RDWR | O_CLOEXEC, 0o644)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        while flock(fd, LOCK_EX) != 0 {
            guard errno == EINTR else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }

    private static let textExtensions: Set<String> = ["ips", "crash", "txt", "log", "json", "diag"]

    private func copyAttachment(_ src: URL, id: String) throws -> String {
        let folder = attachmentsDir(for: id)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let name = src.lastPathComponent
        let dest = folder.appending(path: name)
        if Self.textExtensions.contains(src.pathExtension.lowercased()),
            let data = try? Data(contentsOf: src), let text = String(data: data, encoding: .utf8)
        {
            try Self.atomicWrite(Data(redactor.redact(text).utf8), to: dest)
        } else {
            if FileManager.default.fileExists(atPath: dest.path) { try FileManager.default.removeItem(at: dest) }
            try FileManager.default.copyItem(at: src, to: dest)
        }
        return "\(id)/\(name)"
    }

    private func save(_ incident: Incident) throws {
        guard Self.isValidId(incident.id) else { throw CocoaError(.fileWriteInvalidFileName) }
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Self.atomicWrite(Self.encode(incident), to: file(for: incident.id))
    }

    public static func encode(_ incident: Incident) throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try enc.encode(incident)
    }

    /// Temp file (dot-prefixed, same directory) + `rename(2)`: readers never see half a file.
    public static func atomicWrite(_ data: Data, to url: URL) throws {
        let tmp = url.deletingLastPathComponent()
            .appending(path: ".\(url.lastPathComponent).tmp-\(getpid())-\(UInt32.random(in: 0...UInt32.max))")
        try data.write(to: tmp)
        guard rename(tmp.path, url.path) == 0 else {
            let err = errno
            try? FileManager.default.removeItem(at: tmp)
            throw POSIXError(POSIXErrorCode(rawValue: err) ?? .EIO)
        }
    }

    // MARK: reading and housekeeping

    public func get(_ id: String) -> Incident? {
        guard Self.isValidId(id), let data = try? Data(contentsOf: file(for: id)) else { return nil }
        return try? JSONDecoder().decode(Incident.self, from: data)
    }

    /// All readable incidents, most recently seen first.
    public func list() -> [Incident] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return names.filter { $0.hasSuffix(".json") && !$0.hasPrefix(".") }
            .compactMap { get(String($0.dropLast(5))) }
            .sorted { $0.lastSeen != $1.lastSeen ? $0.lastSeen > $1.lastSeen : $0.id < $1.id }
    }

    public func markSeen(_ id: String) throws {
        try locked {
            guard var incident = get(id) else { return }
            incident.status = Incident.statusSeen
            try save(incident)
        }
    }

    /// Removes `<id>.json` and `<id>/`.
    public func delete(_ id: String) throws {
        guard Self.isValidId(id) else { return }
        try locked {
            let fm = FileManager.default
            if fm.fileExists(atPath: file(for: id).path) { try fm.removeItem(at: file(for: id)) }
            if fm.fileExists(atPath: attachmentsDir(for: id).path) { try fm.removeItem(at: attachmentsDir(for: id)) }
        }
    }

    /// Removes every incident and attachments folder (other files in the directory stay).
    public func clear() throws {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        var ids = Set<String>()
        for n in names {
            let base = n.hasSuffix(".json") ? String(n.dropLast(5)) : n
            if Self.isValidId(base) { ids.insert(base) }
        }
        for id in ids { try delete(id) }
    }
}

/// At most one notification per incident per `interval` (engine crash loops stay quiet).
public struct IncidentNotifyLimiter: Sendable {
    public let interval: TimeInterval
    private var last: [String: Date] = [:]

    public init(interval: TimeInterval = 600) { self.interval = interval }

    public mutating func shouldNotify(_ id: String, now: Date = Date()) -> Bool {
        if let t = last[id], now.timeIntervalSince(t) < interval { return false }
        last[id] = now
        return true
    }
}
