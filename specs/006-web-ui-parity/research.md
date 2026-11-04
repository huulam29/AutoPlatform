# Research: Web UI Parity With the Mac App

Each decision below was made by reading the code named under it.

## R1. Restarting the engine from the web

- **Decision**: `POST /api/engine/restart` replies `{"restarting": true}`, then sets a restart
  flag and the `serve` stop event. When `cmd_serve`'s event loop has finished (worker drained,
  uvicorn closed its socket, start marker removed, lock released), it calls
  `os.execv(sys.executable, [sys.executable, "-m", "agentline.cli", *sys.argv[1:]])`. That's the
  same arguments, so the address is the same.
- **Rationale**: `cmd_serve` (`src/agentline/cli/commands.py`) already stops cleanly on the stop
  event (the parent-pid watchdog uses it). An exec in place needs no supervisor and keeps the
  process id, so a process manager doesn't see the engine as dead. Python file descriptors are not
  inheritable by default, so the data folder lock is released on exec.
- **Alternatives**: spawning a child and exiting (the port can be briefly taken by both; a
  terminal loses the process); asking the user to restart by hand (that's the gap we're closing).
- In the Mac app (`AGENTLINE_PARENT_PID` set) the endpoint refuses with 409 "the Mac app restarts
  its engine", and the web keeps using `nativeCall("engine.restart")`.

## R2. "On the engine's own machine"

- **Decision**: `is_local_request(request)` is true when `request.client.host` is loopback
  (`api.server.is_local`) and the request has no `Forwarded`, `X-Forwarded-For` or
  `X-Real-IP` header.
- **Rationale**: matches the spec's assumption. Proxies add those headers, and a proxied request
  must count as remote.
- **Used by**: "Show in folder" (local only), restart (local or token), and the `local` field of
  app info, which the web uses to show or hide "Show in folder".

## R3. Who may browse folders and set the pipelines folder

- **Decision** (user, clarification 1): the same rule as the rest of `/api`. The router uses the
  normal token dependency, so it's open on localhost with no token and needs the token otherwise.
- The folder browser lists directories only, skips names that start with `.`, and marks
  unreadable ones `readable: false`.

## R4. Changing the pipelines folder without a restart

- **Decision**: `PUT /api/app/pipelines-dir {path}` checks the folder exists, writes
  `pipelines_dir = "<abs path>"` to `agentline.toml` (`tomlwrite`), then points the running
  `PipelineFiles` and `LivePipelines` at the new folder and reloads them (new
  `PipelineFiles.set_dir`, `LivePipelines.set_dir`).
- **Rationale**: `LivePipelines.dir` and `PipelineFiles` hold the folder as plain attributes, and
  `LivePipelines.reload()` already rebuilds from `self.dir` (`src/agentline/live.py`). An empty
  folder is seeded with the `ticket-to-eod` template, as the Mac app does.
- **Caveat**: when `serve` was started with `--pipelines`, that flag wins on the next start. The
  reply then carries `note: "started with --pipelines <dir>: that folder is used again after a
  restart"`.

## R5. Show in folder

- **Decision**: `POST /api/app/reveal {path}`, local requests only. The path must be inside the
  data folder, the pipelines folder or the settings file's folder. The launcher is chosen per
  platform: `open -R` (macOS), `explorer /select,` (Windows), `xdg-open <dir>` (Linux). It runs
  without a shell (argument list), and `subprocess` is patched in tests.
- **Alternatives**: no reveal at all (a regression for local users).

## R6. Export problem reports

- **Decision**: `GET /api/incidents/export` streams a zip with the same layout as the Mac app's
  `ReportExport.entries`: `incidents/<id>.json` plus attachments, `system.txt`, `engine.log` (the
  last 500 ring lines, redacted) and `summary.md`. The download name is
  `agentline-report-YYYY-MM-DD-HHMM.zip`. Built in memory with `zipfile`.
- **Rationale**: one report format for both UIs, so triage readers don't care where it came from.

## R7. Unclean-exit detection

- **Decision**: new module `src/agentline/runmarker.py`. `serve` writes
  `<home>/engine.run.json` `{pid, started_at, version, platform, address}` after taking the lock,
  and removes it in `finally` after a normal stop. At start, a marker that is still there (and
  we hold the lock, so its process is gone) becomes one incident of the new kind
  `engine_unclean_exit`, then is replaced. It's skipped when `AGENTLINE_PARENT_PID` is set (the
  Mac app has its own `RunMarker` and `app_unclean_exit`, FR-016).
- **Banner**: the web reads `GET /api/incidents`, and shows the banner for the newest
  `engine_unclean_exit` (or `app_*`, only outside the Mac app) with `status: "new"`. View report
  and Dismiss both call `POST /incidents/{id}/seen`. The App reports page already lists every
  incident (`triage.triage_incidents`).

## R8. One engine per data folder

