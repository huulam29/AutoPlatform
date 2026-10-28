import Foundation
import XCTest

@testable import AgentlineCore

private func scratchDir() throws -> URL {
    let dir = FileManager.default.temporaryDirectory.appending(path: "agentline-incidents-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

private func draft(_ message: String = "The engine stopped unexpectedly (signal 9, SIGKILL).") -> IncidentDraft {
    IncidentDraft(
        kind: IncidentKind.engineExit, title: "Engine exited (SIGKILL)", message: message, exceptionType: "SIGKILL",
        context: ["signal": .string("SIGKILL")], version: "0.1.0", platform: "macOS 27.0 arm64")
}

final class IncidentStoreTests: XCTestCase {
    func testRecordMergesByFingerprint() throws {
        let dir = try scratchDir()
        let store = IncidentStore(dir: dir)
        let t0 = Date(timeIntervalSince1970: 1_000)
        let first = try store.record(draft(), now: t0)
        XCTAssertEqual(first.count, 1)
        XCTAssertEqual(first.id.count, 16)
        XCTAssertEqual(first.id, String(first.fingerprint.prefix(16)))
        XCTAssertEqual(first.status, "new")
        try store.markSeen(first.id)
        XCTAssertEqual(store.get(first.id)?.status, "seen")

        // Same problem, different numbers in the message: same file, count 2, status new again.
        var again = draft("The engine stopped unexpectedly (signal 11, SIGKILL).")
        again.context = ["signal": .string("SIGKILL"), "attempt": .number(2)]
        let second = try store.record(again, now: Date(timeIntervalSince1970: 2_000))
        XCTAssertEqual(second.id, first.id)
        XCTAssertEqual(second.count, 2)
        XCTAssertEqual(second.firstSeen, 1_000)
        XCTAssertEqual(second.lastSeen, 2_000)
        XCTAssertEqual(second.status, "new")
        XCTAssertEqual(second.message, again.message)
        XCTAssertEqual(second.context["attempt"], .number(2))

        // A different problem gets its own file.
        var other = draft()
        other.exceptionType = "exit 1"
        let third = try store.record(other, now: Date(timeIntervalSince1970: 3_000))
        XCTAssertNotEqual(third.id, first.id)
        XCTAssertEqual(store.list().map(\.id), [third.id, first.id])  // newest first

        // The file is the shared format: snake_case keys, explicit null exception_type.
        let raw = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: store.file(for: first.id)))
        for key in ["id", "fingerprint", "source", "kind", "title", "message", "exception_type", "traceback", "context",
            "first_seen", "last_seen", "count", "version", "platform", "status", "attachments"]
        {
            XCTAssertNotNil(raw[key], key)
        }
        XCTAssertEqual(raw["source"], .string("app"))
        var noType = draft()
        noType.exceptionType = nil
        let n = try store.record(noType)
        let rawN = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: store.file(for: n.id)))
        XCTAssertEqual(rawN["exception_type"], .null)

        // No temp files left behind; the lock file is a dotfile.
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path)
        XCTAssertFalse(names.contains { $0.contains(".tmp") }, "\(names)")
    }

    func testAttachmentsDeleteAndClear() throws {
        let dir = try scratchDir()
        let store = IncidentStore(dir: dir, redactor: Redactor(secrets: ["hunter2-secret"]))
        let src = dir.appending(path: "report.ips")
        try "{\"x\":1}\npassword hunter2-secret here".write(to: src, atomically: true, encoding: .utf8)
        let inc = try store.record(draft(), attach: [src])
        XCTAssertEqual(inc.attachments, ["\(inc.id)/report.ips"])
        let copied = try String(contentsOf: XCTUnwrap(store.url(forAttachment: inc.attachments[0])), encoding: .utf8)
        XCTAssertFalse(copied.contains("hunter2-secret"))
        XCTAssertTrue(copied.contains(Redactor.mask))
        XCTAssertNil(store.url(forAttachment: "../../etc/passwd"))

        let other = try store.record(IncidentDraft(kind: IncidentKind.appUncleanExit, title: "x", message: "y", version: "1", platform: "p"))
        try store.delete(inc.id)
        XCTAssertNil(store.get(inc.id))
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.attachmentsDir(for: inc.id).path))
        XCTAssertEqual(store.list().map(\.id), [other.id])
        try "keep".write(to: dir.appending(path: "notes.txt"), atomically: true, encoding: .utf8)
        try store.clear()
        XCTAssertTrue(store.list().isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: dir.appending(path: "notes.txt").path))
        // Bad ids are refused.
        XCTAssertNil(store.get("../x"))
        XCTAssertFalse(IncidentStore.isValidId("ABCDEF0123456789"))
    }

    func testLenientDecodingOfEngineSummary() throws {
        let json = #"{"id":"0123456789abcdef","fingerprint":"0123456789abcdef99","source":"engine","kind":"api_error","title":"t","message":"m","exception_type":"KeyError","first_seen":1.5,"last_seen":2.5,"count":3,"version":"0.1","platform":"macOS 27.0 arm64","status":"new","attachments":[]}"#
        let i = try JSONDecoder().decode(Incident.self, from: Data(json.utf8))
        XCTAssertEqual(i.traceback, "")
        XCTAssertEqual(i.context, .object([:]))
        XCTAssertEqual(i.count, 3)
        let r = try JSONDecoder().decode(IncidentsResponse.self, from: Data(#"{"incidents":[\#(json)],"dir":"/x","new":1}"#.utf8))
        XCTAssertEqual(r.incidents.first?.exceptionType, "KeyError")
    }

    func testAPIErrorKeepsIncidentId() {
        let body = Data(#"{"detail": "internal error (recorded as an incident)", "incident": "0123456789abcdef"}"#.utf8)
        let e = APIError.http(status: 500, detail: APIClient.detail(from: body))
        XCTAssertEqual(e.incidentId, "0123456789abcdef")
        XCTAssertNil(APIError.http(status: 404, detail: "no run x").incidentId)
    }

    func testNotifyLimiter() {
        var l = IncidentNotifyLimiter(interval: 600)
        let t = Date(timeIntervalSince1970: 0)
        XCTAssertTrue(l.shouldNotify("a", now: t))
        XCTAssertFalse(l.shouldNotify("a", now: t.addingTimeInterval(599)))
        XCTAssertTrue(l.shouldNotify("b", now: t.addingTimeInterval(10)))
        XCTAssertTrue(l.shouldNotify("a", now: t.addingTimeInterval(601)))
    }
}

final class FingerprintTests: XCTestCase {
    /// Reference values computed with the engine's `agentline.incidents.fingerprint`.
    func testMatchesEngine() {
        XCTAssertEqual(
            Fingerprint.normalize("  Engine died at 0xdeadBEEF run r0123456789abcdef01 after 42 s\nsecond line"),
            "Engine died at <hex> run <id> after N s")
        XCTAssertEqual(
            Fingerprint.compute(
                source: "app", kind: "engine_exit", exceptionType: "SIGKILL",
                message: "The engine stopped unexpectedly (signal 9).", frames: []),
            "01880de9c45ec24b418cd3ffa28967b5665c31be4c3ed4e64a21349dacf33c44")
        XCTAssertEqual(
            Fingerprint.compute(
                source: "app", kind: "app_crash", exceptionType: "EXC_BREAKPOINT (SIGTRAP)",
                message: "Fatal error: boom 12", frames: ["AppModel.launch()", "main"]),
            "176ed7b088af61a58f37797359174ff74e81e07ab8083b3a791d3f6242be1fda")
    }

    func testStableAcrossAddressesAndOffsets() {
        let a = ExceptionReport.draft(
            name: "NSInvalidArgumentException", reason: "bad index 3 at 0x600001234",
            callStack: [
                "0   CoreFoundation    0x000000018a1b2c3d __exceptionPreprocess + 176",
                "1   libobjc.A.dylib   0x0000000189f0e1d2 objc_exception_throw + 60",
                "2   Agentline         0x0000000100a0b0c0 AppModel.poll() + 120",
                "3   Agentline         0x0000000100a0b1c0 AppModel.launch() + 44",
            ], version: "1", platform: "p")
        let b = ExceptionReport.draft(
            name: "NSInvalidArgumentException", reason: "bad index 7 at 0x600009999",
            callStack: [
                "0   CoreFoundation    0x00000001aaaaaaaa __exceptionPreprocess + 176",
                "1   libobjc.A.dylib   0x00000001bbbbbbbb objc_exception_throw + 60",
                "2   Agentline         0x0000000100cccccc AppModel.poll() + 124",
                "3   Agentline         0x0000000100dddddd AppModel.launch() + 48",
            ], version: "2", platform: "p")
        XCTAssertEqual(a.fingerprint, b.fingerprint)
        XCTAssertEqual(a.id.count, 16)
        XCTAssertEqual(
            Fingerprint.topFrames(a.frames), ["Agentline AppModel.poll()", "Agentline AppModel.launch()"])
        var c = b
        c.frames = ["2   Agentline   0x0000000100cccccc AppModel.refresh() + 12"]
        XCTAssertNotEqual(a.fingerprint, c.fingerprint)
    }
}

final class RedactorTests: XCTestCase {
    func testKeychainValuesAndPatterns() {
        let r = Redactor(secrets: ["my-jira-token-value", "abc"])  // "abc" is too short to redact
        let text = """
            JIRA_API_TOKEN=my-jira-token-value and abc stays
            Authorization: Bearer eyabc.def-123456789
            key sk-or-v1-0123456789abcdef0123 and sk-ant-api03-AAAAAAAAAAAAAAAA
            gh ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 slack xoxb-1234-5678-abcdefgh
            url https://x.test/?token=9f8e7d6c5b4a39281706&page=2 password: "s3cr3tP4ssw0rdValue"
            "api_key": "QWERTYUIOPASDF1234567890" api_key_env = "OPENROUTER_API_KEY"
            """
        let out = r.redact(text)
        for leaked in ["my-jira-token-value", "eyabc.def-123456789", "sk-or-v1-0123456789abcdef0123",
            "sk-ant-api03", "ghp_ABCDEF", "xoxb-1234", "9f8e7d6c5b4a39281706", "s3cr3tP4ssw0rdValue",
            "QWERTYUIOPASDF1234567890"]
        {
            XCTAssertFalse(out.contains(leaked), "\(leaked) leaked in:\n\(out)")
        }
        XCTAssertTrue(out.contains("abc stays"))
        XCTAssertTrue(out.contains("Bearer [REDACTED]"))
        XCTAssertTrue(out.contains("token=[REDACTED]&page=2"), out)
        XCTAssertTrue(out.contains(#""OPENROUTER_API_KEY""#), "env var names survive: \(out)")
        XCTAssertEqual(r.redact(out), out, "idempotent")
    }

    func testRedactsJSONAndDrafts() {
        let r = Redactor(secrets: ["supersecretvalue"])
        let v: JSONValue = .object(["log_tail": .array([.string("x supersecretvalue y")]), "n": .number(1)])
        XCTAssertEqual(r.redact(v), .object(["log_tail": .array([.string("x [REDACTED] y")]), "n": .number(1)]))
        let d = IncidentDraft(
            kind: "engine_exit", title: "supersecretvalue", message: "m supersecretvalue", traceback: "supersecretvalue",
            context: ["k": .string("supersecretvalue")], version: "1", platform: "p"
        ).redacted(r)
        XCTAssertEqual([d.title, d.message, d.traceback], ["[REDACTED]", "m [REDACTED]", "[REDACTED]"])
        XCTAssertEqual(d.context["k"], .string("[REDACTED]"))
        // The fingerprint never depends on the secret.
        let d2 = IncidentDraft(kind: "engine_exit", title: "t", message: "m othersecretvalue1", version: "1", platform: "p")
            .redacted(r.adding(["othersecretvalue1"]))
        XCTAssertEqual(d2.message, "m [REDACTED]")
        XCTAssertEqual(d.fingerprint, d2.fingerprint)
    }
}

final class CrashReportTests: XCTestCase {
    func testParsesIPS() throws {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "Agentline-2026-10-19-101500", withExtension: "ips", subdirectory: "Fixtures"))
        let text = try String(contentsOf: url, encoding: .utf8)
        let s = CrashReportParser.parse(text, fileName: url.lastPathComponent)
        XCTAssertTrue(s.parsed)
        XCTAssertEqual(s.exceptionType, "EXC_BREAKPOINT (SIGTRAP)")
        XCTAssertEqual(s.signal, "SIGTRAP")
        XCTAssertEqual(s.pid, 48213)
        XCTAssertEqual(s.appVersion, "0.1.0")
        XCTAssertEqual(s.appFrame, "closure #1 in static CrashCapture.scheduleTestCrash()")
        XCTAssertEqual(s.frames.first, "_assertionFailure(_:_:file:line:flags:) (libswiftCore.dylib)")
        XCTAssertEqual(s.frames.count, 6)
        XCTAssertEqual(s.title, "Crash: EXC_BREAKPOINT (SIGTRAP) in closure #1 in static CrashCapture.scheduleTestCrash()")
        XCTAssertTrue(s.message?.contains("Fatal error: AGENTLINE_TEST_CRASH=trap") == true)
        XCTAssertEqual(s.timestamp?.timeIntervalSince1970 ?? 0, 1_792_422_900, accuracy: 1)

        let d = s.draft(version: "dev", platform: "macOS 27.0 arm64")
        XCTAssertEqual(d.kind, "app_crash")
        XCTAssertEqual(d.version, "0.1.0")
        XCTAssertEqual(Fingerprint.topFrames(d.frames).first, "closure #1 in static CrashCapture.scheduleTestCrash() (Agentline)")
        // Stored through the store, the key in the message is gone, and the copy is redacted too.
        let dir = try scratchDir()
        let inc = try IncidentStore(dir: dir).record(d, attach: [url])
        XCTAssertFalse(inc.message.contains("SECRETSECRET"))
        let copy = try String(contentsOf: XCTUnwrap(IncidentStore(dir: dir).url(forAttachment: inc.attachments[0])), encoding: .utf8)
        XCTAssertFalse(copy.contains("SECRETSECRET"))
        XCTAssertTrue(copy.contains("EXC_BREAKPOINT"))
    }

    func testGenericTitleForUnparseableReport() {
        let s = CrashReportParser.parse("not json at all", fileName: "Agentline-2026-10-19-000000.ips")
        XCTAssertFalse(s.parsed)
        XCTAssertEqual(s.title, "agentline crashed")
        XCTAssertEqual(s.draft(version: "1", platform: "p").kind, "app_crash")
    }

    func testParsesLegacyCrash() {
        let text = """
            Process:               Agentline [777]
            Exception Type:        EXC_BAD_ACCESS (SIGSEGV)
            Termination Reason:    Namespace SIGNAL, Code 11

            Thread 0 Crashed:: Dispatch queue: com.apple.main-thread
            0   libswiftCore.dylib            0x000000019a1b2c3d swift_retain + 8
            1   Agentline                     0x0000000100a0b0c0 AppModel.poll() + 120
            2   Agentline                     0x0000000100a0b1c0 main + 44

            Thread 1:
            """
        let s = CrashReportParser.parse(text, fileName: "Agentline_2026-10-19-1.crash")
        XCTAssertEqual(s.pid, 777)
        XCTAssertEqual(s.exceptionType, "EXC_BAD_ACCESS (SIGSEGV)")
        XCTAssertEqual(s.signal, "SIGSEGV")
        XCTAssertEqual(s.appFrame, "AppModel.poll()")
        XCTAssertEqual(s.frames.count, 3)
    }

    func testScannerSelection() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        func f(_ name: String, _ ago: TimeInterval) -> DiagnosticFile {
            DiagnosticFile(url: URL(fileURLWithPath: "/r/\(name)"), modified: now.addingTimeInterval(-ago))
        }
        XCTAssertTrue(CrashScanner.isAgentlineReport("Agentline-2026-10-19-101500.ips"))
        XCTAssertTrue(CrashScanner.isAgentlineReport("Agentline_2026-10-19-1.crash"))
        XCTAssertFalse(CrashScanner.isAgentlineReport("AgentlineHelper-2026.ips"))
        XCTAssertFalse(CrashScanner.isAgentlineReport("Agentline-2026.diag"))
        XCTAssertFalse(CrashScanner.isAgentlineReport("Safari-2026.ips"))
        let files = [f("Agentline-new.ips", 10), f("Agentline-old.ips", 30 * 86400), f("Other-1.ips", 5), f("Agentline-mid.ips", 3600)]
        // First scan: the last 7 days.
        XCTAssertEqual(CrashScanner.select(files, state: CrashScanState(), now: now).map(\.name), ["Agentline-mid.ips", "Agentline-new.ips"])
        // Later: only newer than the last scan (minus slack), never one already processed.
        var st = CrashScanState()
        st.note([f("Agentline-mid.ips", 3600)], at: now.addingTimeInterval(-60))
        XCTAssertEqual(CrashScanner.select(files, state: st, now: now).map(\.name), ["Agentline-new.ips"])
        st.note([f("Agentline-new.ips", 10)], at: now)
        XCTAssertTrue(CrashScanner.select(files, state: st, now: now).isEmpty)
    }
}

final class LaunchCheckTests: XCTestCase {
    let marker = RunMarker(pid: 4242, startedAt: 1_000, version: "0.1.0")

    func testVerdicts() {
        XCTAssertEqual(LaunchCheck.evaluate(previous: nil, reports: [], previousStillRunning: false), .clean)
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [], previousStillRunning: false), .uncleanExit(marker))
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [], previousStillRunning: true), .otherInstance)

        var mine = CrashReportSummary(fileName: "a.ips")
        mine.pid = 4242
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [mine], previousStillRunning: false), .explained)
        var otherPid = CrashReportSummary(fileName: "b.ips")
        otherPid.pid = 99
        otherPid.timestamp = Date(timeIntervalSince1970: 2_000)  // pid wins over time when both are known
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [otherPid], previousStillRunning: false), .uncleanExit(marker))
        var timed = CrashReportSummary(fileName: "c.crash")
        timed.timestamp = Date(timeIntervalSince1970: 1_500)
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [timed], previousStillRunning: false), .explained)
        timed.timestamp = Date(timeIntervalSince1970: 500)  // before this run started
        XCTAssertEqual(LaunchCheck.evaluate(previous: marker, reports: [timed], previousStillRunning: false), .uncleanExit(marker))

        var excepted = marker
        excepted.exceptionIncident = "0123456789abcdef"
        XCTAssertEqual(LaunchCheck.evaluate(previous: excepted, reports: [], previousStillRunning: false), .explained)
    }

    func testMarkerFileRoundTripAndDraft() throws {
        let url = try scratchDir().appending(path: "running.marker")
        XCTAssertNil(RunMarker.read(url))
        try marker.write(url)
        XCTAssertEqual(RunMarker.read(url), marker)
        try "garbage".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertNotNil(RunMarker.read(url), "an unreadable marker still means an unclean exit")
        RunMarker.remove(url)
        XCTAssertNil(RunMarker.read(url))

        let d = LaunchCheck.uncleanExitDraft(marker, version: "0.1.0", platform: "p")
        XCTAssertEqual(d.kind, "app_unclean_exit")
        XCTAssertEqual(d.title, "agentline quit unexpectedly")
        // Every unclean exit is the same problem: it counts up in one file.
        XCTAssertEqual(d.fingerprint, LaunchCheck.uncleanExitDraft(RunMarker(pid: 1, startedAt: 5, version: "x"), version: "y", platform: "p").fingerprint)
        XCTAssertFalse(LaunchCheck.isAgentlineRunning(pid: getpid()))
    }
}

