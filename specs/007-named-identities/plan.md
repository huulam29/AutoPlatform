# Implementation Plan: Named Identities for the Engine API

**Branch**: `claude/intelligent-murdock-60ac01` (on phase 54; merged as `phase-55`) | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/007-named-identities/spec.md`

## Summary

Every API request is resolved to an **identity** (a person, `shared-token`, or the local OS user)
by one FastAPI dependency that replaces `_token_check`. Routes that write audit rows take the actor
from the request instead of `default_actor()`. People live in `<home>/people.json` (owner-only,
atomic writes) as name, admin flag, creation time and the SHA-256 of their token; the token is shown
once and never kept. People are managed through `/api/people` (local user, shared token and admins
only), the `agentline users` CLI, Settings → People, and a `person` Chat action.

## Technical Context

**Language/Version**: Python 3.11+ (engine), TypeScript + React (Studio)

**Primary Dependencies**: stdlib only for `people.py` (`hashlib`, `hmac`, `secrets`, `json`);
FastAPI for the routes (`server` extra), as today.

**Storage**: `$AGENTLINE_HOME/people.json`, 0600, written atomically (same helper pattern as
`secrets.py`). Not the ledger (no schema change, no Postgres work), not `agentline.toml`.

**Testing**: pytest with FastAPI `TestClient`; vitest for the People page and `applyAction`.

**Target Platform**: `agentline serve` on macOS/Linux; Studio in any browser and the Mac app.

**Project Type**: web service + web UI + CLI.

**Performance Goals**: identity lookup costs one `stat` per request (reload on mtime change) plus
one SHA-256; revocations are seen on the next request (SC-002).

**Constraints**: constant-time comparison; the token never in a response other than the one that
issued it, never in logs, config, ledger or Chat history.

**Scale/Scope**: tens of people per engine.

## Constitution Check

| Principle | How this plan holds it |
|-----------|------------------------|
| I Durable | No change to scheduler, worker, leases or transitions. Audit rows still commit in the action's transaction (`App.approve` etc. take `actor=`). PASS |
| II People decide | This feature makes "naming who acted" true for the web API. People-management actions are audited. PASS |
| III Secrets | Tokens are never stored; only SHA-256 hashes in an owner-only file in the home folder. API returns a token exactly once on issue, never hashes. A newly issued token is added to the running redactor. The Chat never sees a token: Studio applies the action and shows the token itself. PASS |
| IV One engine | `people.py` is stdlib. No new frameworks. PASS |
| V Least privilege | Non-admins cannot mint tokens. Roles beyond that are out of scope (spec Assumptions). PASS |
| VI Tested | Tests for every audited route per identity kind, people store, API permissions, CLI, Chat action, web page; leak test searches responses/ledger/config/logs for the token. PASS |
| VII Honest scope | `docs/phase-55.md` says what is built; chat links and roles listed as not done. PASS |

## Design

### Identity resolution (`src/agentline/api/identity.py`, new)

```python
@dataclass(frozen=True)
class Identity:
    kind: Literal["local", "shared", "person"]
    name: str  # OS user, "shared-token", or the person's name
    admin: bool  # local and shared: True
