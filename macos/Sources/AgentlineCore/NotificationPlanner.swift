import Foundation

/// Something worth a user notification.
public enum EngineEvent: Sendable, Equatable {
    case approvalNeeded(Approval)
    case runFinished(RunSummary)  // done, dead or needs-review

    /// What makes it unique: an execution id for gates, run id + status for runs.
    public var dedupeKey: String {
        switch self {
        case .approvalNeeded(let a): return "gate:\(a.executionId)"
        case .runFinished(let r): return "run:\(r.id):\(r.status)"
        }
    }

    public var title: String {
        switch self {
        case .approvalNeeded(let a):
            return "Approval needed: \([a.pipeline, a.ticketKey].compactMap { $0 }.joined(separator: " "))"
        case .runFinished(let r):
            let what = r.status == "needs-review" ? "needs review" : r.status
            return "Run \(what): \([r.flow, r.ticketKey].compactMap { $0 }.joined(separator: " "))"
        }
    }

    public var body: String {
        switch self {
        case .approvalNeeded(let a):
            return a.prompt?.isEmpty == false ? a.prompt! : "Gate \(a.nodeId) is waiting."
        case .runFinished(let r):
            if let e = r.error, !e.isEmpty { return String(e.prefix(200)) }
            return "Run \(r.id)"
        }
    }

    public var runId: String {
        switch self {
        case .approvalNeeded(let a): return a.runId
        case .runFinished(let r): return r.id
        }
    }
}

/// Decides which poll results deserve a notification, never twice for the same thing during
/// one launch.
///
/// - Every waiting gate is announced once (including gates already waiting at launch: they
///   need a decision).
/// - Runs are announced when they *become* done / dead / needs-review after the first poll; the
///   first poll is only a baseline, so old finished runs don't flood the screen at launch.
public struct NotificationPlanner: Sendable {
    public private(set) var notified: Set<String> = []
    private var lastStatus: [String: String] = [:]
    private var hasBaseline = false

    public static let announcedRunStatuses: Set<String> = ["done", "dead", "needs-review"]

    public init() {}

    public mutating func events(approvals: [Approval]?, runs: [RunSummary]?) -> [EngineEvent] {
        var out: [EngineEvent] = []
        for a in approvals ?? [] {
            let e = EngineEvent.approvalNeeded(a)
            if notified.insert(e.dedupeKey).inserted { out.append(e) }
        }
        if let runs {
            for r in runs {
                let before = lastStatus[r.id]
                lastStatus[r.id] = r.status
                guard hasBaseline, before != r.status, Self.announcedRunStatuses.contains(r.status) else {
                    continue
                }
                let e = EngineEvent.runFinished(r)
                if notified.insert(e.dedupeKey).inserted { out.append(e) }
            }
            hasBaseline = true
        }
        return out
    }
}
