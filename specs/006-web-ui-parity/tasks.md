---

description: "Task list for Web UI Parity With the Mac App"
---

# Tasks: Web UI Parity With the Mac App

**Input**: Design documents from `specs/006-web-ui-parity/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, quickstart.md

**Tests**: Required by the constitution (Principle VI): every behaviour change has automated
tests, using fakes (patched `subprocess`, patched `os.execv`, fake browser APIs).

**Organization**: one phase per user story, in priority order (P1, P2, P3).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [ ] T001 Run the baseline test suites (`uv run pytest -m "not slow" -q` in WSL Ubuntu on a clone; `npm test` and `npx tsc --noEmit` in `web/`) and record the pass counts in specs/006-web-ui-parity/tasks.md Notes

---

## Phase 2: Foundational (blocks the user stories)

- [ ] T002 Add `is_local_request(request)` to src/agentline/api/server.py: true when `request.client.host` is loopback (`is_local`) and none of `Forwarded`, `X-Forwarded-For`, `X-Real-IP` is present (research R2)
- [ ] T003 Create src/agentline/api/app.py with `create_app_router(files, app, server_state)` returning an APIRouter, and include it in src/agentline/api/server.py with the token dependency; add an unauthenticated sub-router for `/auth/check`
- [ ] T004 Implement `GET /api/app/info` in src/agentline/api/app.py returning `version, data_folder, config_path, pipelines_dir, pipelines_dir_from_cli, address, mac_app, local, token_required, can_restart, can_reveal` (contracts/api.md); `mac_app` = `AGENTLINE_PARENT_PID` set; `address` from `server.state.address` (set by `cmd_serve`) else null
- [ ] T005 [P] Add `api.appInfo()` and the `AppInfoApi` type to web/src/api.ts
- [ ] T006 [P] Create web/src/lib/engine.ts with `REFRESH_EVENT`, `useEngineHealth()` (polls `/healthz` every 3 s, down after 2 misses, fires `REFRESH_EVENT` on recovery) and `restartEngine()` (native `engine.restart` in the Mac app, else `POST /api/engine/restart`, then a "restarting" state until `/healthz` answers)
- [ ] T007 [P] Create web/src/lib/clipboard.ts `copyText(text)` with a `navigator.clipboard` fallback to a hidden textarea, and a `<CopyPath path>` component in web/src/components/CopyPath.tsx (Copy button, plus "Show in folder" when `can_reveal`, calling `api.reveal`)
- [ ] T008 Tests for T002–T004 in tests/api/test_app_routes.py: loopback without forward headers is local, with `X-Forwarded-For` it isn't, app info fields and types, `mac_app` with `AGENTLINE_PARENT_PID`

**Checkpoint**: app info answers in a browser; shared web helpers exist.

---

## Phase 3: User Story 1 - Engine down banner and restart (P1) 🎯 MVP

**Goal**: a browser notices a stopped engine and can restart a running one.

**Independent Test**: stop the engine with Studio open → banner within 10 s; start → it clears. Restart from Settings → back at the same address.

- [ ] T009 [US1] Implement `POST /api/engine/restart` in src/agentline/api/app.py: 409 in the Mac app, while restarting, or when `server.state` has no stop event ("restart needs agentline serve", e.g. the dev server), 403 when neither local nor token-holding, else set `server.state.restart = True` and the stop event after the reply (background task), log `engine restart asked by <actor> from <client>` at WARNING
- [ ] T010 [US1] In src/agentline/cli/commands.py `cmd_serve`: put the stop event and `restart` flag on `asgi.state`, set `asgi.state.address`; after `asyncio.run(main())` returns with the flag set, `os.execv(sys.executable, [sys.executable, "-m", "agentline.cli", *sys.argv[1:]])`
- [ ] T011 [P] [US1] Tests in tests/api/test_app_routes.py for restart: 202 then flag and stop set; 409 twice; 409 with `AGENTLINE_PARENT_PID`; and tests/cli/test_serve_restart.py checking `cmd_serve` calls a patched `os.execv` with the same arguments when the flag is set
- [ ] T012 [P] [US1] Create web/src/components/EngineBanner.tsx (not shown in the Mac app): "The engine isn't reachable. Start it with `agentline serve`. Retrying…" or "Restarting the engine…", using `<Status>` tones and theme tokens
- [ ] T013 [US1] Mount `<EngineBanner>` in web/src/App.tsx under the header, and make App reload node types, pipelines and counts on `REFRESH_EVENT`
- [ ] T014 [US1] Settings → General in web/src/components/SettingsExtra.tsx: show Restart engine when `isMacApp()` or `info.can_restart`, through `restartEngine()`
- [ ] T015 [P] [US1] Vitest web/src/lib/engine.test.ts: down after 2 misses, not after 1, recovery fires `REFRESH_EVENT`, `restartEngine` uses `nativeCall` when the bridge exists

**Checkpoint**: US1 works in a browser; the Mac app still uses its native restart.

---

## Phase 4: User Story 2 - Token sign-in (P1)

**Goal**: a Studio-style sign-in replaces `window.prompt`.

**Independent Test**: engine with a token → sign-in screen; a wrong token is refused, the right one opens Studio; Sign out returns to sign-in.

- [ ] T016 [US2] Implement `GET /api/auth/check` in src/agentline/api/app.py (no token dependency): `{token_required, ok}` with `hmac.compare_digest`; never echoes the token
- [ ] T017 [P] [US2] Tests in tests/api/test_app_routes.py: no token required → ok; right/wrong/absent candidate; response never contains the token
- [ ] T018 [US2] In web/src/api.ts: remove `window.prompt`; on 401 clear the token, dispatch `AUTH_NEEDED_EVENT`, throw `ApiError`; add `checkToken(candidate)`, `clearToken()`, `hasToken()`; trim the stored token
- [ ] T019 [P] [US2] Create web/src/components/SignIn.tsx: full-page card in the Flight Deck style, password field, explains "AGENTLINE_API_TOKEN in the engine's secrets file (~/.agentline/secrets.env on that machine)", refusal message, stores only after `ok`
- [ ] T020 [US2] In web/src/App.tsx: show `<SignIn>` while auth is needed (listen to `AUTH_NEEDED_EVENT`), reload everything after sign-in
- [ ] T021 [US2] Sign out button in Settings → General (web/src/components/SettingsExtra.tsx) when `hasToken()`
- [ ] T022 [P] [US2] Vitest web/src/api.test.ts: a 401 dispatches `AUTH_NEEDED_EVENT`, clears the token, never calls `window.prompt`; `checkToken` stores only on ok

---

## Phase 5: User Story 3 - App info and folders (P1)

**Goal**: Settings → General shows the info, sets the pipelines folder live, copies and reveals paths.

**Independent Test**: change the pipelines folder through the browser → the Pipelines page lists the new folder without a restart.

- [ ] T023 [US3] Implement `GET /api/app/folders?path=` in src/agentline/api/app.py: default home, sub-folders only, skip dot-names, `readable` via `os.access`, `parent` null at the root, 400 for a non-folder
- [ ] T024 [US3] Add `PipelineFiles.set_dir(path)` in src/agentline/api/editor.py and `LivePipelines.set_dir(path)` in src/agentline/live.py (sets `self.dir`, reloads)
- [ ] T025 [US3] Implement `PUT /api/app/pipelines-dir` in src/agentline/api/app.py: expand and resolve the path, 400 unless it is an existing folder, write `pipelines_dir` with `tomlwrite` to the config file, call both `set_dir`s, seed an empty folder with the `ticket-to-eod` template, `note` when started with `--pipelines` (`server.state.pipelines_from_cli`)
- [ ] T026 [P] [US3] Create src/agentline/reveal.py: `allowed(path, roots) -> bool` (resolved path inside one root), `reveal(path)` using `open -R` / `explorer /select,` / `xdg-open <dir>` as an argument list, no shell
- [ ] T027 [US3] Implement `POST /api/app/reveal` in src/agentline/api/app.py: 403 unless `is_local_request`, `target` maps to data/pipelines/config/incidents, 400 outside the allowed folders or missing
- [ ] T028 [P] [US3] Tests tests/api/test_app_folders.py: folder listing (dirs only, no dot-dirs, unreadable flagged), pipelines-dir success makes `/api/pipelines` list the new folder's pipelines, 400 for a file / missing path, toml written, the `--pipelines` note; tests/test_reveal.py: `allowed` rejects `..` escapes and symlinks out, `reveal` builds the right argument list per platform (patched `subprocess.Popen` and `sys.platform`), the route refuses a forwarded request
- [ ] T029 [P] [US3] Create web/src/components/FolderPicker.tsx: a dialog listing `api.folders(path)` with Up, Home, a typed path field and Choose; unreadable folders disabled
- [ ] T030 [US3] Rewrite `GeneralSection` in web/src/components/SettingsExtra.tsx: in a browser, use `api.appInfo()` for Version, Address, Data folder, Config and Pipelines folder with `<CopyPath>`; a pipelines folder path field plus Browse… (FolderPicker) saving through `api.setPipelinesDir`, with the note shown; in the Mac app keep the native `app.info`, Choose… and Show in Finder exactly as today
- [ ] T031 [P] [US3] Add `api.folders`, `api.setPipelinesDir`, `api.reveal` to web/src/api.ts

---

## Phase 6: User Story 4 - Notifications and badges (P2)

**Goal**: gate/run/report notifications, Approve from the notification, badges, installable.

**Independent Test**: allow notifications, reach a gate, switch tab → one notification, title "(1) Agentline Studio", Approve moves the run on.

- [ ] T032 [P] [US4] Create web/src/lib/notify.ts `NotificationPlanner` porting macos/Sources/AgentlineCore/NotificationPlanner.swift: gates once by `gate:<execution>` (including those waiting at the first poll), runs only when they become `done | dead | needs-review` after a baseline, keys `run:<id>:<status>`; incidents by `incident:<id>:<count>` after a baseline, rate-limited to one per incident per 10 minutes and one per minute overall (the Mac caller rate-limits too); titles/bodies as the Swift `EngineEvent` (title "Approval needed: <pipeline>", body prompt or "Gate <node> is waiting.", "Run <status>: <flow>", error first 200 chars)
- [ ] T033 [P] [US4] Vitest web/src/lib/notify.test.ts mirroring macos/Tests NotificationPlanner tests: no duplicate gates, baseline suppresses old finished runs, a status change to done announces once, needs-review wording
- [ ] T034 [P] [US4] Create web/public/sw.js: on `notificationclick` close it, focus or open a Studio client, `postMessage({type: "agentline.notification", action, data})`; no API calls
- [ ] T035 [US4] In web/src/lib/notify.ts: `startNotifier()` (not in the Mac app, only when `localStorage["agentline.notify"] === "on"` and permission is granted): take the Web Lock `agentline-notify` (fallback: always leader), poll approvals, runs and incidents every 5 s, show via `registration.showNotification` with Approve/Reject actions for gates when a service worker is active, else `new Notification` with `onclick`; handle service worker messages: approve with `api.approve(run, node, "approved from a notification")`; on a 404/409 show "Already decided" (a notification) and open the run, on another error open the run with the error banner; reject opens `#/runs/<id>` with the reject dialog (`?reject=<node>`), open goes to the route
- [ ] T036 [P] [US4] Create web/src/lib/badge.ts: `setBadge(n)` sets `document.title` to `(n) Agentline Studio` or the plain title, redraws the SVG favicon with a count dot, calls `navigator.setAppBadge`/`clearAppBadge` when present; no-op in the Mac app; vitest web/src/lib/badge.test.ts for title and the zero case
- [ ] T037 [US4] In web/src/App.tsx: tick every 5 s, call `setBadge(counts.approvals)`, register `/sw.js` in a browser, start the notifier
- [ ] T038 [US4] Notifications control in Settings → General (web/src/components/SettingsExtra.tsx): states not supported / off / on / blocked (with how to unblock), "Turn on" asks `Notification.requestPermission()` only on click; hidden in the Mac app
- [ ] T039 [US4] Reject from a notification: in web/src/components/RunView.tsx open the existing gate reject UI with a comment field when the route carries `?reject=<node>`; nothing rejects until confirmed
- [ ] T040 [P] [US4] Create web/public/manifest.webmanifest (name "Agentline Studio", short_name "Agentline", `display: standalone`, `start_url: "/"`, colours taken from web/src/theme.css values) and web/public/icon.svg; link both from web/index.html

