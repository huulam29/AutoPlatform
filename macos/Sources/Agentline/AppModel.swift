import AgentlineCore
import AppKit
import Foundation
import Observation

/// Everything the windows, the menu bar extra and notifications share.
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    let backend: BackendManager
    let keychain = KeychainStore()
    let notifier = Notifier()

    // navigation: a place in Agentline Studio (the web UI's hash route)
    struct Route: Equatable {
        var path: String
        /// Bumped on every request, so asking for the same place again still goes there.
        var nonce = 0
    }
    var route = Route(path: "#/pipelines")
    /// A reject asked for from a notification, waiting for the user's confirmation.
    var pendingReject: Approval?

    // data
    private(set) var runs: [RunSummary] = []
    private(set) var approvals: [Approval] = []
    private(set) var status: EngineStatus?
    private(set) var pipelines: [PipelineSummary] = []
    private(set) var pipelinesDirOnServer: String?
    private(set) var pollError: String?
    /// Set when the engine has no `GET /api/approvals` yet: gates are found through the runs.
    private(set) var approvalsFallback = false
    var restartNeeded = false
    var banner: String?
    /// The incident an API 500 was recorded as (the banner links to it).
    var bannerIncident: String?

    // problems (crash and failure reports; local files only, never sent anywhere)
    // (internal, not private: AppModel+Problems.swift sets them)
    var incidents: [Incident] = []
    /// One issue per problem (engine-made; read from the files when the engine is down).
    var issues: [Issue] = []
    var issueFilter = IssueFilter.active
    /// Set at launch when the app died last time: "agentline quit unexpectedly last time".
    var crashBanner: Incident?
    var problemsMessage: String?
    var newIncidentCount: Int { incidents.filter(\.isNew).count }
    var notifyLimiter = IncidentNotifyLimiter(interval: 600)
    /// Keychain values read here when the engine couldn't start (so redaction still knows them).
    var extraSecrets: [String] = []
    var lastEngineVersion: String?
    private var pollCount = 0

    private var planner = NotificationPlanner()
    private var pollTask: Task<Void, Never>?
    private var tick = 0
    private var launched = false

    static var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
    }

    var markerURL: URL { backend.paths.home.appending(path: "running.marker") }
    var scanStateURL: URL { backend.paths.home.appending(path: "crash-scan.json") }
    var reportRedactor: Redactor { backend.redactor.adding(extraSecrets) }
    var incidentStore: IncidentStore { IncidentStore(paths: backend.paths, redactor: reportRedactor) }

    static let pipelinesDirKey = "pipelinesDir"

    private init() {
        var dir: URL?
        if let saved = UserDefaults.standard.string(forKey: Self.pipelinesDirKey), !saved.isEmpty {
            dir = URL(fileURLWithPath: saved, isDirectory: true)
        }
        // Keys live in <home>/secrets.env, which the engine loads itself (decision 60): nothing to
        // pass, and no Keychain read, so no macOS prompt after a rebuild.
        backend = BackendManager(pipelinesDir: dir, keyLoader: { [:] })
        notifier.model = self
    }

    var client: APIClient? { backend.isRunning ? backend.client : nil }

    var runningCount: Int { runs.filter { $0.status == "running" || $0.status == "pending" }.count }
    var needsReviewCount: Int { runs.filter { $0.status == "needs-review" }.count }
    var waitingCount: Int { approvals.count }

    // MARK: lifecycle

    func launch() {
        guard !launched else { return }
        launched = true
        notifier.setUp()

        // Unclean-exit detection: the marker of the previous run, then ours.
        let previous = RunMarker.read(markerURL)
        let mine = RunMarker(pid: getpid(), startedAt: Date().timeIntervalSince1970, version: Self.appVersion)
        try? mine.write(markerURL)
        CrashCapture.install(
            .init(
                store: incidentStore, markerURL: markerURL, marker: mine, version: Self.appVersion,
                platform: SystemInfo.platformString))
        backend.onUnexpectedExit = { [weak self] exit in self?.engineExited(exit) }

        Task {
            await migrateKeychainKeys()
            await backend.start()
            CrashCapture.updateRedactor(reportRedactor)
            await launchCrashCheck(previous: previous)
        }
        CrashCapture.scheduleTestCrashIfRequested()
        if Snapshot.dir != nil { Task { await Snapshot.run(self) } }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.poll()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    static let keysMigratedKey = "keysMovedToSecretsFile"

    /// Once per data folder: copy keys saved in the Keychain by earlier versions into the secrets
    /// file. This is the last Keychain read (macOS may ask one more time); the flag is set first so
    /// a denied prompt is never asked again.
    func migrateKeychainKeys() async {
        let flag = "\(Self.keysMigratedKey):\(backend.paths.home.path)"
        guard !UserDefaults.standard.bool(forKey: flag) else { return }
        UserDefaults.standard.set(true, forKey: flag)
        let store = keychain
        let file = SecretsFile(home: backend.paths.home)
        let result: Result<[String], Error> = await Task.detached {
            Result { try KeyMigration.merge(try store.all(), into: file) }
        }.value
        switch result {
        case .success(let names):
            if !names.isEmpty {
                backend.appendLog("[app] moved \(names.count) key(s) from the Keychain to \(file.url.path)")
            }
        case .failure(let error):
            backend.appendLog("[app] couldn't read the old Keychain keys (\(error.localizedDescription)); set them again under Settings > Keys")
        }
    }

    func restartEngine() {
        restartNeeded = false
        Task {
            await backend.restart()
            await poll(force: true)
        }
    }

    /// Restart the engine and wait (up to a minute) until it answers again.
    func restartEngineAndWait() async {
        restartNeeded = false
        await backend.restart()
        for _ in 0..<120 where client == nil { try? await Task.sleep(for: .milliseconds(500)) }
        await poll(force: true)
    }

    func shutdownNow() {
        pollTask?.cancel()
        backend.stopNow()
        RunMarker.remove(markerURL)  // a clean quit
    }

    func setPipelinesDir(_ url: URL) {
        UserDefaults.standard.set(url.path, forKey: Self.pipelinesDirKey)
        backend.pipelinesDir = url
        restartNeeded = true
    }

    // MARK: polling

    func refresh() { Task { await poll(force: true) } }

    func poll(force: Bool = false) async {
        pollCount += 1
        if force || pollCount % 5 == 1 {
            await reloadIncidents()
        }
        guard let client else {
            updateBadge()
            return
        }
        tick += 1
        do {
            let fetched = try await client.runs(limit: 200)
            runs = fetched
            pollError = nil
            let gates = await fetchApprovals(client)
            if let gates { approvals = gates }
            for event in planner.events(approvals: gates, runs: fetched) {
                notifier.post(event)
            }
        } catch {
            pollError = error.localizedDescription
            if case APIError.unreachable = error { backend.noteHealth(ok: false) }
        }
        if force || tick % 5 == 1 {
            status = try? await client.status()
            if let v = status?.version { lastEngineVersion = v }
            if let r = status?.restartNeeded, !r.isEmpty { restartNeeded = true }
            if let p = try? await client.pipelines() {
                pipelines = p.pipelines
                pipelinesDirOnServer = p.dir
            }
        }
        updateBadge()
    }

    private func fetchApprovals(_ client: APIClient) async -> [Approval]? {
        if !approvalsFallback {
            do {
                return try await client.approvals()
            } catch APIError.http(let s, _) where s == 404 || s == 405 {
                approvalsFallback = true
                backend.appendLog("[app] GET /api/approvals isn't available; finding gates through runs")
            } catch {
                return nil
            }
        }
        // Fallback: look inside waiting runs.
        var out: [Approval] = []
        for run in runs where run.status == "waiting" {
            guard let detail = try? await client.run(run.id) else { continue }
            for g in detail.waitingGates {
                out.append(
                    Approval(
                        runId: run.id, pipeline: run.flow, nodeId: g.nodeId, executionId: g.id,
                        prompt: g.gatePrompt, approvers: g.approvers, waitingSince: g.startedAt ?? g.createdAt,
                        payload: run.payload))
            }
            if out.count > 50 { break }
        }
        return out
    }

    private func updateBadge() {
        let n = approvals.count
        NSApp?.dockTile.badgeLabel = n > 0 ? String(n) : nil
    }

    // MARK: actions

    /// Approve or reject a gate; returns an error message for inline display.
    func decide(_ approve: Bool, runId: String, node: String?, comment: String?) async -> String? {
        guard let client else { return "The engine isn't running." }
        do {
            if approve {
                _ = try await client.approve(runId: runId, node: node, comment: comment)
            } else {
                _ = try await client.reject(runId: runId, node: node, comment: comment)
            }
            await poll(force: true)
            return nil
        } catch APIError.http(let s, let detail) where s == 403 {
            return "You're not an approver for this gate (\(detail))."
        } catch APIError.http(let s, let detail) where s == 409 {
            return "No single gate is waiting (\(detail)). Refresh and try again."
        } catch {
            if let id = (error as? APIError)?.incidentId { bannerIncident = id }
            return error.localizedDescription
        }
    }

    /// Go to a place in Agentline Studio (`#/runs/<id>`, `#/settings`, …) and bring it forward.
    func show(_ path: String) {
        route = Route(path: path, nonce: route.nonce + 1)
        activateMainWindow()
    }

    func showRun(_ id: String) { show("#/runs/\(id)") }

    /// Bring the main window forward (or open it again after it was closed).
    func activateMainWindow() {
        NSApp.activate()
        if let w = NSApp.windows.first(where: { $0.identifier?.rawValue.hasPrefix("main") == true }) {
            w.makeKeyAndOrderFront(nil)
        } else {
            openMainWindow?()
        }
    }

    /// Set by the views: SwiftUI's `openWindow` for the main window.
    var openMainWindow: (() -> Void)?}
