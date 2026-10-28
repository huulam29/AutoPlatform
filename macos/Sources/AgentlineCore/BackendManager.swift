import Darwin
import Foundation
import Observation

/// Runs `agentline serve` as a child process on a free localhost port and watches it.
@MainActor
@Observable
public final class BackendManager {
    public enum State: Equatable, Sendable {
        case stopped
        case starting
        case running
        case crashed(String)
        case failed(String)  // couldn't even start (missing bundle, bad folder)

        public var label: String {
            switch self {
            case .stopped: return "Stopped"
            case .starting: return "Starting"
            case .running: return "Running"
            case .crashed: return "Crashed"
            case .failed: return "Can't start"
            }
        }

        public var problem: String? {
            switch self {
            case .crashed(let m), .failed(let m): return m
            default: return nil
            }
        }
    }

    public private(set) var state: State = .stopped
    public private(set) var log = LogBuffer(capacity: 5000)  // the Logs page shows up to 5000
    public private(set) var port: Int?
    public private(set) var token: String?
    public private(set) var location: BackendLocation?
    /// Bumped on every start, so views holding a client or web view can rebuild.
    public private(set) var generation = 0

    /// Redacts every Keychain value and the API token (refreshed at each start), plus the
    /// token-looking patterns. Used for anything written into a report.
    public private(set) var redactor = Redactor()
    /// True once a start has read the Keychain (so `redactor` knows the key values).
    public private(set) var keysLoaded = false

    /// How an engine that nobody asked to stop ended.
    public struct UnexpectedExit: Sendable, Equatable {
        public let code: Int32
        public let signaled: Bool
        public let pid: Int32
    }

    /// Called on the main actor when the engine exits without a stop/restart from the app.
    public var onUnexpectedExit: (@MainActor (UnexpectedExit) -> Void)?

    public let paths: AppPaths
    public var pipelinesDir: URL