final class EngineExitTests: XCTestCase {
    func testDraftWithTraceback() {
        let lines = (0..<300).map { "line \($0)" } + [
            "Traceback (most recent call last):",
            "  File \"/x/agentline/worker.py\", line 88, in run",
            "    await self.step()",
            "  File \"/x/agentline/scheduler.py\", line 12, in complete",
            "    raise ValueError(\"bad\")",
            "ValueError: bad epoch 7",
            "after",
        ]
        let d = EngineExitReport.draft(code: 1, signaled: false, logTail: lines, version: "0.1", platform: "p")
        XCTAssertEqual(d.kind, "engine_exit")
        XCTAssertEqual(d.exceptionType, "exit 1")
        XCTAssertEqual(d.context["exit_code"], .number(1))
        XCTAssertEqual(d.context["signal"], .null)
        XCTAssertEqual(d.context["log_tail"]?.arrayValue?.count, 200)
        XCTAssertTrue(d.traceback.hasSuffix("ValueError: bad epoch 7"))
        XCTAssertEqual(d.frames, ["worker.py:run", "scheduler.py:complete"])
        XCTAssertTrue(d.message.contains("Last error: ValueError: bad epoch 7"))

        let k = EngineExitReport.draft(code: 9, signaled: true, logTail: ["x"], version: "0.1", platform: "p")
        XCTAssertEqual(k.title, "Engine exited (SIGKILL)")
        XCTAssertEqual(k.context["signal"], .string("SIGKILL"))
        XCTAssertEqual(k.context["exit_code"], .null)
        // A crash loop is one incident.
        let k2 = EngineExitReport.draft(code: 9, signaled: true, logTail: ["y", "z"], version: "0.1", platform: "p")
        XCTAssertEqual(k.fingerprint, k2.fingerprint)
    }
}

