# Phase 55: named identities

Approach: Spec Kit ([specs/007-named-identities](../specs/007-named-identities/spec.md)), Lane 2.
Built on phase 54 (the browser sign-in screen). Decision 90.

## Why

Constitution Principle II says approvals, rejections, retries, cancels and deploys write an audit
row naming who acted. The API had one shared token, and every action through it was recorded as
the engine process's OS user (`default_actor()`). Once phase 54 let teammates use Studio from other
machines, every remote approval read as the person who started the server.

## What is built

- **People** (`src/agentline/people.py`): name, admin flag, created at, SHA-256 of the current
  token, in `<home>/people.json` (0600, atomic write, re-read when the file changes so a CLI
  revoke reaches a running server on its next request). Tokens are `alp_` + 256 random bits,
  shown once, kept nowhere. A corrupt file matches nobody and refuses writes (never overwritten).
- **Identity per request** (`src/agentline/api/identity.py`): one dependency on every `/api`
  router (replaces `_token_check`):
  - a person's token -> that person;
  - the shared `AGENTLINE_API_TOKEN` from another machine -> `shared-token` (still works, admin);
  - the shared token from the engine's own machine (loopback, no proxy headers) -> the local OS
    user. The Mac app always sends the shared token over loopback, so its approvals keep naming
    the Mac user (`AGENTLINE_USER`), as before. Behind a reverse proxy on the same machine that
    adds no `X-Forwarded-For`, shared-token users would read as the local user, as they did before;
  - no bearer and no shared token (loopback-only engine) -> the local OS user (admin);
  - anything else -> 401, also on an open engine (a revoked token is noticed, not downgraded).
- **Audit rows**: `POST /runs/{id}/approve|reject|retry` and `/runs/{id}/messages/{m}/apply`
  (run-chat cards that approve, reject, retry or cancel) pass `actor_of(request)`. Gates with
  `approvers:` now check against the person's name. `tests/api/test_identity.py` fails if any
  `api/` module calls `default_actor()` again.
- **Managing people**: `GET/POST /api/people`, `POST /api/people/{name}/token`,
  `PUT /api/people/{name}/admin`, `DELETE /api/people/{name}`. Writes need an admin identity
  (local user, shared token, or a person marked admin), else 403; non-admins list only
  themselves. Each change writes an audit row (`person.add|token|admin|revoke`) naming who did
  it. A token just issued is taught to the running redactor.
- **CLI**: `agentline users add NAME [--admin] | list [--json] | token NAME | admin NAME on|off |
  revoke NAME`. `add` and `token` print the token once.
- **Studio**: Settings > People (`components/settings/PeoplePage.tsx`): list, Add person, New
  token, Make/Remove admin, Revoke, a one-time token popup (`TokenDialog`). The sign-in screen
  asks for "your token"; Settings > General says who this browser is signed in as
  (`/api/auth/check` now returns `identity`).
- **Chat parity**: action `person` (op add / token / admin / revoke), checked against LIVE STATE
  PEOPLE (listed for admins only; LIVE STATE also says who is chatting). Applied by Studio through
  the same API with the browser's own token, so the same 403 applies; never auto-applied. The
  new token opens the popup and is kept off the card result and the conversation history. Chat
  apps (Telegram, Slack, Discord) answer "apply this one in the app".

## Checked

- `uv run pytest` (WSL Ubuntu), `ruff check`, `ruff format --check`.
- `web`: `npx tsc --noEmit`, `npx vitest run`, `npm run build`.
- New tests: `tests/test_people.py`, `tests/api/test_identity.py` (every audited route by person,
  shared token and local user; revoke and reissue; admin rules; a leak search of responses,
  audit rows, the home folder and the test folder for the token), `tests/cli/test_users.py`,
  person cases in `tests/ai/test_assistant_parity.py`, `web/src/lib/people.test.ts`, person
  cases in `web/src/lib/assistantApply.test.ts`.

## Not done

- Roles: every identity keeps the powers the shared token has (settings, keys, pipelines). Only
  people management is admin-only.
- Chat links still record the engine's OS user (`chatlink/service.py` uses `default_actor()`);
  naming the chat-app user is a follow-up.
- The CLI still acts as `$AGENTLINE_USER` / `$USER`.
- The People page and the sign-in flow with a personal token were not clicked through in a real
  browser; they are covered by type checks and unit tests only.
- The Mac app is unchanged (see the shared-token rule above); not run on a Mac in this phase.
