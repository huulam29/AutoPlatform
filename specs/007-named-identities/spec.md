# Feature Specification: Named Identities for the Engine API

**Feature Branch**: `claude/intelligent-murdock-60ac01` (based on phase 54, to be merged as `phase-55`)

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "Audit rows for actions taken through the web API name the engine's OS
user, not the person. Now that remote browsers sign in with a token (phase 54), every remote
approval is recorded as the server's user. Design and build named identities." Design decided by
the user: one token per person, created in Settings, shown once, only a hash kept; people are
managed by the local user and by people marked admin; the shared API token keeps working and is
recorded as `shared-token`; loopback without a token stays the local OS user.

## Context

Constitution Principle II requires every approval, rejection, resolution, cancel, retry and deploy
to write an audit row naming who acted. The engine API has one shared bearer token (decision 82),
and every action taken through it is recorded under the engine process's OS user. Since phase 54
lets teams use Studio from other machines, an approval by any teammate reads as the person who
started the server. This feature gives each person their own token so the engine knows, and
records, who did what.

## Clarifications

### Session 2026-11-03

- Q: How does the API know who is acting? → A: A token per person, created in Settings, shown once; only its hash is kept. No external identity provider, no typed-in names.
- Q: Who may add and revoke people? → A: The person at the engine's machine (a loopback request with no token) and people marked admin. Others see only themselves.
- Q: What happens to the shared `AGENTLINE_API_TOKEN`? → A: It keeps working; actions taken with it are recorded as `shared-token`.
- Q: Which branch? → A: Built on phase 54 (the browser sign-in screen), so the sign-in screen takes a personal token too.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An approval names the person who approved (Priority: P1)

Alice opens Studio from her laptop, signs in with her personal token and approves a gate. The
run's audit trail says `alice` approved it, not the account the engine runs under.

**Why this priority**: This is the gap: Principle II's "naming who acted" is false for every remote
action today.

**Independent Test**: Create a person, send approve / reject / retry / card-apply requests with
their token, and read the audit rows.

**Acceptance Scenarios**:

1. **Given** a person `alice` with a token, **When** a request with that token approves, rejects or
   retries a run, or applies a run-chat card that does so, **Then** the audit row's actor is `alice`.
2. **Given** a request with the shared API token, **When** it approves a gate, **Then** the actor
   is `shared-token`.
3. **Given** a request from the engine's own machine with no token, **When** it approves a gate,
   **Then** the actor is the local OS user, as today.
4. **Given** the Chat proposes an approval and Alice applies it in Studio, **When** it runs,
   **Then** the actor is `alice` (Chat actions go through the same requests as the pages).
5. **Given** an action's response that echoes the actor, **When** it is returned, **Then** it
   names the same actor the audit row has.

---

### User Story 2 - Give a teammate their own token (Priority: P1)

The person who runs the engine opens Settings → People, adds `bob`, and gets a token to send him.
The token is shown once. Bob signs in with it on the Studio sign-in screen.

**Why this priority**: Without a way to create tokens, Story 1 has no identities to record.

**Independent Test**: Add a person through the API and the CLI; check the token is shown once,
works for sign-in, and appears nowhere afterwards (API responses, config, ledger, logs).

**Acceptance Scenarios**:

1. **Given** the local user, **When** they add a person with a name, **Then** the response holds
   a new token once, and the person is listed with name, admin flag and creation time, never the
   token or its hash.
2. **Given** the CLI on the engine's machine, **When** `agentline users add bob` runs, **Then** it
   prints the token once; `agentline users list` shows names only.
3. **Given** a person's token, **When** it is entered on the sign-in screen, **Then** it is
   accepted and Studio shows who is signed in.
4. **Given** a name already in use, or an empty or reserved name (`shared-token`, a name with
   spaces or control characters), **When** a person is added, **Then** it is refused.
5. **Given** a person who lost their token, **When** an admin issues a new one, **Then** the old
   token stops working and the new one is shown once.

---

### User Story 3 - Revoke access at once (Priority: P1)

Bob leaves the team. An admin revokes him; his next request is refused, without restarting the
engine.

**Why this priority**: Per-person tokens are only safer than one shared token if one can be
withdrawn without changing everyone else's.

**Independent Test**: Revoke a person and send a request with their token.

**Acceptance Scenarios**:

1. **Given** a revoked person, **When** their token is used on any API route, **Then** the request
   is refused as unauthorized.
2. **Given** a revocation, **When** it is done, **Then** an audit row records who revoked whom.
   Adding a person and issuing a new token are audited the same way.
3. **Given** past audit rows naming a revoked person, **When** they are listed, **Then** they still
   name that person.

---

### User Story 4 - Only admins manage people (Priority: P2)

Carol, not an admin, opens Settings → People and sees herself only. She cannot add people, mint a
token under another name, or make herself admin.

