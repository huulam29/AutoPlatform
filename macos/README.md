# agentline for macOS

A native SwiftUI app that runs the whole agentline platform on your Mac: the engine runs as a
private child process, runs and approvals are native windows, and the pipeline editor opens in
its own window. No browser, no terminal. Built and installed locally for one user, ad-hoc
signed.

## Build and install

You need Xcode (Swift 6), [uv](https://docs.astral.sh/uv/) and Node (for the editor).

```bash
macos/scripts/build_backend.sh          # relocatable Python + agentline[server] + editor -> macos/build/backend
macos/scripts/build_app.sh              # -> macos/build/Agentline.app (bundles the backend, ad-hoc signs)
macos/scripts/build_app.sh --install    # ...and copy to /Applications (~/Applications if not writable)
macos/scripts/build_app.sh --backend --install   # all of it in one go
```

`build_app.sh` asks nothing. It runs `swift build -c release`, writes `Info.plist` (bundle id
`dev.agentline.app`), draws the icon (`scripts/make_icon.swift` + `iconutil`), copies
`macos/build/backend/` into `Contents/Resources/backend/`, and signs with
`codesign --force --deep -s -`. When it installs over a running copy, it quits that copy first.

Tests: `cd macos && swift test` (XCTest; no network, no engine, a throwaway Keychain service).

## Development mode

Run against the repo instead of the bundled engine:

```bash
uv sync --extra server && (cd web && npm ci && npm run build)
AGENTLINE_DEV_PYTHON=$PWD/.venv/bin/python AGENTLINE_DEV_WEB=$PWD/web/dist \
  macos/build/Agentline.app/Contents/MacOS/Agentline
```

`swift run` from `macos/` works too, without notifications (they need an app bundle).

Other variables, for testing only:

| Variable | Effect |
|---|---|
| `AGENTLINE_APP_SUPPORT_OVERRIDE=<dir>` | keep all app data in `<dir>` (and pipelines in `<dir>/pipelines`) |
| `AGENTLINE_SNAPSHOT_DIR=<dir>` | once the engine is up, write `main.png` and `editor.png` of the app's own windows there |
| `AGENTLINE_SNAPSHOT_RUN=<run id>\|first`, `AGENTLINE_SNAPSHOT_DELAY=<s>`, `AGENTLINE_SNAPSHOT_QUIT=1` | select a run first, wait longer, quit afterwards |

## Where things live

| What | Where |
|---|---|
| Ledger, config, engine pid | `~/Library/Application Support/agentline/` (`agentline.db`, `agentline.toml`, `engine.pid`) |
| Crash and failure reports | `~/Library/Application Support/agentline/incidents/` (plus `running.marker`, `crash-scan.json` next to it) |
| Pipelines | `~/Documents/agentline/pipelines` by default (Settings > General to change). An empty folder gets the `ticket-to-eod` template once. |
| API keys | the login Keychain, service `dev.agentline.keys`, one item per env var name |
| Preferences | `defaults read dev.agentline.app` (just the pipelines folder) |

The engine listens on `127.0.0.1` on a free port picked at each start, with a random
per-start API token (`AGENTLINE_API_TOKEN`). The editor window gets the token injected into
its page; nothing else can use the API without it.

## How keys work

Settings > Keys lists every key name the settings reference (each model source's
`api_key_env`), the Jira keys (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`), and anything
already stored. Set a key and it goes into the Keychain; the app never shows it again. At each
engine start the app reads all stored keys and passes them to the engine as environment
variables, so **restart the engine after changing a key** (the app offers a button). The engine
runs with `AGENTLINE_KEYS_MANAGED=keychain`, so the web Settings page and
`PUT /api/secrets/*` refuse to store keys in `~/.agentline/secrets.env`.

The app is ad-hoc signed, so after every rebuild macOS sees a "new" app and may ask once to let
it read its Keychain items. Choose "Always Allow".

## Using it

The app has one window, **Agentline Studio**: the web UI served by the app's engine.

- **Pipelines** (⌘1): the visual editor, with the **Chat** panel that builds and edits pipelines.
- **Runs** (⌘2, ⌘N starts a run): status, node colors, tasks, journal, retry.
- **Approvals** (⌘3): every waiting gate. The count shows in the nav, the Dock icon and the
  menu bar icon. Notifications offer Approve / Reject… / Open.
- **Problems** (⌘4): one issue per problem with status tracking (see below).
- **Costs**, **Logs** (⌥⌘L): spend and budget; the engine output.
- **Settings** (⌘,): Agents (with "Chat uses", the Chat's agent and model), Model sources,
  Models, MCP, Skills, Keys (stored in the Keychain by the app), Chat apps, General.
- The **menu bar icon** keeps working with every window closed: status, counts, Start Run…,
  Restart Engine, Quit. Quitting stops the engine (SIGTERM, then SIGKILL after 5 s).

## Crash and failure reports

The **Problems** page (its badge counts open issues) lists:

- **Crashes and errors.** These are incident files written by the app and by the engine:
  - app crashes (from macOS crash reports in `~/Library/Logs/DiagnosticReports`);
  - uncaught exceptions;
  - "agentline quit unexpectedly" (Force Quit, `kill -9`; found at the next launch);
  - engine exits the app didn't ask for (with the last 200 log lines);
  - the engine's own exceptions, API 500s and worker errors.

  The same problem is kept in one file and counts up.
- **Failed runs.** Dead and needs-review runs, linked to Run detail. These are pipeline outcomes, not crashes.

**Where reports live:** `~/Library/Application Support/agentline/incidents/`, with one `<id>.json` file per
problem and an `<id>/` folder for attachments such as the macOS `.ips` file. The engine writes to
the same folder (`$AGENTLINE_HOME/incidents`); `agentline incidents list|show|export` reads it
too.

**Privacy:** reports are stored on this Mac only. Nothing is uploaded or sent anywhere. Before
anything is written, every Keychain key value and the engine's API token are replaced with
`[REDACTED]`, and so is anything that looks like a token (`sk-…`, `ghp_…`, `xox…`, `Bearer …`,
long values after `token=` / `key=` / `password=`). Copied crash reports are redacted the same way.

**Exporting:** "Export Report…" saves a `.zip` (`agentline-report-<date>.zip`) that holds:
- the reports and their attachments;
- `system.txt` (app and engine version, macOS, chip, uptime);
- `engine.log` (the last 500 lines, redacted);
- `summary.md`.

Attach it to an issue yourself. "Copy Summary" puts the markdown summary on the pasteboard.
"Scan for Crash Reports" looks for new macOS crash reports right away (the app also scans at
launch, and again 30 s later, because macOS can take a few seconds to write a report).

When the app died last time, the next launch shows a banner, "agentline quit unexpectedly last
time", with View Report, and posts one notification. Engine exits notify at most once per
problem every 10 minutes.

Verification-only variable: `AGENTLINE_TEST_CRASH=exception|trap` crashes the app on purpose a
few seconds after launch (an NSException or a `fatalError`), and `AGENTLINE_SNAPSHOT_SCREEN=problems` adds
`problems.png` to the snapshots.

## Uninstall

```bash
osascript -e 'tell application id "dev.agentline.app" to quit'
rm -rf /Applications/Agentline.app ~/Applications/Agentline.app
rm -rf ~/Library/Application\ Support/agentline         # ledger and config (your run history)
defaults delete dev.agentline.app
# keys: Keychain Access > search "dev.agentline.keys", or per key:
security delete-generic-password -s dev.agentline.keys -a OPENROUTER_API_KEY
```

Your pipelines in `~/Documents/agentline/pipelines` are left alone.