---

## Phase 7: User Story 5 - Report export and quit unexpectedly (P2)

**Goal**: Export in a browser; an unclean exit becomes a report and a banner.

**Independent Test**: `kill -9` the engine, start it → banner and App report; Export downloads the zip.

- [ ] T041 [P] [US5] Add `engine_unclean_exit` to `KINDS` in src/agentline/incidents.py
- [ ] T042 [P] [US5] Create src/agentline/runmarker.py: `write(home, address)` atomic (`engine.run.json`: "pid, started_at, version, platform, address"), `remove(home)`, `check_previous(home, store)` creating one redacted `engine_unclean_exit` incident titled "agentline engine quit unexpectedly" with context `{previous_pid, started_at, version, platform, address}`; skip all three when `AGENTLINE_PARENT_PID` is set
- [ ] T043 [US5] Wire the marker into `cmd_serve` in src/agentline/cli/commands.py: check then write after the lock (T049) and before serving, remove in `finally` on a normal stop (including restart)
- [ ] T044 [P] [US5] Tests tests/test_runmarker.py: clean cycle makes no incident, leftover marker makes exactly one, its context, redaction of a fake secret in the address, skipped with `AGENTLINE_PARENT_PID`
- [ ] T045 [US5] Implement `GET /api/incidents/export` in src/agentline/api/incidents.py: in-memory zip `incidents/<id>.json`, safe attachments, `system.txt`, `engine.log` (last 500 `RING` lines, redacted), `summary.md`; filename `agentline-report-YYYY-MM-DD-HHMM.zip`. Register it before `/incidents/{iid}`
- [ ] T046 [P] [US5] Tests tests/api/test_incidents_export.py: entries present, an attachment with `..` skipped, a fake secret in the log redacted, the header filename format
- [ ] T047 [US5] In web/src/components/AppReports.tsx: Export for both (native in the Mac app, `api.exportReports()` download in a browser); Scan only in the Mac app; the per-report Finder link becomes `<CopyPath>` in a browser
- [ ] T048 [US5] Crash banner in web/src/components/EngineBanner.tsx (browser only): newest new `engine_unclean_exit` incident → "agentline quit unexpectedly last time" with View report (`#/app-reports`, mark seen) and Dismiss (mark seen)

