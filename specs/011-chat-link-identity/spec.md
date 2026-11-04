# Feature Specification: Chat-Link Audit Identity

**Feature Branch**: `phase-59` (stacked on phase 55, `claude/intelligent-murdock-60ac01`)

**Created**: 2026-11-04

**Status**: Approved (user, 2026-11-04)

**Input**: User description: "Chat links still write the engine's OS user into audit rows. Approve,
reject, retry and card apply through Telegram, Slack and Discord record `default_actor()`. Make
those rows name who acted." Design decided by the user: the pairing names the person; when there
is no person, the chat user is named; gate approver lists are checked against that new actor only.

## Context

Constitution Principle II requires every approval, rejection, retry and deploy to write an audit
row naming who acted. Spec 007 (phase 55) did this for the web API with per-person tokens and left
chat links out of scope. A chat link pairs exactly one chat-app account per platform (Telegram,
Slack, Discord) with the engine, using a short code that someone requests in Settings > Chat apps.
Every decision taken through the link is still recorded as the account the engine runs under, so
the audit trail cannot tell the engine owner from whoever holds the paired phone.

## Clarifications

### Session 2026-11-04

- Q: How does a chat account map to a person? → A: Through the pairing. The person who requests the pairing code with their personal token is stored with the code, then on the link it creates. There is no separate mapping command.
- Q: What when nobody is mapped (code made by the local user or the shared token, or a link paired before this change)? → A: The actor is the chat user, `<platform>:@<username>` (the user id when there is no username).
- Q: Gate `approvers:` lists? → A: Only the new actor counts. A gate whose list names only the engine's OS user refuses approvals from a chat link.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A chat approval names the person who linked the chat (Priority: P1)

Alice signs in to Studio with her personal token, opens Settings > Chat apps and links Telegram.
Later she approves a gate from her phone. The run's audit row says `alice`.

**Why this priority**: This is the gap: Principle II's "naming who acted" is false for every chat
action today.

**Independent Test**: Request a pairing code as person `alice`, pair through the fake Telegram
server, approve a gate from the fake chat and read the audit row.

**Acceptance Scenarios**:

1. **Given** a link paired with a code that `alice` requested, **When** the chat approves, rejects
   or retries a run, applies a Chat proposal that retries, or taps a run-chat card, **Then** the
   audit row's actor is `alice`.
2. **Given** the same link, **When** the action is recorded, **Then** the row still says which chat
   it came through (`via` stays `telegram:@<username>`).
3. **Given** the same setup on Slack or Discord, **Then** the actor is `alice` there too.

---

### User Story 2 - Without a person, the chat user is named (Priority: P1)

The engine owner links Telegram from the Mac app (local user, no personal token). An approval from
the chat is recorded as `telegram:@owner_handle`, not as the OS account.

**Why this priority**: Most installs have no people yet; their rows must still name the actor
truthfully rather than the engine account.

**Independent Test**: Pair with a code made by the local user (or with an old state file that has
no person) and check the audit row.

**Acceptance Scenarios**:

1. **Given** a code requested by the local user or with the shared token, **When** the link is used
   to approve, **Then** the actor is `<platform>:@<username>`.
2. **Given** a chat account with no username, **Then** the actor is `<platform>:@<user id>`.
3. **Given** a link paired before this change, **Then** it keeps working and acts as the chat user.
4. **Given** a link whose person was later removed from the people list, **Then** its actions are
   recorded as the chat user, not the removed name.

---

### User Story 3 - Approver lists are checked against who acted (Priority: P2)

A gate lists `approvers: [alice]`. Alice's linked chat can approve it; a chat linked by someone
else cannot.

**Independent Test**: Gates with approver lists, approved through links with and without a person.

**Acceptance Scenarios**:

1. **Given** `approvers: [alice]` and a link that acts as `alice`, **When** it approves, **Then**
   the gate opens.
2. **Given** `approvers: [<engine OS user>]` and a link with no person, **When** it approves,
   **Then** it is refused with "not an approver" and the gate stays waiting.

---

### User Story 4 - Settings shows who a link acts as (Priority: P3)

Settings > Chat apps shows, for each linked chat, the person it acts as or that it acts as the chat
user.

**Acceptance Scenarios**:

1. **Given** a link with a person, **When** the chat apps view is read, **Then** it names the
   person; without one, it says nothing about a person (existing fields unchanged).

### Edge Cases

- A pairing code requested by `alice`, then a new code requested by `bob` before pairing: only the
  latest code pairs, and it names `bob`.
- The person on a link is renamed or removed: removal falls back to the chat user (people names are
  not renamed in spec 007).
- A message from a chat account other than the paired one: refused as today; nothing recorded.
- Answers to `chat.ask` questions and notes to a run thread are not audit rows; they keep their
  `by` / `via` fields as today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Requesting a pairing code MUST record who requested it when that requester is a
  named person; requests by the local user or the shared token record no person.
- **FR-002**: Pairing MUST copy the code's person (if any) onto the link it creates.
- **FR-003**: Approve, reject, retry (direct or from a Chat proposal) and run-chat card apply from a
  chat link MUST record as actor the link's person while that person exists, else
  `<platform>:@<username or user id>`.
- **FR-004**: The engine's OS user MUST NOT be recorded as the actor of a chat-link action.
- **FR-005**: The `via` value recorded with chat-link actions MUST stay as today.
- **FR-006**: Gate approver lists MUST be checked against the actor of FR-003.
- **FR-007**: Links paired before this change MUST keep working without re-pairing.
- **FR-008**: The chat apps view MUST name the person a link acts as (additive field).
- **FR-009**: Behavior MUST be the same on Telegram, Slack and Discord.

### Key Entities

- **Pairing code**: short-lived code; gains the requesting person (optional).
- **Chat link**: one paired chat account per platform; gains the person it acts as (optional).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 0 audit rows written through a chat link name the engine's OS user (tests cover all
  four action kinds on each platform where they apply).
- **SC-002**: 100% of chat-link audit rows name either a person from the people list or a chat
  account.
- **SC-003**: Existing links keep working with 0 re-pairing steps.

## Assumptions

- Depends on spec 007 (people list, request identity). Built on its branch.
- One paired chat account per platform, as today; multi-user chat links are out of scope.
- The CLI's own actor and Mac-app behavior are unchanged.
- Tests use the fake chat servers (`tests/ai/fake_telegram.py`, `tests/ai/fake_ws.py`).
