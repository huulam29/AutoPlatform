# Phase 12: running agentline as a Mac app (engine side)

The native macOS app (`macos/`, see `macos/README.md` and `docs/phase-12-mac.md`) runs the
engine as a private child process. This is what the engine adds for it.

## API for the app (under `/api`, bearer token required)
| Endpoint | Purpose |
|---|---|
| `GET /api/approvals` | every gate waiting for a human, across runs (drives notifications) |
| `POST /api/runs/{id}/approve` / `reject` `{node?, comment?}` | decide a gate; audited with `via: "app"` and the server's `AGENTLINE_USER` |
| `POST /api/runs` `{pipeline, payload, dedupe_key?}` | start a run (e.g. paste a ticket key) |
| `GET /api/status` | version, registered pipelines, Jira polling status, `keys_managed` |

Errors: 404 unknown run or pipeline, 409 no gate waiting (or several without `node`), 403 not
an approver. Approvals from the app sit beside `agentline approve` (decision 29).

## Jira polling (no webhooks needed)
A laptop isn't reachable by Jira webhooks, so a pipeline can poll instead:

```yaml
trigger:
  type: poll
  source: jira
  event: issue_assigned
  every: 5m                      # at least 30s
  filter: { project: PROJ }      # or { jql: "..." } to replace the query entirely
```

- Needs `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (the Mac app passes them from the
  Keychain).
- Starts a run for each ticket **assigned to the token's account after polling was first
  enabled** for that pipeline (the start time is kept in `$AGENTLINE_HOME/state/jira_poll.json`),
  so enabling it doesn't start your whole backlog. A reassignment back to you is a new run.
- The dedupe key is the webhook's: `jira:<key>:assigned:<accountId>:<changelog id>`, so
  webhooks and polling together never double-start a ticket.
- Default query: `assignee = currentUser() AND assignee CHANGED TO currentUser() AFTER
  "<start - 1 day>"`; the exact cut-off is checked against the changelog timestamp.
- Uses Jira Cloud's `POST /rest/api/3/search/jql`, falling back to `GET /rest/api/3/search`.
  Not yet run against a real Jira.
- Status (last poll, last error, runs started) is in `/api/status` and the Mac app.

## Keys managed by the app
With `AGENTLINE_KEYS_MANAGED=keychain` (set by the app), `PUT/DELETE /api/secrets/*` return 409:
keys live in the macOS Keychain and reach the engine as env vars at launch. The web editor's
Settings → Keys shows that message.

## Bundling
`macos/scripts/build_backend.sh` builds `macos/build/backend/`: a relocatable CPython
(python-build-standalone via uv, about 81 MB with dependencies) with `agentline[server]`
installed, plus the built editor. `AGENTLINE_WEB_DIST` points the server at it. The script
resolves uv's symlinked install dir and copies real files, so uv's shared interpreter is never
modified.

## The engine never outlives the app
The app passes `AGENTLINE_PARENT_PID`; `agentline worker`/`serve` check every second that the
process is still there and stop (worker, poller, schedules and the HTTP server) when it isn't.
So a crash, Force Quit or `kill -9` of the app doesn't leave an engine running on its own.
Verified on the built app: after `kill -9` of the app the engine exited within 2 s. A normal
Quit stops it directly (SIGTERM, then SIGKILL after 5 s).