- **Decision**: new `src/agentline/enginelock.py`. It holds an exclusive non-blocking lock on
  `<home>/engine.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), writing
  `{pid, address}` into it. Held → the error names the address and pid, and the process exits 1.
  When `AGENTLINE_PARENT_PID` is set it retries for up to 10 s, because the Mac app's restart can
  start the new engine while the old one is still exiting. A filesystem without locks
  (`OSError` other than "would block") logs a warning and goes on, as the spec's edge case says.
- **Rationale**: the OS releases a held lock when the process dies, so a stale file never
  blocks. The Mac app's own `app.lock` is a separate file, so the two don't clash.

## R9. Notifications in the browser

- **Planner**: `web/src/lib/notify.ts` ports `NotificationPlanner` exactly (clarification 2):
  each gate once (its execution id), each run once it becomes done / dead / needs-review after a
  baseline poll, and incidents not seen before (by id + count).
- **One tab speaks**: Web Locks (`navigator.locks.request("agentline-notify", …)`, held for the
  tab's life) elects a leader with automatic failover. With no Web Locks support, every tab
  notifies.
- **Buttons**: `registration.showNotification` with `actions: [Approve, Reject]` through a tiny
  service worker `web/public/sw.js`. It doesn't call the API itself (it has no token). On
  `notificationclick` it focuses a Studio client and `postMessage`s
  `{type, run, node, execution}`, and that page approves with the comment "approved from a
  notification" or opens the run with the reject dialog. Without service worker notifications it
  falls back to `new Notification(...)` with `onclick` focus.
- **Off in the Mac app** (`isMacApp()`), where the native `Notifier` already does this.
- **Polling**: the existing 10 s top-bar tick becomes 5 s and also fetches runs (`api.runs("",
  50)`) and incidents, only in the leader tab.

## R10. Badges

- The title is `(N) Agentline Studio` when N > 0. The tab icon is an inline SVG favicon redrawn
  with a dot and the count (`lib/badge.ts`). `navigator.setAppBadge(N)` / `clearAppBadge()` run
  when available (installed app). Off in the Mac app (the Dock badge is native).

## R11. Installable app

- `web/public/manifest.webmanifest` (name, short_name, `display: standalone`, theme colours
  from `theme.css` values, an SVG icon) and `<link rel="manifest">`. The service worker from R9
  makes it installable in Chromium. `<link rel="icon">` becomes the SVG mark.

## R12. Sign-in

- **Decision**: `api.ts` stops calling `window.prompt`. On a 401 it clears the stored token,
  fires `AUTH_NEEDED_EVENT` and throws. `App` renders `<SignIn>` (full-page, Flight Deck style)
  while auth is needed. SignIn calls the new unauthenticated `GET /api/auth/check` with the typed
  token (trimmed) in the header, and stores it only when that returns 200. Sign out (Settings →
  General) removes it and fires the event.
- `GET /api/auth/check` answers `{token_required, ok}` and never reveals the token. It's a
  separate route without the dependency, so a wrong token gets a 200 with `ok: false` instead of
  a loop of 401s.

## R13. Command palette and shortcuts

- `web/src/lib/commands.ts`: the command list (id, title, keywords, shortcut, run), a `filter()`
  that matches words and initials, and `gShortcut()` sequence handling (`g` then a letter within
  1.5 s), all pure and tested. `web/src/components/CommandPalette.tsx`: a `role="dialog"` with a
  `role="listbox"`. Cmd/Ctrl+K toggles it. Ignored while focus is in an input, textarea, select,
  contenteditable or the code editor, or a `[role=dialog]`/`dialog[open]` is open (palette
  excepted).
- Mac menu actions → commands: Start Run… (`n`), Pipelines (`g p`), Runs (`g r`), Approvals
  (`g a`), Problems (`g x`), Settings (`g s`), Refresh, Restart Engine, Show Logs (`g l`), plus
  Lessons, Costs, App reports and Toggle Chat (⌘/Ctrl+J). Refresh fires `REFRESH_EVENT`, which
  App and the pages already listening to settings changes reload on.

## R14. Engine-down banner

- `web/src/lib/engine.ts`: `useEngineHealth()` polls `GET /healthz` (open, never logged on
  success) every 3 s, and treats a network error or 5xx as down after two misses (so it fires in
  6–9 s, under FR-001's 10 s). On recovery it fires `REFRESH_EVENT`. `restartEngine()` uses the
  native call in the Mac app and the API otherwise, then marks "restarting" until `/healthz`
  answers. Hidden in the Mac app (it has `EngineBanner`).

## R15. Chat parity

- Existing `engine` action: its ops grow from `restart | choose_folder` to
  `restart | choose_folder | reveal | export_reports`. In a browser, `choose_folder` takes a
  `path` (the card has a path field when missing), `reveal` takes `target: data | pipelines |
  config` (local only) and `export_reports` downloads the zip. In the Mac app the native calls
  are unchanged.
- Dismissing the crash banner = mark the report seen: a new `incident` op `seen`.
- No Chat action, with the reason in the parity table: sign out (it removes the browser's own
  credential, and a Chat that does it would sign the user out mid-conversation) and
  notification permission (browsers allow the permission request only from a click).

## R16. Windows

- The engine imports `fcntl` (`incidents.py`, `issues.py`, `chatlink/state.py`), so `agentline
  serve` doesn't start on Windows today. That's out of this feature's scope: the browser can be
  on Windows while the engine runs on macOS, Linux or WSL. The new modules (lock, reveal) are
  written to work on Windows anyway, so a later port has less to do. This is recorded in the docs
  and the report to the user.
