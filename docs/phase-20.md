# Phase 20: one app window (Agentline Studio), Chat naming, MCP blank page

User request: Settings → MCP showed a blank page in the Mac app; it wasn't clear where the
chat's default agent is set; the landing page should go and everything should live in the
pipeline editor, renamed to something meaningful, as the main app GUI; "Assistant" becomes
"Chat".

## What changed
- **Agentline Studio** is the web UI's name and the Mac app's only window. Nav: Pipelines,
  Runs, Approvals (badge), Problems (badge), Costs, Logs, Settings.
- New web pages: `Approvals.tsx` (approve/reject with a comment), `Problems.tsx` (issues with
  status, notes, history, Fix this, incident details, copy summary; Scan/Export in the Mac app),
  `Logs.tsx` (engine log, follow, copy), `StartRun.tsx` (Runs → Start run; `#/runs/new` opens it).
  Settings gained **General** (engine, pipelines folder, Jira polling) and a native-Keychain
  Keys tab in the Mac app; "Chat apps" is the chat-link tab.
- **Where the Chat's agent is set**: a "Chat uses" bar (agent + model) at the top of
  Settings → Agents and in Settings → Chat apps; the chat panel's picker names the default
  (`default: claude-code (Settings → Agents)`). Saved in `$AGENTLINE_HOME/setup.json`.
- Engine: `GET /api/logs?limit=` returns the last redacted log lines (`logs.RING`, 2000 lines,
  installed by `setup_logging`; the dev server sets logging up too).
- Mac app: `StudioWindow` (engine banner + `EditorWebView` + setup sheet + reject alert)
  replaces the sidebar and the native Runs/Approvals/Pipelines/Problems/Settings/MCP/Chat/Logs
  screens. Navigation is `AppModel.show("#/route")` (a nonce makes repeat requests navigate).
  Menus: Start Run… (⌘N), Pipelines/Runs/Approvals/Problems (⌘1–4), Settings… (⌘,), Engine →
  Show Logs. Notifications open `#/runs/<id>` / `#/problems`.
- **Native bridge** (`NativeBridge.swift`, `WKScriptMessageHandlerWithReply` "agentline";
  web side `web/src/lib/native.ts`): `app.info`, `logs.get`, `engine.restart`, `folder.choose`,
  `finder.dataFolder|reveal|incident`, `problems.scan|export`, `keys.list|set|delete`
  (Keychain first, secrets file fallback via `KeySaver`). Replies are objects; failures are
  `{ok: false, error}`.
- MCP blank page: the native SwiftUI pane is gone; MCP is the web Settings → MCP tab
  (checked in the browser).

## Checks
- `uv run pytest` (incl. `/api/logs` redaction), ruff, web `tsc` + vitest (84) + build,
  `swift build` + `swift test` (59). Web pages checked in the browser against the dev server.
