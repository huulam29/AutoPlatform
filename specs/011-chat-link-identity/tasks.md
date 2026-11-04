# Tasks: Chat-Link Audit Identity

**Input**: [spec.md](spec.md), [plan.md](plan.md)

## Phase 1: Tests first (fail without the change)

- [x] T001 [US2] `tests/ai/test_chatlink.py`: approve, reject, retry and run-chat card from a link
  paired by the local user record actor `telegram:@user42` (not the OS user).
- [x] T002 [US1] `tests/ai/test_chatlink.py`: a code requested as person `alice` pairs a link that
  records `alice`; after `alice` is removed, actions record `telegram:@user42`.
- [x] T003 [US2] `tests/ai/test_chatlink.py`: an old link with no `person` and no username acts as
  `telegram:@<user id>`.
- [x] T004 [US3] `tests/ai/test_chatlink.py`: `approvers: [alice]` opens for alice's link;
  `approvers: [<OS user>]` refuses a link with no person.
- [x] T005 [US1] `tests/ai/test_chat_platforms.py`: Slack and Discord approvals record the person
  (fake_ws).
- [x] T006 [US1,US4] API test: `POST /api/chat/telegram/pair` with alice's token stores the person;
  with no token stores none; `GET /api/chat` returns `acts_as`.

## Phase 2: Implementation

- [x] T007 `chatlink/state.py`: `new_pair_code(person=None)`.
- [x] T008 `chatlink/service.py`: `link_actor`, `ChatLink.actor`, `try_pair` copies the person;
  `decide`, `retry`, `decide_card` use it.
- [x] T009 `api/chat.py`: pair records a person identity; view adds `acts_as`.
- [x] T010 Web: Settings > Chat apps shows "acts as"; type field; `npx tsc --noEmit`, `npm test`.

## Phase 3: Polish

- [x] T011 `docs/phase-59.md` (Approach: Spec Kit (specs/011-chat-link-identity)); mark spec 007's
  "Not done" chat item as done in phase 59 notes.
- [x] T012 Full suite in WSL Ubuntu, ruff check + format check.
