import AgentlineCore
import AppKit
import SwiftUI

@main
struct AgentlineApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    private let model = AppModel.shared

    var body: some Scene {
        Window("Agentline Studio", id: "main") {
            StudioWindow()
                .environment(model)
        }
        .defaultSize(width: 1320, height: 840)
        .commands { AppCommands(model: model) }

        MenuBarExtra {
            MenuBarContent()
                .environment(model)
        } label: {
            MenuBarLabel(model: model)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Held while the app runs: one app (and so one engine) per data folder.
    private var instanceLock: InstanceLock?

    func applicationWillFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated {
            let lock = InstanceLock(home: AppModel.shared.backend.paths.home)
            if case .heldBy(let pid) = lock.acquire() {
                Self.handOver(to: pid)
            } else {
                instanceLock = lock
            }
        }
    }

    /// Another copy already runs on this data: bring it forward (a reopen event, so it shows its
    /// main window even from the menu bar) and quit without touching the engine or run marker.
    @MainActor private static func handOver(to pid: pid_t?) {
        let other = pid.flatMap { NSRunningApplication(processIdentifier: $0) }
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
                .first { $0.processIdentifier != getpid() }
        guard let other, let url = other.bundleURL, url.pathExtension == "app" else {
            other?.activate()
            exit(0)
        }
        let config = NSWorkspace.OpenConfiguration()
        config.createsNewApplicationInstance = false
        NSWorkspace.shared.openApplication(at: url, configuration: config) { _, _ in exit(0) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard instanceLock != nil else { return }  // handing over to the running copy
        MainActor.assumeIsolated {
            if Bundle.main.bundleURL.pathExtension != "app" {
                NSApp.setActivationPolicy(.regular)  // `swift run`: still get a Dock icon and menus
            }
            AppModel.shared.launch()
        }
    }

    /// Closing the last window keeps the app (and the engine) running in the menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { MainActor.assumeIsolated { AppModel.shared.activateMainWindow() } }
        return true
    }

    /// Stop the engine cleanly (SIGTERM, SIGKILL after 5 s) before quitting.
    /// Blocking on purpose: `.terminateLater` + a main-actor Task deadlocks when terminate is
    /// called from inside a main-actor job (the modal quit loop can't drain the main queue).
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        MainActor.assumeIsolated { AppModel.shared.shutdownNow() }
        return .terminateNow
    }
}

struct AppCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("Start Run…") { model.show("#/runs/new") }
                .keyboardShortcut("n")
            Button("Pipelines") { model.show("#/pipelines") }
                .keyboardShortcut("1")
            Button("Runs") { model.show("#/runs") }
                .keyboardShortcut("2")
            Button("Approvals") { model.show("#/approvals") }
                .keyboardShortcut("3")
            Button("Problems") { model.show("#/problems") }
                .keyboardShortcut("4")
        }
        CommandGroup(replacing: .appSettings) {
            Button("Settings…") { model.show("#/settings") }
                .keyboardShortcut(",")
        }
        CommandMenu("Engine") {
            Button("Refresh") { model.refresh() }
                .keyboardShortcut("r")
            Button("Restart Engine") { model.restartEngine() }
                .keyboardShortcut("r", modifiers: [.command, .shift])
            Divider()
            Button("Show Logs") { model.show("#/logs") }
                .keyboardShortcut("l", modifiers: [.command, .option])
        }
    }
}

struct MenuBarLabel: View {
    let model: AppModel

    var body: some View {
        let n = model.waitingCount
        if n > 0 {
            Label("\(n)", systemImage: "flowchart.fill")
                .labelStyle(.titleAndIcon)
                .accessibilityLabel("agentline, \(n) approvals waiting")
        } else {
            Image(systemName: "flowchart")
                .accessibilityLabel("agentline")
        }
    }
}

struct MenuBarContent: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Text("Engine: \(model.backend.state.label)")
        Text("Running: \(model.runningCount)")
        Text("Waiting for approval: \(model.waitingCount)")
        Text("Needs review: \(model.needsReviewCount)")
        Divider()
        Button("Open Agentline Studio") { model.show(model.route.path) }
        Button("Start Run…") { model.show("#/runs/new") }
        if model.waitingCount > 0 {
            Button("Show Approvals") { model.show("#/approvals") }
        }
        Divider()
        Button("Restart Engine") { model.restartEngine() }
        Button("Quit agentline") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}