final class ReportExportTests: XCTestCase {
    let system = SystemInfo(
        appVersion: "0.1.0", engineVersion: "0.1.0", osVersion: "macOS Version 27.0 (Build 26A428)",
        platform: "macOS 27.0 arm64", chip: "Apple M1 Pro", model: "MacBookPro18,3", memoryGB: 16, uptime: 90061)

    func incident(_ id: String, attachments: [String] = []) -> Incident {
        Incident(
            id: id, fingerprint: id + "00", source: "app", kind: "app_crash", title: "Crash | here", message: "m",
            exceptionType: "EXC_BAD_ACCESS (SIGSEGV)", traceback: "0 a\n1 b", context: .object([:]), firstSeen: 0,
            lastSeen: 60, count: 2, version: "0.1.0", platform: "macOS 27.0 arm64", status: "new",
            attachments: attachments)
    }

    func testEntries() {
        let a = incident("0123456789abcdef", attachments: ["0123456789abcdef/Agentline-1.ips", "../escape"])
        let b = incident("fedcba9876543210")
        let log = (0..<600).map { "log \($0)" } + ["Bearer abcdefghijkl1234"]
        let entries = ReportExport.entries(incidents: [a, b], system: system, logLines: log, redactor: Redactor())
        XCTAssertEqual(entries.map(\.destination), [
            "incidents/0123456789abcdef.json", "incidents/0123456789abcdef/Agentline-1.ips",
            "incidents/fedcba9876543210.json", "system.txt", "engine.log", "summary.md",
        ])
        XCTAssertEqual(entries[1], .copy(from: "0123456789abcdef/Agentline-1.ips", to: "incidents/0123456789abcdef/Agentline-1.ips"))
        guard case .text(_, let logText) = entries[4], case .text(_, let md) = entries[5], case .text(_, let sys) = entries[3]
        else { return XCTFail("text entries") }
        XCTAssertEqual(logText.split(separator: "\n").count, 500)
        XCTAssertFalse(logText.contains("abcdefghijkl1234"))
        XCTAssertTrue(md.contains("| app | app_crash | 2 | Crash \\| here |"), md)
        XCTAssertTrue(md.contains("`EXC_BAD_ACCESS (SIGSEGV)`"))
        XCTAssertTrue(sys.contains("Chip: Apple M1 Pro"))
        XCTAssertTrue(sys.contains("Uptime: 1d 1h 1m"))
        XCTAssertTrue(ReportExport.summaryMarkdown(incidents: [], system: system).contains("No crashes or errors"))
        XCTAssertEqual(
            ReportExport.baseName(date: Date(timeIntervalSince1970: 0), timeZone: TimeZone(identifier: "UTC")!),
            "agentline-report-1970-01-01-0000")
    }

