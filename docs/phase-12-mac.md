# Phase 12: the macOS app (as built)

A SwiftUI app in `macos/` that installs to /Applications and runs agentline locally with
native windows. Build, install and data locations: [macos/README.md](../macos/README.md). The
engine-side endpoints it uses were added on `phase-12` (see `docs/phase-12.md` if present).

## Design

```
Agentline.app
├─ Contents/MacOS/Agentline            SwiftUI app (no third-party dependencies)
└─ Contents/Resources/backend/
   ├─ python/bin/python3               relocatable CPython + agentline[server]
   └─ web/                             built editor
        │
        │ spawns:  python3 -m agentline.cli serve --host 127.0.0.1 --port <free> --concurrency 4
        │                    --pipelines <folder>
        ▼
   engine on 127.0.0.1:<port>  ◀── URLSession (Bearer token) ── native screens, notifications
                               ◀── WKWebView (token in localStorage) ── editor window
```

Swift package `macos/Package.swift` (tools 6.0, Swift 6 language mode, macOS 15+):

| Target | Contents |
|---|---|
| `AgentlineCore` (library) | wire models (`Models.swift`, `JSONValue`), `APIClient`, `BackendLaunch` (paths, interpreter lookup, the pure command/env builder), `BackendManager` (child process, health, logs), `KeychainStore`, `NotificationPlanner`, `PortFinder`, `LogBuffer`, model source presets |
| `Agentline` (executable) | `AppModel` (shared `@MainActor @Observable` state + 2 s polling), screens, editor window, menu bar extra, `Notifier`, `Snapshot` (verification aid) |
| `AgentlineCoreTests` | 28 XCTests: decoding against JSON fixtures shaped like the contract, request headers and bodies (URLProtocol stub), launch command/env, Keychain round trip on a throwaway service, notification de-duplication, free ports, log buffer |

### Engine process

- Env: `AGENTLINE_HOME`/`_DB`/`_CONFIG` under Application Support, `AGENTLINE_API_TOKEN`
  (32 random bytes, hex, new per engine start), `AGENTLINE_WEB_DIST`,
  `AGENTLINE_KEYS_MANAGED=keychain`, `AGENTLINE_USER` (full name, else login name), every
  Keychain key, `PATH` + `/opt/homebrew/bin:/usr/local/bin:~/.local/bin:~/.npm-global/bin`,
  `PYTHONUNBUFFERED=1`, `PYTHONNOUSERSITE=1`; `PYTHONHOME`/`PYTHONPATH`/`VIRTUAL_ENV` and the
  app's own dev variables are removed.
- First run: creates the data folder, a commented empty `agentline.toml`, the pipelines folder;
  seeds an empty pipelines folder once with `python3 -m agentline.cli new ticket-to-eod <dir>`
  (remembered per folder in `<home>/.seeded`, so deleting the template doesn't bring it back).
- Health: polls `GET /healthz` every 250 ms for up to 90 s (any 200 is healthy). States:
  stopped, starting, running, crashed (unexpected exit or no answer), can't start (missing
  bundle). An exit while running shows the engine-down banner with "Restart Engine".
- Output: stdout+stderr into a 200-line ring buffer (Engine Logs).
- Stop: SIGTERM, wait up to 5 s, SIGKILL. `engine.pid` lets the next launch stop an engine
  that a crashed app left behind (only if that pid is still a Python).

### Screens

Main window (`NavigationSplitView`): Runs (list + detail side by side), Approvals, Pipelines,
Settings, Engine Logs. A banner above every screen shows engine down / restart needed / the
last action error. Separate windows: the pipeline editor (`Window` id `editor`). Menu bar extra
with status and counts. Commands: ⌘N Start Run, ⌘R Refresh, ⇧⌘R Restart Engine, ⌘, Settings,
⇧⌘E Editor, ⌥⌘L Logs.

### Notifications

`NotificationPlanner` (pure, tested) turns each poll into events:

- every waiting gate once per execution id (gates already waiting at launch included: they need
  a decision);
- runs that *become* done / dead / needs-review after the first poll (the first poll is a
  baseline, so old runs don't flood the screen); keyed by run id + status.

Approve acts in the background; Reject… opens the app and asks for confirmation (with a
comment); Open shows the run. Nothing is remembered across launches.

## Decisions (proposals for the decision log)

1. **Core library + thin app target.** Everything testable lives in `AgentlineCore`; the
   SwiftUI target holds views and AppKit glue only. No Xcode project: SwiftPM + a shell script
   assembles the bundle.
2. **Blocking stop on quit.** `applicationShouldTerminate` stops the engine synchronously and
   returns `.terminateNow`. The `.terminateLater` + main-actor `Task` version deadlocks when
   `terminate` is called from inside a main-actor job (seen in testing).
3. **Approvals fallback.** If `GET /api/approvals` answers 404/405 (an older engine), gates are
   found by reading the details of waiting runs.
4. **Ticket field heuristic.** Start Run shows "Ticket key" when the pipeline is in
   `jira_poll.pipelines` or its name contains "jira" or "ticket"; it writes `issue_key` into the
   payload JSON, which stays editable.
5. **Jira credentials as Keychain keys.** `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` are
   always listed under Keys (the base URL isn't secret, but one place for engine env is simpler).
6. **Editor web view.** Non-persistent website data store; the token is injected at document
   start on every load; same-origin navigation stays inside, everything else goes to the
   default browser; Web Inspector only in debug builds; JS alert/confirm/prompt are native
   panels.
7. **Self-snapshot for verification.** The shell running the build had no Screen Recording
   permission, so `screencapture` couldn't capture; with `AGENTLINE_SNAPSHOT_DIR` set the app
   renders its own main window and the editor web view to PNG. It's inert otherwise.

## Verified

- `swift build`, `swift test` (28 tests) pass; `build_app.sh` builds an 85 MB app with the
  bundled backend, signature verifies.
- Launched the built app with the bundled Python and, separately, with
  `AGENTLINE_DEV_PYTHON`/`AGENTLINE_DEV_WEB`, both with `AGENTLINE_APP_SUPPORT_OVERRIDE` on a
  scratch dir: the engine started on a free port, `/healthz` answered, `/api/*` required the
  token, `POST /api/runs` started runs (and returned `created: false` for a repeated dedupe
  key), the runs list, approvals badge (sidebar) and run detail with the waiting gate rendered,
  the editor window loaded the pipeline list through the API with the injected token, and quit
  stopped the engine and removed the pid file.
- Not verified by hand: clicking through the UI (no input automation was available),
  notification delivery and actions, the Keychain prompt after a rebuild.

## Limits

- Ad-hoc signing: macOS may ask again for Keychain access after each rebuild; Gatekeeper
  doesn't apply because the app is built locally (no quarantine flag).
- Keys reach the engine only at start: changing one needs "Restart Engine". Running workers
  keep their settings until a restart (as everywhere in agentline).
- Polling every 2 s, up to 200 most recent runs; runs older than that don't drive counts or
  notifications.
- `AGENTLINE_USER` is the full name; gates whose `approvers` list login names won't match it.
- If the app is force-killed, the engine keeps running until the next launch stops it.
- Jira polling settings are edited in the pipeline's trigger (YAML / editor), not natively.
