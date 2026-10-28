import Darwin
import Foundation

// App crash capture: macOS crash reports (.ips / .crash), the running marker that detects an
// unclean exit, and the drafts for app exceptions and engine exits. All pure or file-only, so
// the app target holds just the AppKit glue.

/// What a macOS crash report says, as far as it could be parsed.
public struct CrashReportSummary: Sendable, Equatable {
    public var fileName: String
    public var exceptionType: String?  // "EXC_BREAKPOINT (SIGTRAP)"
    public var signal: String?
    public var message: String?  // application-specific information, else the termination reason
    public var frames: [String] = []  // crashed thread (or the last exception backtrace), top first
    public var appFrame: String?  // the first frame in agentline's own binary
    /// Frames in agentline's own binary: they tell two crashes apart better than the runtime
    /// frames above them (every `fatalError` starts in libswiftCore).
    public var appFrames: [String] = []
    public var pid: Int32?
    public var timestamp: Date?
    public var appVersion: String?
    public var osVersion: String?
    public var parsed = false

    public init(fileName: String) { self.fileName = fileName }

    /// "Crash: EXC_BREAKPOINT (SIGTRAP) in AppModel.launch()", or a generic title.
    public var title: String {
        guard parsed, let type = exceptionType ?? signal else { return "agentline crashed" }
        if let where_ = appFrame ?? frames.first { return "Crash: \(type) in \(where_)" }
        return "Crash: \(type)"
    }

    public func draft(version: String, platform: String) -> IncidentDraft {
        var ctx: [String: JSONValue] = ["crash_report": .string(fileName)]
        if let pid { ctx["pid"] = .number(Double(pid)) }
        if let signal { ctx["signal"] = .string(signal) }
        if let osVersion { ctx["os_version"] = .string(osVersion) }
        if let timestamp { ctx["crashed_at"] = .number(timestamp.timeIntervalSince1970) }
        return IncidentDraft(
            kind: IncidentKind.appCrash, title: title,
            message: message ?? (parsed ? title : "macOS wrote a crash report for agentline (\(fileName))."),
            exceptionType: exceptionType ?? signal,
            traceback: frames.enumerated().map { "\($0.offset)  \($0.element)" }.joined(separator: "\n"),
            context: ctx, frames: appFrames.isEmpty ? frames : appFrames, version: appVersion ?? version, platform: platform)
    }
}

public enum CrashReportParser {
    public static let appImageName = "Agentline"

    public static func parse(_ text: String, fileName: String) -> CrashReportSummary {
        if fileName.lowercased().hasSuffix(".ips"), let s = parseIPS(text, fileName: fileName) { return s }
        return parseLegacy(text, fileName: fileName)
    }

