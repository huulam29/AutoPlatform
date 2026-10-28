import AgentlineCore
import AppKit
import Foundation
@preconcurrency import UserNotifications

/// Local notifications for gates and finished runs, with Approve / Reject / Open actions.
@MainActor
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    weak var model: AppModel?
    private(set) var available = false

    static let approvalCategory = "dev.agentline.approval"
    static let runCategory = "dev.agentline.run"
    static let incidentCategory = "dev.agentline.incident"
    static let approveAction = "approve"
    static let rejectAction = "reject"
    static let openAction = "open"

    /// UNUserNotificationCenter needs a real app bundle; a bare `swift run` binary has none.
    static var bundleSupportsNotifications: Bool {
        Bundle.main.bundleURL.pathExtension == "app" && Bundle.main.bundleIdentifier != nil
    }

    func setUp() {
        guard Self.bundleSupportsNotifications else { return }
        available = true
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let approve = UNNotificationAction(identifier: Self.approveAction, title: "Approve", options: [])
        let reject = UNNotificationAction(
            identifier: Self.rejectAction, title: "Reject…", options: [.destructive, .foreground])
        let open = UNNotificationAction(identifier: Self.openAction, title: "Open", options: [.foreground])
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: Self.approvalCategory, actions: [approve, reject, open], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.runCategory, actions: [open], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.incidentCategory, actions: [open], intentIdentifiers: []),
        ])
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func post(_ event: EngineEvent) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = event.title
        content.body = event.body
        var info: [String: String] = ["run_id": event.runId]
        switch event {
        case .approvalNeeded(let a):
            content.categoryIdentifier = Self.approvalCategory
            content.sound = .default
            info["node_id"] = a.nodeId
            info["execution_id"] = a.executionId
            info["pipeline"] = a.pipeline
        case .runFinished:
            content.categoryIdentifier = Self.runCategory
        }
        content.userInfo = info
        let req = UNNotificationRequest(identifier: event.dedupeKey, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { _ in }
    }

    /// A crash or failure report (local only; the notification just opens Problems). The
    /// caller rate-limits; the identifier also replaces an older banner for the same incident.
    func postIncident(_ incident: Incident, title: String) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = incident.count > 1 ? "\(incident.title) (\(incident.count) times)" : incident.title
        content.categoryIdentifier = Self.incidentCategory
        content.userInfo = ["incident_id": incident.id]
        let req = UNNotificationRequest(identifier: "incident:\(incident.id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { _ in }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        let info = response.notification.request.content.userInfo
        let runId = info["run_id"] as? String ?? ""
        let nodeId = info["node_id"] as? String
        let execId = info["execution_id"] as? String ?? ""
        let pipeline = info["pipeline"] as? String ?? ""
        let action = response.actionIdentifier
        if let incidentId = info["incident_id"] as? String {
            await MainActor.run { self.model?.showProblem(incidentId) }
            return
        }
        await MainActor.run {
            guard let model = self.model, !runId.isEmpty else { return }
            switch action {
            case Self.approveAction:
                Task {
                    if let err = await model.decide(true, runId: runId, node: nodeId, comment: "approved from a notification") {
                        model.banner = err
                        model.showRun(runId)
                    }
                }
            case Self.rejectAction:
                model.showRun(runId)
                model.pendingReject = Approval(
                    runId: runId, pipeline: pipeline, nodeId: nodeId ?? "", executionId: execId,
                    prompt: nil, approvers: nil, waitingSince: nil, payload: nil)
            default:
                model.showRun(runId)
            }
        }
    }
}
