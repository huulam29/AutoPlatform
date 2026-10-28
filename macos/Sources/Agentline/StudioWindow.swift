import AgentlineCore
import AppKit
import SwiftUI
import WebKit

/// The one window: Agentline Studio, the web UI served by the app's private engine, with the
/// engine's state above it. First-run setup is the web UI's SetupDialog.
struct StudioWindow: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    @State private var rejectComment = ""

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            EngineBanner()
            Group {
                if let base = model.backend.baseURL, let token = model.backend.token, model.backend.isRunning {
                    EditorWebView(baseURL: base, token: token, route: model.route)
                        .id(model.backend.generation)  // a restarted engine has a new port and token
                } else {
                    EmptyState(
                        title: model.backend.state == .starting ? "Starting the engine…" : "The engine isn't running",
                        symbol: "bolt.horizontal.circle",
                        message: model.backend.state.problem ?? "Agentline Studio opens when the engine is up.")
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: 900, minHeight: 560)
        .background(Color(nsColor: EditorWebView.surfacePage))
        .preferredColorScheme(.dark)  // Flight Deck is a dark console: title bar and sheets match
        .onAppear { model.openMainWindow = { openWindow(id: "main") } }
        .alert(
            "Reject this gate?",
            isPresented: Binding(get: { model.pendingReject != nil }, set: { if !$0 { model.pendingReject = nil } }),
            presenting: model.pendingReject
        ) { a in
            TextField("Comment (optional)", text: $rejectComment)
            Button("Reject", role: .destructive) {
                let comment = rejectComment
                rejectComment = ""
                Task {
                    if let err = await model.decide(false, runId: a.runId, node: a.nodeId.isEmpty ? nil : a.nodeId, comment: comment) {
                        model.banner = err
                    }
                }
            }
            Button("Cancel", role: .cancel) { rejectComment = "" }
        } message: { a in
            Text("Rejecting gate \(a.nodeId) fails run \(a.runId).")
        }
    }
}

/// The React Flow editor served by the app's private engine. The API token is put in
/// localStorage before the page's scripts run; links to other hosts open in the default browser.
struct EditorWebView: NSViewRepresentable {
    let baseURL: URL
    let token: String
    let route: AppModel.Route

    func makeCoordinator() -> Coordinator { Coordinator(baseURL: baseURL) }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let script = WKUserScript(
            source: "try { localStorage.setItem('agentline.token', '\(token)'); } catch (e) {}",
            injectionTime: .atDocumentStart, forMainFrameOnly: true)
        config.userContentController.addUserScript(script)
        // the editor's Export: `webkit.messageHandlers.agentlineSave.postMessage({filename, base64})`
        config.userContentController.add(context.coordinator, name: "agentlineSave")
        config.userContentController.addScriptMessageHandler(
            context.coordinator.bridge, contentWorld: .page, name: NativeBridge.name)
        let view = WKWebView(frame: .zero, configuration: config)
        view.underPageBackgroundColor = Self.surfacePage  // no white flash before the page paints
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = false
        view.setAccessibilityLabel("Agentline Studio")
        #if DEBUG
            view.isInspectable = true
        #else
            view.isInspectable = false
        #endif
        context.coordinator.route = route
        Snapshot.webView = view
        view.load(URLRequest(url: Self.url(baseURL, route.path)))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        guard context.coordinator.route != route else { return }
        context.coordinator.route = route
        let hash = route.path.hasPrefix("#") ? String(route.path.dropFirst()) : route.path
        let literal = JSONValue.string(hash).compactText
        view.evaluateJavaScript("window.location.hash = \(literal);", completionHandler: nil)
    }

    /// `--surface-page` in web/src/theme.css (#0A0E14).
    static let surfacePage = NSColor(srgbRed: 10 / 255, green: 14 / 255, blue: 20 / 255, alpha: 1)

    static func url(_ base: URL, _ route: String) -> URL {
        URL(string: base.absoluteString + "/" + (route.hasPrefix("#") ? route : "#" + route)) ?? base
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let baseURL: URL
        var route = AppModel.Route(path: "")
        let bridge = NativeBridge()

        init(baseURL: URL) { self.baseURL = baseURL }

        func isInternal(_ url: URL) -> Bool {
            if url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" { return true }
            return url.scheme == baseURL.scheme && url.host == baseURL.host && url.port == baseURL.port
        }

        func webView(
            _ webView: WKWebView, decidePolicyFor action: WKNavigationAction
        ) async -> WKNavigationActionPolicy {
            guard let url = action.request.url else { return .cancel }
            if isInternal(url) { return .allow }
            NSWorkspace.shared.open(url)  // other hosts and schemes: the default browser / app
            return .cancel
        }

        /// `target="_blank"`: internal links load here, external ones go to the browser.
        func webView(
            _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
            for action: WKNavigationAction, windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = action.request.url {
                if isInternal(url) { webView.load(URLRequest(url: url)) } else { NSWorkspace.shared.open(url) }
            }
            return nil
        }

        /// Export from the editor: save the bytes where the user picks.
        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "agentlineSave", let body = message.body as? [String: Any],
                let b64 = body["base64"] as? String, let data = Data(base64Encoded: b64)
            else { return }
            let panel = NSSavePanel()
            let name = (body["filename"] as? String).map { ($0 as NSString).lastPathComponent } ?? "pipeline.zip"
            panel.nameFieldStringValue = name.isEmpty ? "pipeline.zip" : name
            if panel.runModal() == .OK, let url = panel.url {
                do { try data.write(to: url) } catch {
                    let alert = NSAlert(error: error)
                    alert.runModal()
                }
            }
        }

        /// `<input type="file">` (the editor's Import).
        func webView(
            _ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
            initiatedByFrame frame: WKFrameInfo
        ) async -> [URL]? {
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.canChooseDirectories = false
            return panel.runModal() == .OK ? panel.urls : nil
        }

        func webView(
            _ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo
        ) async {
            let alert = NSAlert()
            alert.messageText = message
            alert.runModal()
        }

        func webView(
            _ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo
        ) async -> Bool {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            return alert.runModal() == .alertFirstButtonReturn
        }

        func webView(
            _ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
            defaultText: String?, initiatedByFrame frame: WKFrameInfo
        ) async -> String? {
            let alert = NSAlert()
            alert.messageText = prompt
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
            field.stringValue = defaultText ?? ""
            alert.accessoryView = field
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            return alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil
        }
    }
}