    private let environment: [String: String]
    private let resources: URL?
    private let keyLoader: @Sendable () throws -> [String: String]
    private var process: Process?
    private var stopping = false
    /// Processes the app itself is stopping: their exit is expected.
    private var expectedExits: [Process] = []
    private var healthTask: Task<Void, Never>?

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        resources: URL? = Bundle.main.resourceURL,
        pipelinesDir: URL? = nil,
        keyLoader: @escaping @Sendable () throws -> [String: String]
    ) {
        self.environment = environment
        self.resources = resources
        self.keyLoader = keyLoader
        paths = AppPaths(environment: environment)
        self.pipelinesDir = pipelinesDir ?? paths.defaultPipelinesDir
    }

    public var baseURL: URL? { port.map { URL(string: "http://127.0.0.1:\($0)")! } }

    /// A client for the current engine (nil until a start has picked a port and token).
    public var client: APIClient? {
        guard let baseURL, let token else { return nil }
        return APIClient(baseURL: baseURL, token: token)
    }

    public var isRunning: Bool { state == .running }

    // MARK: lifecycle

    public func start() async {
        guard process == nil, state != .starting else { return }
        state = .starting
        stopping = false
        generation += 1
        let gen = generation
        do {
            let location = try BackendLocation.resolve(
                environment: environment, resources: resources,
                fileExists: { FileManager.default.isExecutableFile(atPath: $0) })
            self.location = location
            try prepareFolders()
            killStaleEngine(location: location)
            let keys = try await Task.detached(priority: .userInitiated) { [keyLoader] in try keyLoader() }.value
            let token = Tokens.random()
            let fileKeys = SecretsFile(home: paths.home).read().values  // fallback store
            redactor = Redactor(secrets: Array(keys.values) + Array(fileKeys) + [token])
            keysLoaded = true
            let port = try PortFinder.freePort()
            let env = LaunchSpec.environment(
                base: environment, paths: paths, location: location, token: token,
                user: macUserName(), keys: keys)
            try await seedIfEmpty(location: location, environment: env)
            guard gen == generation, !stopping else { return }
            let spec = LaunchSpec.serve(location: location, pipelinesDir: pipelinesDir, port: port, environment: env)
            self.token = token
            self.port = port
            appendLog("[app] starting engine on 127.0.0.1:\(port) (\(location.isDev ? "dev" : "bundled") Python)")
            try spawn(spec)
            healthTask = Task { [weak self] in await self?.waitHealthy(gen: gen) }
        } catch {
            appendLog("[app] \(error.localizedDescription)")
            state = .failed(error.localizedDescription)
        }
    }

    /// SIGTERM, then SIGKILL after `grace` seconds.
    public func stop(grace: TimeInterval = 5) async {
        healthTask?.cancel()
        healthTask = nil
        guard let p = process else {
            if state == .starting { stopping = true }
            if case .running = state { state = .stopped }
            return
        }
        stopping = true
        expectedExits.append(p)
        appendLog("[app] stopping engine")
        if p.isRunning { kill(p.processIdentifier, SIGTERM) }
        let deadline = Date().addingTimeInterval(grace)
        while p.isRunning, Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if p.isRunning {
            appendLog("[app] engine didn't stop in \(Int(grace)) s; killing it")
            kill(p.processIdentifier, SIGKILL)
            p.waitUntilExit()
        }
        process = nil
        removePidFile()
        state = .stopped
    }

    /// The same as `stop`, but blocking: used while the app quits, where the main actor can't
    /// run async work (`applicationShouldTerminate` may be called from inside a main-actor job).
    public func stopNow(grace: TimeInterval = 5) {
        healthTask?.cancel()
        healthTask = nil
        guard let p = process else { return }
        stopping = true
        expectedExits.append(p)
        if p.isRunning { kill(p.processIdentifier, SIGTERM) }
        let deadline = Date().addingTimeInterval(grace)
        while p.isRunning, Date() < deadline { usleep(50_000) }
        if p.isRunning {
            kill(p.processIdentifier, SIGKILL)
            p.waitUntilExit()
        }
        process = nil
        removePidFile()
        state = .stopped
    }

    public func restart() async {
        await stop()
        await start()
    }

    // MARK: internals

    private func prepareFolders() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: paths.home, withIntermediateDirectories: true)
        if !fm.fileExists(atPath: paths.config.path) {
            try AppPaths.configTemplate.write(to: paths.config, atomically: true, encoding: .utf8)
        }
        try fm.createDirectory(at: pipelinesDir, withIntermediateDirectories: true)
    }

    /// The first time a pipelines folder is used and it's empty, add the ticket-to-eod template.
    private func seedIfEmpty(location: BackendLocation, environment env: [String: String]) async throws {
        let fm = FileManager.default
        let marker = paths.home.appending(path: ".seeded")
        let seeded = (try? String(contentsOf: marker, encoding: .utf8))?.split(separator: "\n").map(String.init) ?? []
        guard !seeded.contains(pipelinesDir.path) else { return }
        let contents = (try? fm.contentsOfDirectory(atPath: pipelinesDir.path))?.filter { !$0.hasPrefix(".") } ?? []
        if contents.isEmpty {
            let spec = LaunchSpec.seed(location: location, pipelinesDir: pipelinesDir, environment: env)
            appendLog("[app] seeding \(pipelinesDir.path) with the ticket-to-eod template")
            let output = await Task.detached { Self.runToEnd(spec) }.value
            for line in output.split(separator: "\n") { appendLog(String(line)) }
        }
        try (seeded + [pipelinesDir.path]).joined(separator: "\n").write(to: marker, atomically: true, encoding: .utf8)
    }

    nonisolated private static func runToEnd(_ spec: LaunchSpec) -> String {
        let p = Process()
        p.executableURL = spec.executable
        p.arguments = spec.arguments
        p.environment = spec.environment
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do {
            try p.run()
        } catch {
            return "[app] seeding failed: \(error.localizedDescription)"
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }

    private func spawn(_ spec: LaunchSpec) throws {
        let p = Process()
        p.executableURL = spec.executable
        p.arguments = spec.arguments
        p.environment = spec.environment
        p.currentDirectoryURL = paths.home
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty else {
                h.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.appendChunk(text) }
        }
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            let reason = proc.terminationReason
            Task { @MainActor in self?.exited(proc, code: code, signaled: reason == .uncaughtSignal) }
        }
        try p.run()
        process = p
        writePidFile(p.processIdentifier)
    }

    private func exited(_ p: Process, code: Int32, signaled: Bool) {
        let how = signaled ? "signal \(code)" : "exit code \(code)"
        // A process the app stopped (stop/restart/quit) is never a crash, even when this
        // handler runs after a restart already started the next engine.
        if let i = expectedExits.firstIndex(where: { $0 === p }) {
            expectedExits.remove(at: i)
            appendLog("[app] engine exited (\(how))")
            return
        }
        guard p === process else { return }
        appendLog("[app] engine exited (\(how))")
        if stopping {
            return  // stop() sets the state
        }
        process = nil
        healthTask?.cancel()
        removePidFile()
        state = .crashed("The engine stopped unexpectedly (\(how)). See Logs and Problems.")
        onUnexpectedExit?(UnexpectedExit(code: code, signaled: signaled, pid: p.processIdentifier))
    }

    private func waitHealthy(gen: Int) async {
        guard let client else { return }
        let deadline = Date().addingTimeInterval(90)
        while !Task.isCancelled, gen == generation, process != nil, Date() < deadline {
            if (try? await client.health()) == true {
                if state == .starting {
                    state = .running
                    appendLog("[app] engine is up")
                }
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
        if !Task.isCancelled, gen == generation, state == .starting, process != nil {
            state = .crashed("The engine didn't answer /healthz within 90 s. See Logs.")
        }
    }

    /// Called periodically by the app: a running engine that stops answering is reported.
    public func noteHealth(ok: Bool) {
        if !ok, state == .running, process?.isRunning == false {
            state = .crashed("The engine stopped answering.")
        }
    }

    private func appendChunk(_ text: String) {
        log.append(text)
    }

    public func appendLog(_ line: String) {
        log.appendLine(line)
    }

    // A pid file lets the next launch stop an engine an earlier crash left behind.
    private var pidFile: URL { paths.home.appending(path: "engine.pid") }

    private func writePidFile(_ pid: Int32) {
        try? String(pid).write(to: pidFile, atomically: true, encoding: .utf8)
    }

    private func removePidFile() {
        try? FileManager.default.removeItem(at: pidFile)
    }

    private func killStaleEngine(location: BackendLocation) {
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
            let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1
        else { return }
        removePidFile()
        var buf = [CChar](repeating: 0, count: 4096)
        guard proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 else { return }
        let path = String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        // Only a Python that is ours: the same interpreter (after symlinks) or a "python" binary.
        let ours = location.python.resolvingSymlinksInPath().path
        guard path == ours || (path as NSString).lastPathComponent.hasPrefix("python") else { return }
        appendLog("[app] stopping an engine left over from an earlier launch (pid \(pid))")
        kill(pid, SIGTERM)
    }
}
