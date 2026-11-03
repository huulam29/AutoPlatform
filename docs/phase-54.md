# Phase 54: web UI parity with the Mac app

Spec: [specs/006-web-ui-parity](../specs/006-web-ui-parity/spec.md) (plan, research, tasks next
to it). Agentline Studio was already the Mac app's only window (phase 20) and `agentline serve`
already served it to any browser. This phase gives a plain browser the twelve things the Mac app
added around the page. The Mac app's source (`macos/`) is unchanged: each browser path sits next
to the existing `isMacApp()` native path.

## What a browser has now

| # | Mac app | Browser |
|---|---|---|
| 1 | Engine banner, Restart Engine | `components/EngineBanner.tsx`: "isn't reachable" after 2 missed `/healthz` probes 3 s apart (6–9 s), "Restarting…"; `POST /api/engine/restart` → `serve` restarts in place with `os.execv`, same arguments and address (local or token only; 409 in the Mac app, during a restart, or without `serve`) |
| 2 | App info (native `app.info`) | `GET /api/app/info` (version, data folder, config, pipelines folder, address, `mac_app`, `local`, `can_restart`, `can_reveal`) |
| 3 | Choose… pipelines folder | Path field + `FolderPicker.tsx` (`GET /api/app/folders`, folders only, no dot-folders); `PUT /api/app/pipelines-dir` writes `pipelines_dir` to agentline.toml (`tomlwrite.set_top_level`) and points the running engine at it (`PipelineFiles.set_dir` → `LivePipelines.set_dir`); an empty folder gets the ticket-to-eod template. A `--pipelines` flag still wins after a restart (the reply says so) |
| 4 | Show in Finder | `CopyPath.tsx`: Copy everywhere; Show in folder only when local (loopback, no proxy headers): `POST /api/app/reveal` → `reveal.py` (`open -R` / `explorer /select,` / `xdg-open`, no shell, only inside the data, pipelines and settings folders) |
| 5 | Logs "app and engine output" | "engine log" + data folder Copy path |
| 6 | Export report… | `GET /api/incidents/export`: the same zip layout as `ReportExport.entries` (`incidents/`, `system.txt`, `engine.log` redacted, `summary.md`) |
| 7 | Crash / unclean-exit banner | `runmarker.py`: `<home>/engine.run.json` written after the lock, removed on a normal stop; a leftover becomes one `engine_unclean_exit` incident; the banner shows the newest unseen one (View report / Dismiss mark it seen). Skipped inside the Mac app (`AGENTLINE_PARENT_PID`). Scanning macOS crash logs stays Mac-only |
| 8 | Notifications with Approve / Reject / Open | `lib/notify.ts` ports `NotificationPlanner` (gates once; runs that *become* done / dead / needs-review after a baseline; new reports, at most one per report per 10 min and one a minute). One tab notifies (Web Lock `agentline-notify`). `public/sw.js` routes clicks back to a page: Approve (comment "approved from a notification"; 404/409 → "Already decided"), Reject… opens `#/approvals?reject=<execution>` with that gate's comment focused, Open goes to the run / App reports. Turned on in Settings → General (permission asked only on the click) |
| 9 | Dock badge, menu bar extra | `lib/badge.ts`: "(N) Agentline Studio", a count dot on the tab icon, `navigator.setAppBadge` when installed; `public/manifest.webmanifest` + `icon.svg` make Studio installable |
| 10 | Menu shortcuts (⌘N, ⌘1–4, ⌘R, …) | `CommandPalette.tsx` (⌘/Ctrl+K) with every menu action; `g p/r/a/x/s/l` and `n` outside fields and dialogs (`lib/commands.ts`). The browser owns ⌘N/⌘R/⌘1–4 |
| 11 | Token passed by the app | `SignIn.tsx` instead of `window.prompt`: checks with `GET /api/auth/check` (outside the token check; returns only booleans) before storing; a 401 forgets the token and shows sign-in; Sign out in Settings → General |
| 12 | One app per data folder (`InstanceLock`) | `enginelock.py`: `serve` holds `<home>/engine.lock` (`flock` / `msvcrt`); a second engine exits 1 naming the first's address and pid; the Mac app's engine waits up to 10 s for its old engine |

Chat parity: `engine` ops are now `restart | choose_folder | reveal | export_reports`
(`choose_folder` takes a `path` in a browser, or the card asks for one; `reveal` a `target`), and
`incident` has op `seen`. Sign out and the notification permission have no Chat action (see
docs/phase-25.md).

## Checked

- pytest in WSL: 1027 passed (992 before), new tests in `tests/api/test_app_routes.py`,
  `tests/test_enginelock.py`, `tests/test_reveal_and_marker.py`, `tests/cli/test_serve_parity.py`,
  `tests/ai/test_assistant_parity.py`. Web: 229 vitest tests (202 before), `tsc`, build.
- By hand in the built-in browser against `serve` in WSL on a throwaway `AGENTLINE_HOME`:
  Settings → General (info, Copy, Show in folder shown when local), the folder picker changing
  the pipelines folder live, the palette (`appr` → Approvals) and `g r`, Restart Engine (same
  pid and address, banner cleared), `kill -9` (banner after 6 s, then the quit-unexpectedly banner
  and App report after the restart, View report marked it seen), Export (zip layout), a second
  `serve` refused, sign-in with a wrong and the right (fake) token, Sign out.
- Not checked by hand: real system notifications and their buttons (the built-in browser blocks
  notifications), the installed-app badge, a real file manager opening (WSL has none), Safari and
  Firefox. The notification code is tested with fakes only. `swift test` wasn't run (no Mac; the
  Mac app's source is unchanged).

## Notes

- The engine itself still doesn't start on native Windows (`incidents.py`, `issues.py` and
  `chatlink/state.py` import `fcntl`). A browser on Windows works against an engine on macOS,
  Linux or WSL. The new modules (`enginelock.py`, `reveal.py`) already handle Windows.
- After a live folder change, `files.config.path` can be relative; the app routes resolve it.
