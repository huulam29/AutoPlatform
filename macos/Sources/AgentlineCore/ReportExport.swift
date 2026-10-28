import Darwin
import Foundation

/// The machine facts a report needs (no user names, no paths, no serial numbers).
public struct SystemInfo: Sendable, Equatable {
    public var appVersion: String
    public var engineVersion: String?
    public var osVersion: String  // "macOS 27.0 (Build 26A428)"
    public var platform: String  // "macOS 27.0 arm64", the incident `platform` field
    public var chip: String
    public var model: String
    public var memoryGB: Int
    public var uptime: TimeInterval

    public init(
        appVersion: String, engineVersion: String?, osVersion: String, platform: String, chip: String,
        model: String, memoryGB: Int, uptime: TimeInterval
    ) {
        self.appVersion = appVersion
        self.engineVersion = engineVersion
        self.osVersion = osVersion
        self.platform = platform
        self.chip = chip
        self.model = model
        self.memoryGB = memoryGB
        self.uptime = uptime
    }

    public static func current(appVersion: String, engineVersion: String?) -> SystemInfo {
        let p = ProcessInfo.processInfo
        return SystemInfo(
            appVersion: appVersion, engineVersion: engineVersion,
            osVersion: "macOS \(p.operatingSystemVersionString)", platform: platformString,
            chip: sysctlString("machdep.cpu.brand_string") ?? "unknown", model: sysctlString("hw.model") ?? "unknown",
            memoryGB: Int((Double(p.physicalMemory) / 1_073_741_824).rounded()), uptime: p.systemUptime)
    }

    /// "macOS 27.0 arm64" (the same shape as the engine's `platform_text()`).
    public static var platformString: String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let version = v.patchVersion > 0
            ? "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)" : "\(v.majorVersion).\(v.minorVersion)"
        return "macOS \(version) \(machine)"
    }

    public static var machine: String {
        var u = utsname()
        uname(&u)
        return withUnsafeBytes(of: &u.machine) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
    }

    static func sysctlString(_ name: String) -> String? {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
        var buf = [CChar](repeating: 0, count: size)
        guard sysctlbyname(name, &buf, &size, nil, 0) == 0 else { return nil }
        return String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }

    public var text: String {
        let up = Int(uptime)
        return """
            agentline app: \(appVersion)
            agentline engine: \(engineVersion ?? "not running")
            OS: \(osVersion)
            Platform: \(platform)
            Chip: \(chip)
            Model: \(model)
            Memory: \(memoryGB) GB
            Uptime: \(up / 86400)d \((up % 86400) / 3600)h \((up % 3600) / 60)m

            """
    }
}

/// Builds the "Export report…" zip. `entries` (pure) decides what goes in; `write` puts it in a
/// folder that `/usr/bin/ditto -c -k --keepParent` then zips.
public enum ReportExport {
    public enum Entry: Sendable, Equatable {
        /// Copy `<incidents dir>/<from>` to `<to>` inside the report folder.
        case copy(from: String, to: String)
        case text(to: String, content: String)

        public var destination: String {
            switch self {
            case .copy(_, let to), .text(let to, _): return to
            }
        }
    }

    public static let logLines = 500

    /// `agentline-report-2026-10-19-1432` (local time; the zip adds `.zip`).
    public static func baseName(date: Date, timeZone: TimeZone = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = timeZone
        f.dateFormat = "yyyy-MM-dd-HHmm"
        return "agentline-report-\(f.string(from: date))"
    }

