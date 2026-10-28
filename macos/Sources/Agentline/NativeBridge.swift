import AgentlineCore
import AppKit
import WebKit

/// What the web UI asks the app for (`web/src/lib/native.ts`):
/// `webkit.messageHandlers.agentline.postMessage({action, ...})` resolves with a reply object;
/// failures reply `{ok: false, error}`. Only the things a browser can't do live here: Keychain
/// keys (the secrets file), the engine process, the app's own log, folder pickers, Finder, crash-report export.
@MainActor
final class NativeBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let name = "agentline"

    /// Jira polling needs these three; chat links need these tokens.
    static let jiraKeys = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    static let chatKeys = ["TELEGRAM_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "DISCORD_BOT_TOKEN"]

    private var model: AppModel { AppModel.shared }

    func userContentController(
        _ controller: WKUserContentController, didReceive message: WKScriptMessage
    ) async -> (Any?, String?) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else {
            return (Self.failure("bad request"), nil)
        }
        do {
            return (try await handle(action, body), nil)
        } catch {
            return (Self.failure(error.localizedDescription), nil)
        }
    }

    static func failure(_ message: String) -> [String: Any] { ["ok": false, "error": message] }

    struct BridgeError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    private func handle(_ action: String, _ args: [String: Any]) async throws -> [String: Any] {
        let backend = model.backend
        switch action {
        case "app.info":
            return [
                "ok": true,
                "version": AppModel.appVersion,
                "dataFolder": backend.paths.home.path,
                "configPath": backend.paths.config.path,
                "pipelinesDir": backend.pipelinesDir.path,
                "engineState": backend.state.label,
                "address": backend.port.map { "127.0.0.1:\($0)" } as Any,
                "dev": backend.location?.isDev == true,
            ]
        case "logs.get":
            let redactor = model.reportRedactor
            let limit = max(1, min((args["limit"] as? Int) ?? 500, backend.log.capacity))
            return ["ok": true, "lines": backend.log.lines.suffix(limit).map { redactor.redact($0) }]
        case "engine.restart":
            Task { await model.restartEngineAndWait() }  // reply first: this page goes away
            return ["ok": true]
        case "folder.choose":
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.directoryURL = backend.pipelinesDir
            panel.prompt = "Use Folder"
            guard panel.runModal() == .OK, let url = panel.url else { return ["ok": true] }
            model.setPipelinesDir(url)
            return ["ok": true, "path": url.path]
        case "finder.dataFolder":
            NSWorkspace.shared.activateFileViewerSelecting([backend.paths.home])
            return ["ok": true]
        case "finder.reveal":
            guard let path = args["path"] as? String, FileManager.default.fileExists(atPath: path) else {
                throw BridgeError(message: "no such file or folder")
            }
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            return ["ok": true]
        case "finder.incident":
            let store = model.incidentStore
            let id = (args["id"] as? String ?? "").replacingOccurrences(of: "/", with: "")
            let file = store.file(for: id)
            let target = !id.isEmpty && FileManager.default.fileExists(atPath: file.path) ? file : store.dir
            NSWorkspace.shared.activateFileViewerSelecting([target])
            return ["ok": true]
        case "problems.scan":
            let found = model.scanCrashReports(previous: nil)
            await model.reloadIncidents()
            let msg = found.isEmpty ? "No new crash reports." : "Recorded \(found.count) crash report(s)."
            return ["ok": true, "message": msg]
        case "problems.export":
            model.problemsMessage = nil
            await model.exportReport(ids: nil)
            return ["ok": true, "message": model.problemsMessage ?? ""]
        case "keys.list":
            return ["ok": true, "keys": await keyRows()]
        case "keys.set":
            guard let name = args["name"] as? String, KeychainStore.isValidName(name) else {
                throw BridgeError(message: "a key name is an env var name, e.g. MY_SERVER_KEY")
            }
            guard let value = args["value"] as? String, !value.isEmpty else {
                throw BridgeError(message: "empty value")
            }
            let notice = try KeySaver.save(value, for: name, model: model)
            return ["ok": true, "notice": notice as Any]
        case "keys.delete":
            guard let name = args["name"] as? String, KeychainStore.isValidName(name) else {
                throw BridgeError(message: "bad key name")
            }
            try SecretsFile(home: backend.paths.home).delete(name)  // the engine follows the file
            return ["ok": true]
        default:
            throw BridgeError(message: "unknown action \(action)")
        }
    }

    /// Every key worth showing: the ones the config references, stored ones, Jira and chat keys.
    private func keyRows() async -> [[String: Any]] {
        let file = SecretsFile(home: model.backend.paths.home)
        let inFile = Set(file.names())
        let referenced = (try? await model.client?.settings())?.keys ?? []
        let fromEnv = Set(referenced.filter(\.set).map(\.name))
        let names = Set(referenced.map(\.name)).union(inFile)
            .union(Self.jiraKeys).union(Self.chatKeys)
        return names.sorted().map { name in
            let place = inFile.contains(name) ? "file" : fromEnv.contains(name) ? "environment" : "none"
            var row: [String: Any] = ["name": name, "where": place]
            if Self.jiraKeys.contains(name) { row["jira"] = true }
            return row
        }
    }
}
