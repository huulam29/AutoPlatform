import AgentlineCore
import AppKit
import Foundation
import UniformTypeIdentifiers

/// Crash and failure reports: launch-time crash check, engine exits, the Problems screen's
/// data and actions, export. Reports stay on this Mac; nothing here uploads anything.
extension AppModel {
    // MARK: launch check

    /// Look for crash reports from earlier runs, and decide whether the last run ended
    /// uncleanly. macOS can take a few seconds to write a crash report, so an unexplained
    /// marker is re-checked twice before it counts as an unclean exit.
    func launchCrashCheck(previous: RunMarker?) async {
        var reports: [CrashReportSummary] = []
        var appDeaths: [Incident] = []
        let stillRunning = previous.map { LaunchCheck.isAgentlineRunning(pid: $0.pid) } ?? false
        var verdict = LaunchCheck.Verdict.clean
        for delay in previous == nil ? [0.0] : [0.0, 3, 5] {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            let found = scanCrashReports(previous: previous)
            reports += found.map(\.0)
            appDeaths += found.map(\.1)
            verdict = LaunchCheck.evaluate(previous: previous, reports: reports, previousStillRunning: stillRunning)
            if case .uncleanExit = verdict { continue }
            break
        }
        switch verdict {
        case .uncleanExit(let m):
            if let inc = try? incidentStore.record(
                LaunchCheck.uncleanExitDraft(m, version: Self.appVersion, platform: SystemInfo.platformString))
            {
                backend.appendLog("[app] the last run didn't quit cleanly (incident \(inc.id))")
                appDeaths.append(inc)
            }
        case .explained:
            if let id = previous?.exceptionIncident, let inc = incidentStore.get(id),
                !appDeaths.contains(where: { $0.id == id })
            {
                appDeaths.append(inc)
            }
        case .clean, .otherInstance:
            break
        }
        await reloadIncidents()
        if let newest = appDeaths.max(by: { $0.lastSeen < $1.lastSeen }) {
            crashBanner = newest
            notifier.postIncident(newest, title: "agentline quit unexpectedly last time")
        }
        // A report that took longer still gets picked up.
        try? await Task.sleep(for: .seconds(30))
        if !scanCrashReports(previous: previous).isEmpty { await reloadIncidents() }
    }

    /// Record every new `Agentline*.ips` / `.crash`. A report of the run whose uncaught
    /// exception was already recorded is attached to that incident instead of a new one.
    @discardableResult
    func scanCrashReports(previous: RunMarker?) -> [(CrashReportSummary, Incident)] {
        var state = CrashScanState.load(scanStateURL)
        let now = Date()
        let files = CrashScanner.select(CrashScanner.list(), state: state, now: now)
        let store = incidentStore
        var out: [(CrashReportSummary, Incident)] = []
        for f in files {
            guard let data = try? Data(contentsOf: f.url) else { continue }
            let text = String(decoding: data, as: UTF8.self)
            let summary = CrashReportParser.parse(text, fileName: f.name)
            if let prev = previous, let excId = prev.exceptionIncident, summary.pid == prev.pid,
                let inc = try? store.attach(f.url, to: excId)
            {
                try? store.updateContext(excId, ["crash_report": .string(f.name)])
                out.append((summary, inc))
            } else if let inc = try? store.record(
                summary.draft(version: Self.appVersion, platform: SystemInfo.platformString), attach: [f.url])
            {
                out.append((summary, inc))
            }
            backend.appendLog("[app] recorded crash report \(f.name)")
        }
        state.note(files, at: now)
        state.save(scanStateURL)
        return out
    }

    /// "Scan for Crash Reports" in the Problems screen.
    func scanNow() {
        let found = scanCrashReports(previous: nil)
        problemsMessage = found.isEmpty ? "No new crash reports." : "Recorded \(found.count) crash report(s)."
        Task { await reloadIncidents() }
    }

    // MARK: engine exits

    func engineExited(_ exit: BackendManager.UnexpectedExit) {
        Task {
            try? await Task.sleep(for: .milliseconds(500))  // let the last output arrive
            var draft = EngineExitReport.draft(
                code: exit.code, signaled: exit.signaled, logTail: Array(backend.log.lines.suffix(500)),
                version: lastEngineVersion ?? "unknown", platform: SystemInfo.platformString)
            draft.context["pid"] = .number(Double(exit.pid))
            guard let inc = try? incidentStore.record(draft) else { return }
            backend.appendLog("[app] recorded the engine exit as incident \(inc.id) (seen \(inc.count)x)")
            await reloadIncidents()
            if notifyLimiter.shouldNotify(inc.id) {
                notifier.postIncident(inc, title: "The agentline engine stopped")
            }
        }
    }