    /// Everything in the report: each incident's JSON and attachments under `incidents/`,
    /// `system.txt`, `engine.log` (the last 500 lines, redacted) and `summary.md`.
    public static func entries(
        incidents: [Incident], system: SystemInfo, logLines: [String], redactor: Redactor, generated: Date = Date()
    ) -> [Entry] {
        var out: [Entry] = []
        for i in incidents where IncidentStore.isValidId(i.id) {
            out.append(.copy(from: "\(i.id).json", to: "incidents/\(i.id).json"))
            for a in i.attachments where IncidentStore.isSafeRelative(a) {
                out.append(.copy(from: a, to: "incidents/\(a)"))
            }
        }
        out.append(.text(to: "system.txt", content: system.text))
        let log = redactor.redact(Array(logLines.suffix(Self.logLines)))
        out.append(.text(to: "engine.log", content: log.joined(separator: "\n") + (log.isEmpty ? "" : "\n")))
        out.append(.text(to: "summary.md", content: summaryMarkdown(incidents: incidents, system: system, generated: generated)))
        return out
    }

    /// A readable list for an issue or a chat message.
    public static func summaryMarkdown(incidents: [Incident], system: SystemInfo, generated: Date = Date()) -> String {
        let iso = ISO8601DateFormatter()
        func when(_ t: Double) -> String { iso.string(from: Date(timeIntervalSince1970: t)) }
        var md = "# agentline problem report\n\n"
        md += "Generated \(iso.string(from: generated)). App \(system.appVersion), engine \(system.engineVersion ?? "not running"), \(system.platform), \(system.chip).\n\n"
        if incidents.isEmpty {
            md += "No crashes or errors recorded.\n"
            return md
        }
        md += "| Last seen | Source | Kind | Count | Title |\n|---|---|---|---|---|\n"
        for i in incidents {
            let title = i.title.replacingOccurrences(of: "|", with: "\\|").replacingOccurrences(of: "\n", with: " ")
            md += "| \(when(i.lastSeen)) | \(i.source) | \(i.kind) | \(i.count) | \(title) |\n"
        }
        for i in incidents {
            md += "\n## \(i.title)\n\n"
            md += "- id: `\(i.id)`, \(i.source) / \(i.kind), seen \(i.count)x, first \(when(i.firstSeen)), last \(when(i.lastSeen))\n"
            md += "- version \(i.version.isEmpty ? "?" : i.version), \(i.platform)\n"
            if let t = i.exceptionType { md += "- exception: `\(t)`\n" }
            if !i.message.isEmpty { md += "\n\(i.message)\n" }
            if !i.traceback.isEmpty {
                let lines = i.traceback.split(separator: "\n", omittingEmptySubsequences: false)
                let shown = lines.count > 30 ? lines.prefix(30) + ["… (\(lines.count - 30) more lines)"] : lines
                md += "\n```\n\(shown.joined(separator: "\n"))\n```\n"
            }
            if !i.attachments.isEmpty {
                md += "\nAttachments: \(i.attachments.map { "`\($0)`" }.joined(separator: ", "))\n"
            }
        }
        return md
    }

    /// Writes `entries` into `folder` (created). Missing sources are skipped.
    public static func write(_ entries: [Entry], incidentsDir: URL, into folder: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        for e in entries {
            let dest = folder.appending(path: e.destination)
            try fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
            switch e {
            case .copy(let from, _):
                let src = incidentsDir.appending(path: from)
                guard fm.fileExists(atPath: src.path) else { continue }
                if fm.fileExists(atPath: dest.path) { try fm.removeItem(at: dest) }
                try fm.copyItem(at: src, to: dest)
            case .text(_, let content):
                try Data(content.utf8).write(to: dest)
            }
        }
    }

    /// `folder` -> `zip` with `/usr/bin/ditto -c -k --keepParent` (blocking; call off the main actor).
    public static func zip(folder: URL, to zip: URL) throws {
        if FileManager.default.fileExists(atPath: zip.path) { try FileManager.default.removeItem(at: zip) }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        p.arguments = ["-c", "-k", "--sequesterRsrc", "--keepParent", folder.path, zip.path]
        let err = Pipe()
        p.standardError = err
        try p.run()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else {
            let msg = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            throw CocoaError(.fileWriteUnknown, userInfo: [NSLocalizedDescriptionKey: "ditto failed: \(msg)"])
        }
    }
}
