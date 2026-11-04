# Phase 59: chat-link audit identity

Approach: Spec Kit (specs/011-chat-link-identity). Lane 1 (score 3/5: ambiguity, blast radius,
review cost). Stacked on phase 55 (named identities, spec 007).

## What changed

Chat links (Telegram, Slack, Discord) used to record the engine's OS user as the actor of every
approve, reject, retry and run-chat card tap. Now they name who acted:

- **The pairing names the person.** `POST /api/chat/{platform}/pair` called with a person's token
  stores that person with the code (`pair.person` in `chatlink.json`); pairing copies it onto the
  link (`<platform>.person`). A code made by the local user or with the shared token links no person.
- **Actor** (`chatlink.service.link_actor`): the link's person while they are in `people.json`,
  else the chat user `<platform>:@<username or user id>`, the same string as `via`. A person
  revoked later falls back to the chat user on the next action. Links paired before this phase
  have no person and act as the chat user; no re-pairing needed.
- Used by `/approve`, `/reject`, the gate buttons, `/retry` and the Retry button, Chat proposals
  that retry, and run-chat Confirm cards. `via` is unchanged.
- **Approver lists** are checked against that actor (`App.approve` already compares the actor it
  is given). A gate whose `approvers:` list names only the engine's OS user now refuses
  approvals from a chat link with "not an approver". Add the person (or `telegram:@handle`) to
  the list. This is the user's decision (stricter, matches the audit row).
- `GET /api/chat` gains `acts_as` per platform (null while unlinked). Settings > Chat apps shows
  "Approvals and retries from it are recorded as …".

## Files

`src/agentline/chatlink/state.py` (`new_pair_code(person)`), `src/agentline/chatlink/service.py`
(`link_via`, `link_actor`, `ChatLink.actor`, `try_pair`), `src/agentline/api/chat.py`,
`web/src/api.ts`, `web/src/components/SettingsExtra.tsx`.

## Tests

- `tests/ai/test_chatlink.py` (fake Telegram server): chat user as actor for approve / reject /
  retry; a person's code links a chat that acts as them, `via` kept, falls back after revoke; an
  old link with no username acts as `telegram:@<id>`; approver lists; the pair API records the
  person and `acts_as`; the run-chat card test now asserts the actor too.
- `tests/ai/test_chat_platforms.py`: Slack and Discord approvals name the person, then the chat
  user after revoke (the loop's fake transport).

## Not done

- Answers to `chat.ask` and notes to run threads aren't audit rows; they keep their `by` / `via`.
- One paired chat account per platform, as before.
- Not tried against real Telegram / Slack / Discord.
