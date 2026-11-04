# Implementation Plan: Chat-Link Audit Identity

**Branch**: `phase-59` | **Date**: 2026-11-04 | **Spec**: [spec.md](spec.md)

## Summary

The pairing code remembers the person who asked for it; pairing copies that person onto the link;
one helper `ChatLink.actor(link)` turns a link into the audit actor (the person while they exist in
`people.json`, else the chat user string that `via` already uses). The four audited call sites in
`chatlink/service.py` use it instead of `default_actor()`. Gate approver checks already compare
against the actor passed in (`App.approve`), so FR-006 needs no engine change.

## Technical Context

Python 3.11+, stdlib only in `chatlink/`; FastAPI for `api/chat.py`. Tests: pytest with the fake
Telegram server and the fake websocket server. `chatlink/state.py` uses `fcntl`, so the suite runs
under WSL Ubuntu on this machine.

## Constitution Check

- Principle II (audit names who acted): this feature closes the chat-link gap. Pass.
- Durability: no scheduler, ledger or transition change; actor is a string argument already
  passed through. Pass.
- Secrets: nothing secret is stored; the link holds a person's name, not a token. Pass.
- Testing: fakes only; tests fail without the change (they assert the actor). Pass.

## Design

1. `chatlink/state.py` `new_pair_code(person: str | None = None)`: stores
   `pair = {code, expires, person?}`.
2. `api/chat.py` `POST /chat/{name}/pair` takes the request; when `identity_of(request).kind ==
   "person"` it passes the name. The view's `one(name)` adds `acts_as` (the actor string the link
   records, or null when unlinked) — additive.
3. `chatlink/service.py`:
   - `try_pair`: copies `pair.get("person")` to the link as `person` (omitted when none).
   - `_via(link)` stays; new `actor(link)`: `person` if `PeopleStore().get(person)` is not None,
     else `_via(link)`. The store is created once per `ChatLink` (it re-reads on file change).
   - `decide`, `retry`, `decide_card` use `self.actor(link)`; `decide`/`retry` use `self._via`.
   - `default_actor` import removed from the service.
4. A module function `chatlink.service.link_actor(platform, link, people)` shared by the service and
   the API view so both say the same thing.
5. Web: Settings > Chat apps linked line adds "acts as <acts_as>" (one `<span>`, existing tokens).
   `ChatPlatform` type gains `acts_as?: string | null`.

## Files

- `src/agentline/chatlink/state.py`, `src/agentline/chatlink/service.py`, `src/agentline/api/chat.py`
- `web/src/components/SettingsExtra.tsx` and its type (`web/src/lib/api.ts` or wherever
  `ChatPlatform` lives)
- `tests/ai/test_chatlink.py` (Telegram), `tests/ai/test_chat_platforms.py` (Slack/Discord via
  `fake_ws.py`), an API test for the pair endpoint + `acts_as`
- `docs/phase-59.md`, `specs/011-chat-link-identity/*`

## Risks

- Gates whose `approvers:` list the OS user stop accepting chat approvals (user decision; called
  out in docs/phase-59.md).
- Existing tests that assert the OS user as the chat actor will change to the chat user string.