---

## Phase 8: User Story 7 - One engine per data folder (P2)

**Goal**: a second `serve` on the same data folder exits naming the first.

**Independent Test**: two `serve`s, one `AGENTLINE_HOME` → the second exits 1.

- [ ] T049 [P] [US7] Create src/agentline/enginelock.py `EngineLock(home).acquire(address, wait=0.0)` / `release()`: exclusive non-blocking `fcntl.flock` (POSIX) or `msvcrt.locking` (Windows) on `<home>/engine.lock`, writes `{"pid", "address"}`; raises `EngineLocked(pid, address)` when held; an unsupported filesystem logs a warning and returns; `wait` retries every 0.2 s
- [ ] T050 [US7] Take the lock in `cmd_serve` (src/agentline/cli/commands.py) before building the server; `wait=10` when `AGENTLINE_PARENT_PID` is set; on `EngineLocked` print `serve: another engine is running on this data folder (<address>, pid <pid>)` and return 1
- [ ] T051 [P] [US7] Tests tests/test_enginelock.py (POSIX, skipped on Windows): second acquire from a subprocess fails with pid/address, released after the holder exits, `wait` succeeds once released, two homes don't clash

---

## Phase 9: User Story 6 - Command palette and shortcuts (P3)

**Goal**: every Mac menu action from the keyboard.

