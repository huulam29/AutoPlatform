import AgentlineCore
import AppKit
import Foundation
import Synchronization

/// In-process crash capture: the uncaught-exception handler, and the verification-only test
/// crash. Everything it writes goes through `IncidentStore` (redacted, atomic, locked).
enum CrashCapture {
    struct Config: Sendable {
        var store: IncidentStore
        var markerURL: URL
        var marker: RunMarker
        var version: String
        var platform: String
    }

    private static let config = Mutex<Config?>(nil)

    static func install(_ c: Config) {
        config.withLock { $0 = c }
        NSSetUncaughtExceptionHandler { exception in CrashCapture.handle(exception) }
    }

    /// Once the Keychain was read, the handler redacts key values too.
    static func updateRedactor(_ r: Redactor) {
        config.withLock { $0?.store.redactor = r }
    }

    /// Runs synchronously on the dying thread, before the runtime aborts: record the exception
    /// and note its id in the running marker, so the next launch doesn't also call it an
    /// unclean exit (and attaches macOS's crash report to this incident).
    static func handle(_ e: NSException) {
        guard let c = config.withLock({ $0 }) else { return }
        let draft = ExceptionReport.draft(
            name: e.name.rawValue, reason: e.reason, callStack: e.callStackSymbols, version: c.version,
            platform: c.platform)
        guard let incident = try? c.store.record(draft) else { return }
        var marker = c.marker
        marker.exceptionIncident = incident.id
        try? marker.write(c.markerURL)
    }

    /// Verification only: `AGENTLINE_TEST_CRASH=exception` raises an NSException, `=trap` calls
    /// `fatalError`, a few seconds after launch. Inert unless that variable is set.
    static func scheduleTestCrashIfRequested(after delay: TimeInterval = 6) {
        // only on a throwaway data folder, never the user's real one
        guard let mode = TestCrash.mode(environment: ProcessInfo.processInfo.environment) else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            switch mode {
            case "exception":
                NSException(
                    name: NSExceptionName("AgentlineTestException"),
                    reason: "AGENTLINE_TEST_CRASH=exception (verification only)", userInfo: nil
                ).raise()
            case "trap":
                fatalError("AGENTLINE_TEST_CRASH=trap (verification only)")
            default:
                break
            }
        }
    }
}