    /// `.ips` (macOS 12+): one JSON header line, then the JSON body.
    public static func parseIPS(_ text: String, fileName: String) -> CrashReportSummary? {
        guard let nl = text.firstIndex(of: "\n") else { return nil }
        let headerText = text[..<nl]
        let bodyText = text[text.index(after: nl)...]
        let dec = JSONDecoder()
        guard let header = try? dec.decode(JSONValue.self, from: Data(headerText.utf8)),
            let body = try? dec.decode(JSONValue.self, from: Data(bodyText.utf8))
        else { return nil }
        var s = CrashReportSummary(fileName: fileName)
        s.parsed = true
        s.appVersion = header["app_version"]?.stringValue
        s.osVersion = header["os_version"]?.stringValue
        if let ts = header["timestamp"]?.stringValue ?? body["captureTime"]?.stringValue {
            s.timestamp = parseDate(ts)
        }
        if case .number(let n)? = body["pid"] { s.pid = Int32(n) }
        if let exc = body["exception"] {
            let type = exc["type"]?.stringValue
            let sig = exc["signal"]?.stringValue
            s.signal = sig
            switch (type, sig) {
            case (let t?, let g?): s.exceptionType = "\(t) (\(g))"
            case (let t?, nil): s.exceptionType = t
            case (nil, let g?): s.exceptionType = g
            default: break
            }
        }
        // Application Specific Information: Swift's "Fatal error: …", NSException's reason.
        var asi: [String] = []
        if case .object(let o)? = body["asi"] {
            for key in o.keys.sorted() {
                for line in o[key]?.arrayValue ?? [] { if let l = line.stringValue { asi.append(l) } }
            }
        }
        if !asi.isEmpty {
            s.message = asi.joined(separator: "\n")
        } else if let ind = body["termination"]?["indicator"]?.stringValue {
            s.message = ind
        }
        let images: [String] = (body["usedImages"]?.arrayValue ?? []).map { $0["name"]?.stringValue ?? "???" }
        func frameText(_ f: JSONValue) -> (String, Bool) {
            var image = "???"
            if case .number(let i)? = f["imageIndex"], Int(i) >= 0, Int(i) < images.count { image = images[Int(i)] }
            let symbol = f["symbol"]?.stringValue
            return (symbol.map { "\($0) (\(image))" } ?? "??? (\(image))", image == appImageName)
        }
        // An NSException: its backtrace says more than the abort() on the crashed thread.
        var raw: [JSONValue] = body["lastExceptionBacktrace"]?.arrayValue ?? []
        if raw.isEmpty {
            let threads = body["threads"]?.arrayValue ?? []
            var crashed: JSONValue?
            if case .number(let i)? = body["faultingThread"], Int(i) >= 0, Int(i) < threads.count {
                crashed = threads[Int(i)]
            }
            crashed = crashed ?? threads.first { if case .bool(true)? = $0["triggered"] { return true } else { return false } }
            raw = crashed?["frames"]?.arrayValue ?? []
        }
        for f in raw {
            let (t, ours) = frameText(f)
            s.frames.append(t)
            if ours {
                if s.appFrame == nil { s.appFrame = f["symbol"]?.stringValue }
                s.appFrames.append(t)
            }
        }
        return s
    }

    /// The older plain-text `.crash` format: just the headline fields and the crashed thread.
    public static func parseLegacy(_ text: String, fileName: String) -> CrashReportSummary {
        var s = CrashReportSummary(fileName: fileName)
        let lines = text.components(separatedBy: "\n")
        var inCrashed = false
        for line in lines {
            if line.hasPrefix("Exception Type:") {
                s.exceptionType = value(line)
                s.parsed = true
            } else if line.hasPrefix("Process:"), let open = line.lastIndex(of: "["), let close = line.lastIndex(of: "]"),
                open < close
            {
                s.pid = Int32(line[line.index(after: open)..<close])
            } else if line.hasPrefix("Termination Reason:") || line.hasPrefix("Application Specific Information:") {
                s.message = s.message ?? value(line)
            } else if line.range(of: #"^Thread \d+ Crashed"#, options: .regularExpression) != nil {
                inCrashed = true
            } else if inCrashed {
                if line.trimmingCharacters(in: .whitespaces).isEmpty {
                    inCrashed = false
                    continue
                }
                let frame = Fingerprint.normalizeFrame(line)
                s.frames.append(frame)
                if frame.hasPrefix(appImageName + " ") {
                    if s.appFrame == nil { s.appFrame = String(frame.dropFirst(appImageName.count + 1)) }
                    s.appFrames.append(frame)
                }
            }
        }
        if let t = s.exceptionType, let r = t.range(of: #"SIG[A-Z]+"#, options: .regularExpression) {
            s.signal = String(t[r])
        }
        return s
    }

    private static func value(_ line: String) -> String? {
        guard let c = line.firstIndex(of: ":") else { return nil }
        let v = line[line.index(after: c)...].trimmingCharacters(in: .whitespaces)
        return v.isEmpty ? nil : v
    }

    /// "2026-10-19 12:34:56.00 -0500" (the .ips header and captureTime format).
    static func parseDate(_ s: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        for format in ["yyyy-MM-dd HH:mm:ss.SS Z", "yyyy-MM-dd HH:mm:ss.SSSS Z", "yyyy-MM-dd HH:mm:ss Z"] {
            f.dateFormat = format
            if let d = f.date(from: s) { return d }
        }
        return nil
    }
}