    // MARK: data

    /// Through the engine when it's up, else straight from the directory (the same files).
    func reloadIncidents() async {
        if let client, let r = try? await client.incidents(limit: 500) {
            incidents = r.incidents
        } else {
            incidents = incidentStore.list()
        }
    }

    /// The engine syncs first, so new problems get their issue here.
    func reloadIssues() async {
        if let client, let r = try? await client.issues() {
            issues = r.issues
        } else {
            issues = IssueFiles(home: backend.paths.home).list()
        }
    }

    func issue(forProblem tag: String) -> Issue? {
        issues.first { $0.problemTag == tag }
    }

    /// Change an issue's status and/or add a note; returns an error message on failure.
    func updateIssue(_ number: Int, status: String? = nil, note: String? = nil) async -> String? {
        guard let client else { return "The engine isn't running; issues can be changed once it is." }
        do {
            let updated = try await client.updateIssue(number, status: status, note: note)
            if let k = issues.firstIndex(where: { $0.number == number }) { issues[k] = updated }
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    /// The full record (traceback, context): the engine's API, else the file.
    func incidentDetail(_ id: String) async -> Incident? {
        if let client, let i = try? await client.incident(id) { return i }
        return incidentStore.get(id)
    }

    func markSeen(_ id: String) async {
        do {
            if let client { try await client.markIncidentSeen(id) } else { try incidentStore.markSeen(id) }
        } catch {
            try? incidentStore.markSeen(id)
        }
        await reloadIncidents()
    }

    func deleteIncident(_ id: String) async {
        do {
            if let client { try await client.deleteIncident(id) } else { try incidentStore.delete(id) }
        } catch {
            try? incidentStore.delete(id)
        }
        if crashBanner?.id == id { crashBanner = nil }
        await reloadIncidents()
    }

    func clearIncidents() async {
        do {
            if let client { try await client.clearIncidents() } else { try incidentStore.clear() }
        } catch {
            try? incidentStore.clear()
        }
        crashBanner = nil
        await reloadIncidents()
    }

    func showProblem(_ id: String?) { show("#/problems") }

    // MARK: export

    func systemInfo() async -> SystemInfo {
        if let client, let s = try? await client.status(), let v = s.version { lastEngineVersion = v }
        return SystemInfo.current(appVersion: Self.appVersion, engineVersion: backend.isRunning ? lastEngineVersion : nil)
    }

    /// Full records from the directory (the API's list leaves out tracebacks).
    func fullIncidents(_ ids: [String]?) -> [Incident] {
        let store = incidentStore
        return (ids ?? incidents.map(\.id)).compactMap { store.get($0) }
    }

    /// "Export Report…": a zip of the chosen incidents (all when `ids` is nil) with attachments,
    /// system.txt, the redacted last 500 engine log lines and summary.md. Saved where the user
    /// picks; never sent anywhere.
    func exportReport(ids: [String]?) async {
        let base = ReportExport.baseName(date: Date())
        let panel = NSSavePanel()
        panel.title = "Export Problem Report"
        panel.nameFieldStringValue = "\(base).zip"
        panel.allowedContentTypes = [.zip]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let dest = panel.url else { return }
        let incidents = fullIncidents(ids)
        let entries = ReportExport.entries(
            incidents: incidents, system: await systemInfo(), logLines: Array(backend.log.lines.suffix(500)), redactor: reportRedactor)
        let incidentsDir = incidentStore.dir
        let result: String? = await Task.detached {
            let tmp = FileManager.default.temporaryDirectory.appending(path: "agentline-export-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: tmp) }
            do {
                let folder = tmp.appending(path: base)
                try ReportExport.write(entries, incidentsDir: incidentsDir, into: folder)
                try ReportExport.zip(folder: folder, to: dest)
                return nil
            } catch {
                return error.localizedDescription
            }
        }.value
        if let result {
            problemsMessage = "Export failed: \(result)"
        } else {
            problemsMessage = "Exported \(incidents.count) incident(s) to \(dest.lastPathComponent)."
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        }
    }

    /// "Copy Summary": markdown for an issue or a chat message.
    func copySummary(ids: [String]?) async {
        let md = ReportExport.summaryMarkdown(incidents: fullIncidents(ids), system: await systemInfo())
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(reportRedactor.redact(md), forType: .string)
        problemsMessage = "Summary copied."
    }
}
