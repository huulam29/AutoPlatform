# API Contract: Web UI Parity

All routes are under `/api`. "Token rule" means the existing bearer token dependency: open when
the server has no token, otherwise `Authorization: Bearer <token>` is required.

## GET /api/auth/check (no token dependency)

Request: optional `Authorization: Bearer <candidate>`.
Response 200: `{"token_required": bool, "ok": bool}`. `ok` is true when no token is required
or the candidate matches (constant-time compare). The token is never returned.

## GET /api/app/info (token rule)

Response 200:
```json
{
  "version": "0.x",
  "data_folder": "/home/u/.agentline",
  "config_path": "/home/u/.agentline/agentline.toml",
  "pipelines_dir": "/home/u/pipelines",
  "pipelines_dir_from_cli": false,
  "address": "http://127.0.0.1:8080",
  "mac_app": false,
  "local": true,
  "token_required": false,
  "can_restart": true,
  "can_reveal": true
}
```
`can_restart` = not `mac_app` and (`local` or a token was given). `can_reveal` = `local`.

## POST /api/engine/restart (token rule, plus local-or-token)

- 202 `{"restarting": true}`: sent before the restart starts.
- 403: remote with no token configured (it can't happen behind the token rule, but it's checked).
- 409 `{"detail": "a restart is already under way"}` or `"the Mac app restarts its engine"`.
- Logs `engine restart asked by <actor> from <client>` at WARNING.

## GET /api/app/folders?path= (token rule)

Lists the sub-folders of `path` (default: the user's home).
Response 200: `{"path": abs, "parent": abs|null, "home": abs, "folders": [{"name", "path",
"readable"}]}`, sorted by name, with dot-folders skipped.
400 when `path` isn't an existing folder.

## PUT /api/app/pipelines-dir (token rule)

Body `{"path": str}` (`~` is expanded and the path made absolute).
- 200 `{"pipelines_dir": abs, "seeded": bool, "note": str|null}`: saved to `agentline.toml`
  and live in the running engine.
- 400 when it isn't an existing folder (with the reason).

## POST /api/app/reveal (token rule, plus local only)

Body `{"path": str}` or `{"target": "data" | "pipelines" | "config" | "incidents"}`.
- 200 `{"revealed": abs}`.
- 403 when the request isn't local.
- 400 when the path is outside the allowed folders or doesn't exist.

## GET /api/incidents/export (token rule)

200 `application/zip`, with `Content-Disposition: attachment;
filename="agentline-report-YYYY-MM-DD-HHMM.zip"`. Entries: `incidents/<id>.json`,
`incidents/<attachment>`, `system.txt`, `engine.log`, `summary.md`.

## POST /api/incidents/{id}/seen

Existing route, unchanged; used by the crash banner.

## Service worker messages (page ⇄ `sw.js`)

- Page → SW: `registration.showNotification(title, {body, tag: key, data: {route, run, node,
  execution, kind}, actions?})`.
- SW → page: `{"type": "agentline.notification", "action": "approve" | "reject" | "open",
  "data": {...}}`, sent to the focused (or first) Studio client after `client.focus()`. When no
  client is open it calls `clients.openWindow("/#" + route)`.

## Chat actions

- `{"type": "engine", "op": "restart" | "choose_folder" | "reveal" | "export_reports",
  "path"?: str, "target"?: "data" | "pipelines" | "config"}`
- `{"type": "incident", "op": "seen", "id": str}` (alongside `delete` and `clear`).

## CLI

`agentline serve` exits 1 with `serve: another engine is running on this data folder (<address>,
pid <pid>)` when the lock is held.