// MARK: - finding new crash reports

public struct DiagnosticFile: Sendable, Equatable {
    public let url: URL
    public let modified: Date
    public var name: String { url.lastPathComponent }

    public init(url: URL, modified: Date) {
        self.url = url
        self.modified = modified
    }
}

/// Remembered between launches in `<app support>/crash-scan.json`.
public struct CrashScanState: Codable, Sendable, Equatable {
    public var lastScan: Double?
    public var processed: [String]

    public init(lastScan: Double? = nil, processed: [String] = []) {
        self.lastScan = lastScan
        self.processed = processed
    }

    enum CodingKeys: String, CodingKey {
        case lastScan = "last_scan"
        case processed
    }

    public static func load(_ url: URL) -> CrashScanState {
        guard let data = try? Data(contentsOf: url) else { return CrashScanState() }
        return (try? JSONDecoder().decode(CrashScanState.self, from: data)) ?? CrashScanState()
    }

    public func save(_ url: URL) {
        guard let data = try? JSONEncoder().encode(self) else { return }
        try? IncidentStore.atomicWrite(data, to: url)
    }

    /// Remember these files and the scan time (the newest 300 names are kept).
    public mutating func note(_ files: [DiagnosticFile], at now: Date) {
        lastScan = now.timeIntervalSince1970
        processed = Array((processed + files.map(\.name).filter { !processed.contains($0) }).suffix(300))
    }
}

public enum CrashScanner {
    /// Per user, then system-wide (readable only with admin rights; skipped otherwise).
    public static var defaultDirs: [URL] {
        [
            URL(fileURLWithPath: NSHomeDirectory()).appending(path: "Library/Logs/DiagnosticReports"),
            URL(fileURLWithPath: "/Library/Logs/DiagnosticReports"),
        ]
    }

    /// The first scan looks this far back.
    public static let firstScanWindow: TimeInterval = 7 * 24 * 3600
    /// Files are taken a little before the last scan too (reports are written as the scan runs);
    /// the processed list stops them from being taken twice.
    public static let slack: TimeInterval = 120

    /// `Agentline-2026-10-19-123456.ips`, `Agentline_….crash`, …
    public static func isAgentlineReport(_ name: String) -> Bool {
        let lower = name.lowercased()
        guard lower.hasSuffix(".ips") || lower.hasSuffix(".crash") else { return false }
        guard name.hasPrefix(CrashReportParser.appImageName) else { return false }
        let rest = name.dropFirst(CrashReportParser.appImageName.count)
        return rest.first.map { "-_.".contains($0) } ?? false
    }

    /// The reports this scan should record, oldest first.
    public static func select(_ files: [DiagnosticFile], state: CrashScanState, now: Date) -> [DiagnosticFile] {
        let since = (state.lastScan.map { Date(timeIntervalSince1970: $0) } ?? now.addingTimeInterval(-firstScanWindow))
            .addingTimeInterval(-slack)
        return files
            .filter { isAgentlineReport($0.name) && !state.processed.contains($0.name) && $0.modified > since }
            .sorted { $0.modified < $1.modified }
    }

    public static func list(dirs: [URL] = defaultDirs) -> [DiagnosticFile] {
        let fm = FileManager.default
        var out: [DiagnosticFile] = []
        for dir in dirs {
            guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { continue }
            for n in names where isAgentlineReport(n) {
                let url = dir.appending(path: n)
                guard fm.isReadableFile(atPath: url.path),
                    let attrs = try? fm.attributesOfItem(atPath: url.path),
                    let m = attrs[.modificationDate] as? Date
                else { continue }
                out.append(DiagnosticFile(url: url, modified: m))
            }
        }
        return out
    }
}

// MARK: - unclean exit

/// `<app support>/running.marker`: written at launch, removed on a clean quit. Still there at
/// the next launch means the app didn't quit cleanly.
public struct RunMarker: Codable, Sendable, Equatable {
    public var pid: Int32
    public var startedAt: Double
    public var version: String
    /// Set by the uncaught-exception handler once it recorded the exception.
    public var exceptionIncident: String?

