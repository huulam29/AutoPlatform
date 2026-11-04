# Implementation Plan: Web UI Parity With the Mac App

**Branch**: `claude/web-ui-macos-parity-d2045f` (merged as `phase-54`) | **Date**: 2026-11-02 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/006-web-ui-parity/spec.md`

## Summary

Studio already runs in any browser. This plan adds the twelve things the Mac app supplies around
it. On the engine side: app info, restart in place, a folder browser, setting the pipelines folder
live, reveal, a report zip, a start marker for unclean exits, and a data folder lock. On the web
side: an engine-down banner, a sign-in screen, Settings → General without the Mac-only gates,
notifications with a service worker, badges, a manifest, and a command palette. Each browser path
sits next to the existing `isMacApp()` native path, which stays as it is.

## Technical Context

**Language/Version**: Python 3.11+ (engine), TypeScript + React 18 (web)

**Primary Dependencies**: FastAPI/uvicorn (`server` extra, already used); no new dependencies.
The web uses browser APIs only (Notifications, Service Worker, Web Locks, Badging).

**Storage**: two small JSON files in the data folder (`engine.run.json`, `engine.lock`) and
`localStorage` in the browser. No ledger change.

**Testing**: pytest with FastAPI `TestClient`, a patched `subprocess` for reveal, a patched
`os.execv` for restart; vitest for the pure web libraries (planner, commands, badge, auth).

**Target Platform**: engine on macOS/Linux (Windows: research R16); browsers Chrome/Edge,
Safari, Firefox (desktop).

**Project Type**: web service + web app (one repo: `src/agentline`, `web/`)

**Performance Goals**: engine-down banner ≤ 10 s; notifications within one 5 s poll.

**Constraints**: the Mac app is unchanged; no new Python dependencies; web styling from
`theme.css` tokens only; access logs stay quiet for GETs.

**Scale/Scope**: about 8 new engine routes, 3 new engine modules, about 6 new web files, and edits
to about 8 web files.

## Constitution Check

| Principle | Check | Result |
|---|---|---|
| I. Durable by default | A restart sets the same stop event a signal does: the worker finishes or releases its claims as on Ctrl+C. Nothing in scheduling or the ledger changes, so the chaos runs are unaffected. The lock stops two engines sharing one ledger. | Pass |
| II. People decide | Approve from a notification goes through the existing `/runs/{id}/approve` with the audit row; Reject never rejects without a confirm. | Pass |
| III. Secrets | The token is never returned (`/auth/check` returns a bool) and stays in the browser only; the export zip and the incident are redacted; reveal takes no secrets. | Pass |
| IV. One generic engine | No per-node code; the engine core stays stdlib (`runmarker.py` and `enginelock.py` are stdlib; routes live in `api/`, the `server` extra). | Pass |
| V. Least privilege | Reveal runs only for local requests, only inside allowed folders, with no shell; the folder browser lists names only. | Pass |
| VI. Tested | Each route, the lock, the marker and each web library get tests; triage/review stay off in tests. | Pass |
| VII. Honest scope | Notifications, the service worker and the file manager are checked by hand only; that is stated in `docs/phase-54.md`. | Pass |
| Workflow: Chat parity | `engine` ops extended, `incident seen` added; two actions without Chat with reasons (R15). | Pass |
| Workflow: web is the only UI | Everything is in `web/`; the Mac app only keeps its native bridge. | Pass |

Re-checked after Phase 1 design: no change.

## Project Structure

### Documentation (this feature)

```text
specs/006-web-ui-parity/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/api.md
├── checklists/requirements.md
└── tasks.md            (/speckit-tasks)
```

### Source Code

```text
src/agentline/
├── enginelock.py            NEW  data folder lock (fcntl / msvcrt)
├── runmarker.py             NEW  start marker + unclean-exit incident
├── reveal.py                NEW  file-manager launcher, allowed-folder check
├── incidents.py             +engine_unclean_exit kind
├── live.py                  +LivePipelines.set_dir
├── api/editor.py            +PipelineFiles.set_dir
├── api/app.py               NEW  /auth/check, /app/info, /engine/restart, /app/folders,
│                                 /app/pipelines-dir, /app/reveal
├── api/incidents.py         +/incidents/export (zip)
├── api/server.py            include the app router, is_local_request
├── assistant.py             engine ops reveal/export_reports + path; incident op seen
└── cli/commands.py          serve: lock, marker, restart via execv

web/
├── index.html               manifest + SVG icon links
├── public/manifest.webmanifest, public/icon.svg, public/sw.js     NEW
└── src/
    ├── api.ts               no window.prompt; AUTH_NEEDED_EVENT; new calls
    ├── App.tsx              banners, SignIn gate, palette, shortcuts, badges, notifier
    ├── lib/engine.ts        NEW  health, restart, REFRESH_EVENT
    ├── lib/notify.ts        NEW  planner + leader + service worker glue
    ├── lib/badge.ts         NEW  title / favicon / app badge
    ├── lib/commands.ts      NEW  palette commands, filter, g-sequences
    ├── lib/assistant.ts     engine ops in the browser, incident seen
    ├── components/SignIn.tsx, CommandPalette.tsx, EngineBanner.tsx, FolderPicker.tsx   NEW
    ├── components/SettingsExtra.tsx   General: info for both, folder, reveal/copy, notifications, sign out
    ├── components/AppReports.tsx      Export for both, scan Mac only, reveal local
    └── components/Logs.tsx            wording, copy path

tests/
├── api/test_app_routes.py   NEW
├── test_enginelock.py, test_runmarker.py, test_reveal.py   NEW
├── api/test_incidents_export.py   NEW
└── ai/ (assistant engine/incident ops)
web/src/lib/*.test.ts        notify, commands, badge, engine
```

**Structure Decision**: the existing layout. Engine helpers go next to `incidents.py`, routes go
in `api/`, and the web follows `lib/` (pure, tested) plus `components/`.

## Complexity Tracking

No violations. One judgement call: the restart uses `os.execv` in place rather than a supervisor
process. It's simpler, and a supervisor would be a second long-running process to manage.