    func testWriteAndZip() throws {
        let dir = try scratchDir()
        let store = IncidentStore(dir: dir.appending(path: "incidents"))
        let src = dir.appending(path: "x.ips")
        try "{}\n{}".write(to: src, atomically: true, encoding: .utf8)
        let inc = try store.record(draft(), attach: [src])
        let entries = ReportExport.entries(incidents: [inc], system: system, logLines: ["a"], redactor: Redactor())
        let folder = dir.appending(path: "out/agentline-report-test")
        try ReportExport.write(entries, incidentsDir: store.dir, into: folder)
        for e in entries {
            XCTAssertTrue(FileManager.default.fileExists(atPath: folder.appending(path: e.destination).path), e.destination)
        }
        let zip = dir.appending(path: "r.zip")
        try ReportExport.zip(folder: folder, to: zip)
        XCTAssertGreaterThan((try FileManager.default.attributesOfItem(atPath: zip.path)[.size] as? Int) ?? 0, 100)
    }

    func testTestCrashFiresOnlyOnAThrowawayDataFolder() {
        XCTAssertNil(TestCrash.mode(environment: ["AGENTLINE_TEST_CRASH": "trap"]))  // the real folder
        XCTAssertNil(TestCrash.mode(environment: ["AGENTLINE_TEST_CRASH": "trap", "AGENTLINE_APP_SUPPORT_OVERRIDE": ""]))
        XCTAssertNil(TestCrash.mode(environment: ["AGENTLINE_TEST_CRASH": "boom", "AGENTLINE_APP_SUPPORT_OVERRIDE": "/tmp/x"]))
        XCTAssertEqual(TestCrash.mode(environment: ["AGENTLINE_TEST_CRASH": "trap", "AGENTLINE_APP_SUPPORT_OVERRIDE": "/tmp/x"]), "trap")
        XCTAssertEqual(
            TestCrash.mode(environment: ["AGENTLINE_TEST_CRASH": "exception", "AGENTLINE_APP_SUPPORT_OVERRIDE": "/tmp/x"]), "exception")
    }
}