```

`identity_dependency(shared_token, people)` returns a FastAPI dependency that:

1. Reads `Authorization: Bearer <t>`.
2. No bearer: if a shared token is set (auth required), 401 as today; else `Identity("local",
   default_actor(), True)`.
3. Bearer equal to the shared token (`hmac.compare_digest`): `Identity("shared", "shared-token",
   True)`.
4. Bearer whose SHA-256 matches a person (`people.match`, compare_digest against every stored
   hash): `Identity("person", name, admin)`.
5. Otherwise 401. Note: a bearer sent when no auth is required and that matches nobody is still
   refused, so a mistyped personal token is noticed.

The dependency stores the identity on `request.state.identity`; `actor_of(request)` returns its
name. It replaces `_token_check` on every router in `create_server`. When people exist but no
shared token is set (loopback serve), the API stays open to loopback as today and personal tokens
are accepted.

`/api/auth/check` and the restart rule in `api/app.py` use the same resolution:
`{token_required, ok, identity: {kind, name, admin}}`.

### People store (`src/agentline/people.py`, new, stdlib)

- `PeopleStore(path)`: `list()`, `add(name, admin=False) -> token`, `reissue(name) -> token`,
  `set_admin(name, bool)`, `revoke(name)`, `match(token) -> Person | None`.
- Tokens: `"alp_" + secrets.token_urlsafe(32)`. Hash: `sha256(token).hexdigest()`.
- File: `{"version": 1, "people": [{"name", "admin", "created_at", "token_sha256"}]}`, written via
  temp file + `os.replace`, mode 0600. Reloaded when mtime/size changes, so CLI edits and API edits
  are seen by the running server without a restart.
- Corrupt or unreadable file: logged once, treated as empty for matching; writes refuse (so a bad
  file is never overwritten silently).
- Name rule: `^[A-Za-z0-9._@-]{1,64}$`, unique case-insensitively, not `shared-token` or `local`.

### Routes (`src/agentline/api/people.py`, new) — see [contracts/api.md](contracts/api.md)

`GET /people`, `POST /people`, `POST /people/{name}/token`, `PUT /people/{name}/admin`,
`DELETE /people/{name}`. Writes need `identity.admin`, else 403. Each write calls `app.audit`
(`person.add`, `person.token`, `person.admin`, `person.revoke`) with `actor=identity.name` and
`detail={"person": name}`. The issued token is added to `app.redactor` before the response.

### Actor from the request

`api/runs.py`: `retry`, `approve`, `reject`, `apply_card` take `request: Request` and pass
`actor_of(request)`; the approve response's `actor` echoes it. `rc.say` gets `author=` only if the
run-thread already records one (it doesn't: unchanged). A test walks every route that ends in
`App.approve/reject/retry/cancel/resolve/audit` to catch any route still on `default_actor()`.

### CLI

`agentline users add NAME [--admin]`, `users list [--json]`, `users token NAME`,
`users revoke NAME`, `users admin NAME on|off`. Audited with `env.actor`.

### Chat parity

- `assistant.py`: `ACTION_TYPES += ("person",)`; shape `{"type": "person", "op": "add" | "token" |
  "revoke" | "admin", "name": str, "admin": bool}`; `_check_one` validates op and name rule, and
  for `token`/`revoke`/`admin` that the name is in LIVE STATE people. LIVE STATE lists people names
  and admin flags (only for admin identities; the assistant route passes the request's identity).
  GUIDE line.
- `web/src/lib/assistant.ts`: `applyAction` calls the people API with the browser's own token (so
  403 applies for non-admins); for `add`/`token` the returned token opens the same "copy this
  token now" dialog as Settings and is **not** put in the action result or conversation history.
  `actionTitle`.
- `docs/phase-25.md` table row.

### Studio

- `web/src/api.ts` (`api.people*`, `checkToken` returns the identity).
- `web/src/components/settings/PeoplePage.tsx`: list, Add person (name + admin), Reissue, Admin
  toggle, Revoke (confirm), one-time token dialog with Copy. Non-admins see only their own row and
  no buttons.
- `Settings.tsx`: "People" tab. General: "Signed in as alice (admin)" next to Sign out.
- SignIn copy: "your personal token or the engine's API token".

## Project Structure

### Documentation (this feature)

```text
specs/007-named-identities/
├── spec.md, plan.md, research.md, data-model.md, quickstart.md
├── contracts/api.md
├── checklists/requirements.md, checklists/security.md
└── tasks.md
```

### Source Code

```text
src/agentline/people.py                 new: store, tokens, name rule
src/agentline/api/identity.py           new: Identity, dependency, actor_of
src/agentline/api/people.py             new: /api/people routes
src/agentline/api/server.py             use identity dependency; people store
src/agentline/api/app.py                auth/check + restart use identity
src/agentline/api/runs.py               actor from request
src/agentline/api/assistant.py          pass identity to gather_live
src/agentline/assistant.py              person action, LIVE STATE, GUIDE
src/agentline/cli/__init__.py, commands.py   users subcommands
web/src/api.ts, lib/assistant.ts, components/settings/PeoplePage.tsx,
web/src/components/Settings.tsx, SignIn.tsx, styles.css
tests/test_people.py, tests/api/test_identity.py, tests/api/test_people_api.py,
tests/cli/test_users.py, tests/ai/ (assistant person action), web/src/**/*.test.ts(x)
docs/phase-55.md, docs/phase-25.md (row), ARCHITECTURE.md §12 (decision 90), CLAUDE.md (status)
```

**Structure Decision**: existing layout; two new engine modules, one new API router, one new page.

## Complexity Tracking

| Violation | Why needed | Simpler alternative rejected because |
|-----------|------------|-------------------------------------|
| None | | |
