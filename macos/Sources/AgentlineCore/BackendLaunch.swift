import Foundation

/// Where the app keeps its data. `AGENTLINE_APP_SUPPORT_OVERRIDE` moves all of it (tests and
/// verification runs use a scratch dir so the real data is never touched).
public struct AppPaths: Sendable, Equatable {
    public let home: URL
    public let overridden: Bool

    public init(environment: [String: String], fileManager: FileManager = .default) {
        if let o = environment["AGENTLINE_APP_SUPPORT_OVERRIDE"], !o.isEmpty {
            home = URL(fileURLWithPath: (o as NSString).expandingTildeInPath, isDirectory: true)
            overridden = true
        } else {
            let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? URL(fileURLWithPath: NSHomeDirectory()).appending(path: "Library/Application Support")
            home = base.appending(path: "agentline", directoryHint: .isDirectory)
            overridden = false
        }
    }

    public var db: URL { home.appending(path: "agentline.db") }
    public var config: URL { home.appending(path: "agentline.toml") }
    public var logFile: URL { home.appending(path: "engine.log") }

    /// `~/Documents/agentline/pipelines`, or `<override>/pipelines` when overridden.
    public var defaultPipelinesDir: URL {
        if overridden { return home.appending(path: "pipelines", directoryHint: .isDirectory) }
        return URL(fileURLWithPath: NSHomeDirectory())
            .appending(path: "Documents/agentline/pipelines", directoryHint: .isDirectory)
    }

    public static let configTemplate = """
        # agentline settings for the Mac app.
        #
        # Edit agents, model sources and models in the app (Settings), or by hand here.
        # API keys are kept in the macOS Keychain (Settings > Keys), never in this file.
        # The running engine picks up edits to this file by itself (no restart needed).
        #
        # Reference: docs/harnesses-and-models.md in the agentline repo.

        """
}

/// The Python interpreter and built editor the engine runs with.
public struct BackendLocation: Sendable, Equatable {
    public let python: URL
    public let web: URL?
    public let isDev: Bool

    public enum Problem: Error, LocalizedError, Equatable {
        case missing(String)
        public var errorDescription: String? {
            if case .missing(let what) = self { return what }
            return nil
        }
    }

    /// `AGENTLINE_DEV_PYTHON` / `AGENTLINE_DEV_WEB` win; otherwise the bundle's
    /// `Resources/backend/python/bin/python3` and `Resources/backend/web`.
    public static func resolve(
        environment: [String: String], resources: URL?, fileExists: (String) -> Bool
    ) throws -> BackendLocation {
        if let dev = environment["AGENTLINE_DEV_PYTHON"], !dev.isEmpty {
            let python = URL(fileURLWithPath: (dev as NSString).expandingTildeInPath)
            guard fileExists(python.path) else {
                throw Problem.missing("AGENTLINE_DEV_PYTHON points at \(python.path), which doesn't exist")
            }
            var web: URL?
            if let w = environment["AGENTLINE_DEV_WEB"], !w.isEmpty {
                web = URL(fileURLWithPath: (w as NSString).expandingTildeInPath, isDirectory: true)
            }
            return BackendLocation(python: python, web: web, isDev: true)
        }
        guard let resources else {
            throw Problem.missing("no app bundle resources; set AGENTLINE_DEV_PYTHON for development")
        }
        let backend = resources.appending(path: "backend", directoryHint: .isDirectory)
        let python = backend.appending(path: "python/bin/python3")
        guard fileExists(python.path) else {
            throw Problem.missing(
                "the bundled engine is missing (\(python.path)); rebuild with macos/scripts/build_backend.sh, or set AGENTLINE_DEV_PYTHON"
            )
        }
        var web: URL? = backend.appending(path: "web", directoryHint: .isDirectory)
        if let w = environment["AGENTLINE_DEV_WEB"], !w.isEmpty {
            web = URL(fileURLWithPath: (w as NSString).expandingTildeInPath, isDirectory: true)
        }
        return BackendLocation(python: python, web: web, isDev: false)
    }
}

/// A command line + environment for a child process. Built by pure functions so tests can
/// check it without spawning anything.
public struct LaunchSpec: Sendable, Equatable {
    public let executable: URL
    public let arguments: [String]
    public let environment: [String: String]

    public static let extraPath = [
        "/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.npm-global/bin", "~/.claude/local",
    ]

    /// The environment every engine process gets.
    public static func environment(
        base: [String: String], paths: AppPaths, location: BackendLocation, token: String,
        user: String, keys: [String: String], homeDir: String = NSHomeDirectory()
    ) -> [String: String] {
        var env = base
        // Never let a developer's Python settings leak into the bundled interpreter.
        for k in ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"] { env[k] = nil }
        // Variables only the app itself reads.
        for k in ["AGENTLINE_DEV_PYTHON", "AGENTLINE_DEV_WEB", "AGENTLINE_APP_SUPPORT_OVERRIDE", "AGENTLINE_TEST_CRASH"] { env[k] = nil }
        for (k, v) in keys { env[k] = v }
        env["AGENTLINE_HOME"] = paths.home.path
        env["AGENTLINE_DB"] = paths.db.path
        env["AGENTLINE_CONFIG"] = paths.config.path
        env["AGENTLINE_API_TOKEN"] = token
        env["AGENTLINE_KEYS_MANAGED"] = "file"  // the app's Keys screen writes <home>/secrets.env
        env["AGENTLINE_USER"] = user
        // The engine stops by itself if the app dies (crash, Force Quit, kill -9).
        env["AGENTLINE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        if let web = location.web { env["AGENTLINE_WEB_DIST"] = web.path }
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PATH"] = extendedPath(env["PATH"], homeDir: homeDir)
        return env
    }

    /// `PATH` plus the usual places agent CLIs (`claude`, `codex`, `aider`) live, no duplicates.
    public static func extendedPath(_ current: String?, homeDir: String) -> String {
        var parts = (current ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        for p in extraPath {
            let expanded = p.hasPrefix("~/") ? homeDir + p.dropFirst(1) : p
            if !parts.contains(expanded) { parts.append(expanded) }
        }
        return parts.joined(separator: ":")
    }

    /// `python3 -m agentline.cli serve --host 127.0.0.1 --port <port> --concurrency 4 --pipelines <dir>`.
    public static func serve(
        location: BackendLocation, pipelinesDir: URL, port: Int, environment: [String: String]
    ) -> LaunchSpec {
        LaunchSpec(
            executable: location.python,
            arguments: [
                "-m", "agentline.cli", "serve",
                "--host", "127.0.0.1", "--port", String(port), "--concurrency", "4",
                "--pipelines", pipelinesDir.path,
            ],
            environment: environment
        )
    }

    /// `python3 -m agentline.cli new ticket-to-eod <dir>`: seeds an empty pipelines folder.
    public static func seed(location: BackendLocation, pipelinesDir: URL, environment: [String: String]) -> LaunchSpec {
        LaunchSpec(
            executable: location.python,
            arguments: ["-m", "agentline.cli", "new", "ticket-to-eod", pipelinesDir.path],
            environment: environment
        )
    }
}

public enum Tokens {
    /// 32 random bytes as 64 lowercase hex characters.
    public static func random() -> String {
        var rng = SystemRandomNumberGenerator()
        return (0..<32).map { _ in String(format: "%02x", UInt8.random(in: 0...255, using: &rng)) }.joined()
    }
}

/// The user name the engine records as the actor of approvals.
public func macUserName() -> String {
    let full = NSFullUserName()
    return full.isEmpty ? NSUserName() : full
}
