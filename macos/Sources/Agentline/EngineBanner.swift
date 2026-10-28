import AgentlineCore
import AppKit
import SwiftUI

/// Shown above Agentline Studio while the engine isn't running, or after an action failed.
struct EngineBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let state = model.backend.state
        VStack(spacing: 0) {
            if let crash = model.crashBanner {
                HStack(spacing: 10) {
                    Image(systemName: "exclamationmark.octagon.fill").foregroundStyle(.red)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("agentline quit unexpectedly last time").fontWeight(.medium)
                        if crash.kind != IncidentKind.appUncleanExit {
                            Text(crash.title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer()
                    Button("View Report") {
                        model.showProblem(crash.id)
                        model.crashBanner = nil
                    }
                    .keyboardShortcut(.defaultAction)
                    Button("Dismiss") { model.crashBanner = nil }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.12))
                .accessibilityElement(children: .contain)
                .accessibilityLabel("agentline quit unexpectedly last time. \(crash.title)")
            }
            if state != .running {
                HStack(spacing: 10) {
                    if state == .starting {
                        ProgressView().controlSize(.small)
                        Text("Starting the engine…")
                    } else {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        Text(state.problem ?? "The engine is \(state.label.lowercased()).")
                            .lineLimit(3)
                            .textSelection(.enabled)
                    }
                    Spacer()
                    if state != .starting {
                        Button("Show Logs") { model.show("#/logs") }
                        if case .crashed = state { Button("Problems") { model.show("#/problems") } }
                        Button(state == .stopped ? "Start Engine" : "Restart Engine") { model.restartEngine() }
                            .keyboardShortcut(.defaultAction)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(state == .starting ? Color.secondary.opacity(0.08) : Color.orange.opacity(0.15))
            }
            if model.restartNeeded {
                HStack {
                    Image(systemName: "arrow.clockwise.circle.fill").foregroundStyle(.blue)
                    Text("Restart the engine to apply your changes.")
                    Spacer()
                    Button("Restart Engine") { model.restartEngine() }
                        .accessibilityLabel("Restart engine to apply changes")
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color.blue.opacity(0.10))
            }
            if let msg = model.banner {
                HStack {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                    Text(msg).textSelection(.enabled).lineLimit(4)
                    Spacer()
                    if let id = model.bannerIncident {
                        Button("View Report") {
                            model.showProblem(id)
                            model.banner = nil
                            model.bannerIncident = nil
                        }
                    }
                    Button("Dismiss") {
                        model.banner = nil
                        model.bannerIncident = nil
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.10))
            }
            if state == .running, let err = model.pollError {
                HStack {
                    Image(systemName: "wifi.exclamationmark").foregroundStyle(.orange)
                    Text(err).lineLimit(2)
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.orange.opacity(0.10))
            }
            if state != .running || model.restartNeeded || model.banner != nil || model.pollError != nil
                || model.crashBanner != nil
            {
                Divider()
            }
        }
    }
}

// MARK: - shared bits

/// Green done, red failed, yellow running, grey pending / waiting (same as the editor's run view).
func statusColor(_ status: String) -> Color {
    switch status {
    case "running": return .yellow
    case "done": return .green
    case "failed", "dead", "needs-review": return .red
    case "skipped", "cancelled": return Color.gray.opacity(0.6)
    default: return .gray  // pending, ready, waiting
    }
}

struct StatusBadge: View {
    let status: String

    var body: some View {
        Text(status)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(statusColor(status).opacity(0.18), in: Capsule())
            .foregroundStyle(statusColor(status))
            .accessibilityLabel("Status \(status)")
    }
}

enum Fmt {
    static func usd(_ v: Double?) -> String {
        guard let v else { return "–" }
        return v < 0.01 && v > 0 ? String(format: "$%.4f", v) : String(format: "$%.2f", v)
    }

    static func cost(_ cost: Double?, budget: Double?) -> String {
        if let budget { return "\(usd(cost ?? 0)) / \(usd(budget))" }
        return usd(cost ?? 0)
    }

    static func date(_ t: Timestamp?) -> String {
        guard let t else { return "–" }
        return t.date.formatted(date: .abbreviated, time: .shortened)
    }

    static func relative(_ t: Timestamp?) -> String {
        guard let t else { return "–" }
        return t.date.formatted(.relative(presentation: .named))
    }
}

struct EmptyState: View {
    let title: String
    let symbol: String
    let message: String

    var body: some View {
        ContentUnavailableView(title, systemImage: symbol, description: Text(message))
    }
}