    public init(pid: Int32, startedAt: Double, version: String, exceptionIncident: String? = nil) {
        self.pid = pid
        self.startedAt = startedAt
        self.version = version
        self.exceptionIncident = exceptionIncident
    }

    enum CodingKeys: String, CodingKey {
        case pid, version
        case startedAt = "started_at"
        case exceptionIncident = "exception_incident"
    }

    public static func read(_ url: URL) -> RunMarker? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        // An unreadable marker still means an unclean exit; the details are just lost.
        return (try? JSONDecoder().decode(RunMarker.self, from: data)) ?? RunMarker(pid: 0, startedAt: 0, version: "")
    }

    public func write(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try IncidentStore.atomicWrite(JSONEncoder().encode(self), to: url)
    }

    public static func remove(_ url: URL) { try? FileManager.default.removeItem(at: url) }
}

public enum LaunchCheck {
    public enum Verdict: Sendable, Equatable {
        /// No marker: the last run quit cleanly (or this is the first launch).
        case clean
        /// The marker belongs to an app that is still running (a second instance).
        case otherInstance
        /// A crash report or a recorded exception explains the marker.
        case explained
        /// Nothing explains it: Force Quit, `kill -9`, power loss, or a report still to come.
        case uncleanExit(RunMarker)
    }

    /// Pure: what the previous marker means, given the crash reports found so far.
    public static func evaluate(
        previous: RunMarker?, reports: [CrashReportSummary], previousStillRunning: Bool
    ) -> Verdict {
        guard let m = previous else { return .clean }
        if previousStillRunning { return .otherInstance }
        if m.exceptionIncident != nil { return .explained }
        for r in reports {
            if let pid = r.pid, m.pid > 0 {
                if pid == m.pid { return .explained }
            } else if let t = r.timestamp, m.startedAt > 0, t.timeIntervalSince1970 >= m.startedAt {
                return .explained
            }
        }
        return .uncleanExit(m)
    }

    /// Is `pid` alive and an agentline app (not a reused pid of something else)?
    public static func isAgentlineRunning(pid: Int32) -> Bool {
        guard pid > 1, pid != getpid(), kill(pid, 0) == 0 || errno == EPERM else { return false }
        var buf = [CChar](repeating: 0, count: 4096)
        guard proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 else { return false }
        let path = String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        return (path as NSString).lastPathComponent == CrashReportParser.appImageName
    }

    public static func uncleanExitDraft(_ m: RunMarker, version: String, platform: String) -> IncidentDraft {
        var ctx: [String: JSONValue] = [:]
        if m.pid > 0 { ctx["pid"] = .number(Double(m.pid)) }
        if m.startedAt > 0 { ctx["started_at"] = .number(m.startedAt) }
        if !m.version.isEmpty { ctx["previous_version"] = .string(m.version) }
        return IncidentDraft(
            kind: IncidentKind.appUncleanExit, title: "agentline quit unexpectedly",
            message:
                "The app didn't quit cleanly last time and macOS left no crash report: Force Quit, kill -9, a hang that was killed, or a power loss.",
            context: ctx, version: version, platform: platform)
    }
}

// MARK: - app exceptions and engine exits

public enum ExceptionReport {
    public static func draft(
        name: String, reason: String?, callStack: [String], version: String, platform: String
    ) -> IncidentDraft {
        let reasonText = reason ?? ""
        // Skip the frames that raise every exception, so the fingerprint points at the caller.
        let plumbing = ["__exceptionPreprocess", "objc_exception_throw", "-[NSException raise]"]
        let meaningful = callStack.filter { f in !plumbing.contains { f.contains($0) } }
        return IncidentDraft(
            kind: IncidentKind.appException,
            title: "Uncaught exception: \(name)\(reasonText.isEmpty ? "" : " — \(reasonText.prefix(120))")",
            message: reasonText.isEmpty ? name : reasonText, exceptionType: name,
            traceback: callStack.joined(separator: "\n"), context: ["thread": .string(Thread.isMainThread ? "main" : "background")],
            frames: meaningful, version: version, platform: platform)
    }
}

