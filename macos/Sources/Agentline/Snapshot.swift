import AppKit
import Foundation
import WebKit

/// Verification aid: with `AGENTLINE_SNAPSHOT_DIR` set, once the engine is up the app writes
/// `main.png` (Agentline Studio's web view) there, `<screen>.png` too with
/// `AGENTLINE_SNAPSHOT_SCREEN=<route>` (e.g. `problems`), and quits when
/// `AGENTLINE_SNAPSHOT_QUIT=1`. It draws its own view, so no Screen Recording permission is
/// needed. Unset (always, for normal use), this does nothing.
@MainActor
enum Snapshot {
    static let dir: URL? = ProcessInfo.processInfo.environment["AGENTLINE_SNAPSHOT_DIR"].flatMap {
        $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true)
    }
    static weak var webView: WKWebView?

    static func run(_ model: AppModel) async {
        guard let dir else { return }
        let env = ProcessInfo.processInfo.environment
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for _ in 0..<240 where !model.backend.isRunning { try? await Task.sleep(for: .milliseconds(500)) }
        let delay = Double(env["AGENTLINE_SNAPSHOT_DELAY"] ?? "") ?? 4
        try? await Task.sleep(for: .seconds(delay))  // a few polls, React renders
        // a run id, or "first" for the newest run
        if var selected = env["AGENTLINE_SNAPSHOT_RUN"], !selected.isEmpty {
            if selected == "first" { selected = model.runs.first?.id ?? "" }
            model.showRun(selected)
            try? await Task.sleep(for: .seconds(3))
        }
        await write(to: dir.appending(path: "main.png"))
        if let screen = env["AGENTLINE_SNAPSHOT_SCREEN"], !screen.isEmpty {
            model.show("#/\(screen)")
            try? await Task.sleep(for: .seconds(3))
            await write(to: dir.appending(path: "\(screen).png"))
        }
        if env["AGENTLINE_SNAPSHOT_QUIT"] == "1" {
            NSApp.terminate(nil)
        }
    }

    static func write(to url: URL) async {
        guard let web = webView else {
            AppModel.shared.backend.appendLog("[app] snapshot: no web view")
            return
        }
        let image = try? await web.takeSnapshot(configuration: nil)
        if let image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) {
            try? rep.representation(using: .png, properties: [:])?.write(to: url)
        }
    }
}
