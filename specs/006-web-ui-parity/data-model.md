# Data Model: Web UI Parity With the Mac App

No ledger tables change. Everything new is a small file in the data folder (`$AGENTLINE_HOME`,
default `~/.agentline`), or browser state.

## Start marker: `<home>/engine.run.json`

| Field | Type | Notes |
|---|---|---|
| pid | int | the engine process |
| started_at | float | Unix seconds |
| version | str | `agentline.__version__` |
| platform | str | `incidents.platform_text()` |
| address | str | `http://host:port` |

Lifecycle: written after the lock is taken → removed on a normal stop (signal, stop event,
restart) → still there at the next start = unclean exit → one incident, then overwritten. Never
written when `AGENTLINE_PARENT_PID` is set. Written atomically (temp file + `os.replace`).

## Data folder lock: `<home>/engine.lock`

Holds `{"pid": int, "address": str}` (JSON) while an exclusive OS lock is held on it. The OS
releases the lock when the process exits, so the file's content is only for the error message.
The file isn't deleted on exit (deleting a locked file races with the next start).

## Incident kind `engine_unclean_exit`

Added to `incidents.KINDS`. Title "agentline engine quit unexpectedly". `context`:
`{previous_pid, started_at, version, platform, address}`. It's redacted like every incident, and
its source is `engine`.

## App info (API response, not stored)

`version, data_folder, config_path, pipelines_dir, pipelines_dir_from_cli (bool), address,
mac_app (bool), local (bool), token_required (bool), can_restart (bool), can_reveal (bool)`.

## Browser state

| Key / place | What | Lifetime |
|---|---|---|
| `localStorage["agentline.token"]` | the API token | until sign out or a 401 |
| `localStorage["agentline.notify"]` | `"on"` once the person turned notifications on in Settings | per browser |
| Web Lock `agentline-notify` | which tab notifies | while that tab is open |
| `NotificationPlanner` state (memory) | notified keys, last run statuses, baseline flag, incidents seen | per leader tab |

## Notification event

`{kind: "gate" | "run" | "incident", key, title, body, route, run?, node?, execution?}`.
The `key` is the Mac app's `dedupeKey` (`gate:<execution>`, `run:<id>:<status>`,
`incident:<id>:<count>`).

## Command

`{id, title, keywords[], shortcut?: string, section: "Go" | "Run" | "Engine" | "View",
run(): void}`.