public enum EngineExitReport {
    public static let logTailLines = 200

    public static func signalName(_ n: Int32) -> String {
        let names: [Int32: String] = [
            SIGHUP: "SIGHUP", SIGINT: "SIGINT", SIGQUIT: "SIGQUIT", SIGILL: "SIGILL", SIGTRAP: "SIGTRAP",
            SIGABRT: "SIGABRT", SIGBUS: "SIGBUS", SIGFPE: "SIGFPE", SIGKILL: "SIGKILL", SIGSEGV: "SIGSEGV",
            SIGPIPE: "SIGPIPE", SIGALRM: "SIGALRM", SIGTERM: "SIGTERM", SIGUSR1: "SIGUSR1", SIGUSR2: "SIGUSR2",
            SIGXCPU: "SIGXCPU",
        ]
        return names[n] ?? "signal \(n)"
    }

    /// The last Python traceback in the log, through its final `SomeError: message` line.
    public static func lastTraceback(_ lines: [String]) -> [String]? {
        guard let start = lines.lastIndex(where: { $0.hasPrefix("Traceback (most recent call last)") }) else {
            return nil
        }
        var out: [String] = []
        for line in lines[start...] {
            out.append(line)
            if out.count > 1, !line.hasPrefix(" "), !line.hasPrefix("\t"), !line.hasPrefix("Traceback") {
                break  // the exception line
            }
        }
        return out
    }

    /// `File ".../worker.py", line 12, in run` -> `worker.py:run` (the engine's frame format).
    static func pythonFrames(_ traceback: [String]) -> [String] {
        var out: [String] = []
        for line in traceback {
            let t = line.trimmingCharacters(in: .whitespaces)
            guard t.hasPrefix("File \""), let q = t.dropFirst(6).firstIndex(of: "\""),
                let inR = t.range(of: ", in ")
            else { continue }
            let file = (String(t[t.index(t.startIndex, offsetBy: 6)..<q]) as NSString).lastPathComponent
            out.append("\(file):\(t[inR.upperBound...])")
        }
        return Array(out.suffix(Fingerprint.frameCount))
    }

    public static func draft(
        code: Int32, signaled: Bool, logTail: [String], version: String, platform: String
    ) -> IncidentDraft {
        let tail = Array(logTail.suffix(logTailLines))
        let how = signaled ? "signal \(code), \(signalName(code))" : "exit code \(code)"
        let exceptionType = signaled ? signalName(code) : "exit \(code)"
        let tb = lastTraceback(tail)
        var message = "The engine stopped unexpectedly (\(how))."
        if let last = tb?.last, tb!.count > 1 { message += " Last error: \(last)" }
        return IncidentDraft(
            kind: IncidentKind.engineExit, title: "Engine exited (\(signaled ? signalName(code) : "exit code \(code)"))",
            message: message, exceptionType: exceptionType, traceback: tb?.joined(separator: "\n") ?? "",
            context: [
                "exit_code": signaled ? .null : .number(Double(code)),
                "signal": signaled ? .string(signalName(code)) : .null,
                "log_tail": .array(tail.map { .string($0) }),
            ],
            frames: tb.map(pythonFrames) ?? [], version: version, platform: platform)
    }
}

/// The verification-only crash hook (`AGENTLINE_TEST_CRASH=exception|trap`). It fires only when
/// the app runs on a throwaway data folder (`AGENTLINE_APP_SUPPORT_OVERRIDE`): a test crash in the
/// real folder would leave fake crash reports among the user's own (it did once, in phase 13).
public enum TestCrash {
    public static func mode(environment env: [String: String]) -> String? {
        guard let mode = env["AGENTLINE_TEST_CRASH"], ["exception", "trap"].contains(mode),
            let override = env["AGENTLINE_APP_SUPPORT_OVERRIDE"], !override.isEmpty
        else { return nil }
        return mode
    }
}