**Why this priority**: Whoever can mint tokens can act as anyone; without this the names in the
audit trail prove nothing.

**Independent Test**: With a non-admin token, call each people-management route.

**Acceptance Scenarios**:

1. **Given** a non-admin person, **When** they add, revoke, reissue or change the admin flag of a
   person, **Then** the request is refused (403).
2. **Given** a non-admin person, **When** they list people, **Then** they see only themselves.
3. **Given** an admin, **When** they revoke the last other admin or themselves, **Then** it is
   allowed: the local user can always manage people, so nobody is locked out.
4. **Given** the Chat, **When** a person asks it to add or revoke someone, **Then** the action is
   applied through the same requests and refused the same way for non-admins; the new token is
   shown in Studio, never sent to the model or to a chat app.

---

### Edge Cases

- No people configured: behaviour is exactly as today (shared token or open loopback).
- A token that matches both the shared token and a person cannot exist (tokens are generated
  random; a collision is refused when issuing).
- A request off loopback with no token, when people exist but no shared token is set: refused, as
  today for any token-protected server.
- A person's token used from the engine's own machine: the person is recorded, not the OS user.
- The people file is missing, unreadable or corrupt: the engine starts, personal tokens are
  refused, and the problem is logged; the shared token and loopback keep working.
- Chat links (Telegram, Slack, Discord) and the CLI are not changed by this feature: the CLI keeps
  the local user (`$AGENTLINE_USER`), chat links keep their current actor.
- Two admins editing at once: each change is written atomically; the last write wins per person.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST keep a list of people: name, admin flag, creation time, and a one-way
  hash of their current token. The token itself MUST NOT be stored anywhere.
- **FR-002**: People and their token hashes MUST live in the engine's private home folder
  (owner-only, written atomically), never in `agentline.toml`, the repository, the ledger or logs.
- **FR-003**: Every API request MUST be resolved to an identity: a person (their token), the shared
  token (`shared-token`), or the local user (loopback with no token). A token that matches none is
  refused as today.
- **FR-004**: Every audit row written because of an API request (approve, reject, retry, run-chat
  card apply, and any later audited route) MUST name the request's identity.
- **FR-005**: Token comparison MUST be constant-time, and a revoked or reissued token MUST be
  refused from the next request on, without a restart.
- **FR-006**: The API MUST let the local user and admins add a person (returning the new token
  once), reissue a person's token (returning it once), set the admin flag, and revoke a person.
  Other identities MUST be refused (403). The shared token counts as an admin (it is the engine's
  own setup token and already holds every other power).
- **FR-007**: Listing people MUST return names, admin flags and creation times only. A non-admin
  person sees only themselves.
- **FR-008**: The sign-in check MUST accept a person's token and report who is signed in (name and
  admin flag) without echoing the token.
- **FR-009**: Studio MUST show who is signed in and offer Settings → People for admins.
- **FR-010**: The CLI MUST offer `agentline users add|list|revoke|token` on the engine's machine;
  `add` and `token` print the token once.
- **FR-011**: Adding, revoking and reissuing MUST write an audit row naming who did it and the
  person affected, and never the token.
- **FR-012**: The Chat MUST have an action to add, revoke and reissue people, applied by Studio
  through the same API (so the same permission rules hold), and the new token MUST be shown in
  Studio only, never sent to a model, a chat app or the conversation history.
- **FR-013**: Names MUST be 1-64 characters of letters, digits, `.`, `_`, `-` and `@`, unique
  case-insensitively, and MUST NOT be `shared-token` or `local`.
- **FR-014**: With no people configured, every behaviour MUST be unchanged.

### Key Entities

- **Person**: someone allowed to use the API under their own name. Name, admin flag, created at,
  token hash. Revoking removes the person from the list (past audit rows keep the name).
- **Identity**: who a request is from: `person:<name>`, `shared-token`, or the local OS user.
  Carries whether it may manage people.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of audit rows written through the web API by a signed-in person name that
  person (tested for every audited route).
- **SC-002**: A revoked token is refused on the very next request, with no restart.
- **SC-003**: A search of every API response, the config, the ledger and the logs after adding a
  person finds the token zero times (other than the one response that issued it).
- **SC-004**: An admin adds a teammate and hands over a working token in under a minute.

## Assumptions

- Permissions beyond people management (who may approve, change settings or keys) are out of
  scope: every signed-in identity keeps the powers the shared token has today. Roles can come later.
- Chat links (Telegram, Slack, Discord) keep their current actor; naming the chat user there is a
  separate follow-up.
- The shared token stays the bootstrap for engines reached only remotely: whoever holds it can add
  the first admin.
- One engine process reads the people file; several engines on one Postgres ledger each have their
  own people file (Postgres is not used for people).