**Independent Test**: Cmd/Ctrl+K, "appr", Enter → Approvals; `g r` → Runs.

- [ ] T052 [P] [US6] Create web/src/lib/commands.ts: `buildCommands(deps)` with Start Run… (`n`), Pipelines (`g p`), Runs (`g r`), Approvals (`g a`), Problems (`g x`), Lessons, Costs, App reports, Logs (`g l`), Settings (`g s`), Refresh, Restart Engine, Toggle Chat (⌘/Ctrl+J); `filterCommands(list, query)` (all words match title/keywords, initials rank first); `SequenceKeys` for `g`+letter within 1.5 s; `isTypingTarget(el)` (input, textarea, select, contenteditable, `.cm-editor`)
- [ ] T053 [P] [US6] Vitest web/src/lib/commands.test.ts: filter order, sequences time out, typing targets ignored
- [ ] T054 [US6] Create web/src/components/CommandPalette.tsx: `role="dialog"` + `aria-modal`, search input, `role="listbox"` with `aria-activedescendant`, arrows/Enter/Escape, shortcut hints, theme tokens only
- [ ] T055 [US6] In web/src/App.tsx: Cmd/Ctrl+K toggles the palette (not over another open dialog), `g`/`n` sequences outside typing targets and dialogs; a "Commands ⌘K" hint button in the header

