# Phase 13: crash and failure reports (engine side)

Problems are recorded **locally** so they can be fixed later; nothing is uploaded. The Mac app
writes its own reports (app crashes, unexpected engine exits) in the same format and folder;
see `docs/phase-13-mac.md`.

## Where and what
`$AGENTLINE_HOME/incidents/<id>.json` (the Mac app: `~/Library/Application Support/agentline/
incidents/`; the CLI: `~/.agentline/incidents/`), plus `<id>/` for attachments such as macOS crash
reports. One file per distinct problem: the same problem again (same fingerprint: source, kind,
exception type, message with ids and numbers normalized, innermost frames) updates `count`,
`last_seen`, the latest message/traceback/context, and sets `status` back to `new`. Every text
field goes through the redactor first. Writers take `flock` on `incidents/.lock`.

| kind | recorded when |
|---|---|
| `engine_exception` | an unhandled exception in `agentline worker`/`serve` (main thread, threads, asyncio) |
| `api_error` | an HTTP request fails with 500; the response carries the incident id |
| `worker_error` | a step raises a bug-type error (KeyError, TypeError, AttributeError, …), or the worker loop / lease reaper hits an error (they now keep running instead of crashing the worker) |
| `engine_exit`, `app_crash`, `app_exception`, `app_unclean_exit` | written by the Mac app |

Ordinary pipeline outcomes are **not** incidents: a step failing with a runner or API error, a
timeout, a dead or needs-review run. Those stay in the run history (the Mac app lists them under
Problems → Failed runs).

## Reading them
```bash
agentline incidents list                 # * = new
agentline incidents show <id>            # traceback, context, attachments
agentline incidents export ~/Desktop/agentline-report   # JSON + attachments + summary.md
agentline incidents seen <id> | rm <id> | clear
```
API: `GET /api/incidents`, `GET /api/incidents/{id}`, `POST /api/incidents/{id}/seen`,
`DELETE /api/incidents/{id}`, `DELETE /api/incidents`.

To get a problem fixed, export the report (or the Mac app's "Export report…") and hand the
folder or zip to a Claude Code session in this repo.

Library use (`App(...)` in your own code) records nothing unless you pass
`App(incidents=IncidentStore(...))`; the CLI always does.