---

## Phase 10: User Story 8 - Logs wording (P3)

- [ ] T056 [US8] In web/src/components/Logs.tsx: browser shows "engine log" and `<CopyPath>` of the data folder (Show in folder when local); the Mac app keeps "app and engine output" and Show data folder

---

## Phase 11: Polish & Cross-Cutting

- [ ] T057 Chat parity in src/agentline/assistant.py: `ENGINE_OPS` = restart, choose_folder, reveal, export_reports; `choose_folder` may carry `path`, `reveal` a `target`; update the ACTION_TYPES comment and GUIDE lines; `incident` op `seen` with a LIVE STATE id check
- [ ] T058 Chat parity in web/src/lib/assistant.ts `applyAction`/`actionTitle`: browser engine ops through `api` (restart, setPipelinesDir with the card's input, reveal, export download), native in the Mac app; `incident seen`; remove the "Mac app only" card note in web/src/components/Assistant.tsx for ops that now work in a browser
- [ ] T059 [P] Tests: tests/ai (assistant checks for the new ops and `incident seen`), web/src/lib/assistant.test.ts (applyAction in a browser uses the API)
- [ ] T060 [P] Update the stale Keychain comment in web/src/lib/native.ts and the docstring of src/agentline/api/server.py (new routes)
- [ ] T061 Write docs/phase-54.md (what was built, what is fake-tested only, Windows engine note, parity table rows) and add the rows to docs/phase-25.md's Chat parity table
- [ ] T062 Update CLAUDE.md (Phase 54 line) and ARCHITECTURE.md §12 (decisions: folder access = token rule; notifications = Mac rules; in-place restart; lock file)
- [ ] T063 Run `uv run ruff check . && uv run ruff format --check .`, pytest (WSL clone), `npm test`, `npx tsc --noEmit`, `npm run build`, and the hardcode lint if available; fix failures
- [ ] T065 Confirm the Mac app's source is untouched (`git diff --stat main -- macos/` is empty). No Mac app tests are run here: the Mac app and the web UI are separate sources, and the user decided the Windows machine needn't run them while that holds
- [ ] T064 Walk quickstart.md rows 1–11 in the built-in browser against a throwaway `AGENTLINE_HOME` in WSL; record what was checked by hand in docs/phase-54.md

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. US1–US3 (P1) first, then US4, US5, US7 (P2), then US6, US8 (P3), then Polish.
- T043 depends on T049/T050 (the marker is written after the lock). T010 and T050 and T043 all edit `cmd_serve`: do them in order T010 → T050 → T043.
- T030 depends on T023–T027 and T031. T035 depends on T032, T034. T047/T048 depend on T045 and T007.
- Polish T057–T059 depend on the routes from US1, US3, US5.

## Parallel Opportunities

- Phase 2: T005, T006, T007 together.
- US3: T026, T028, T029, T031 alongside the route work.
- US4: T032, T033, T034, T036, T040 together.
- US5: T041, T042, T044, T046 together. US7 T049, T051 together.
- US6 and US8 are independent of US4/US5.

## Implementation Strategy

MVP is US1 + US2 + US3 (a remote browser can sign in, sees engine state and info, manages
folders). Then US5 and US7 (safety: reports and the lock), then US4 (notifications), then US6 and
US8. Each phase is committed on its own with its tests.

## Notes

- Tests that need POSIX (`fcntl`) run in WSL Ubuntu on a clone (CLAUDE.md shells note).
