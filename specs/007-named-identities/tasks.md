# Tasks: Named Identities for the Engine API

**Input**: `specs/007-named-identities/` (spec, plan, data-model, contracts)

## Phase 1: Foundation

- [x] T001 [US2] `src/agentline/people.py`: PeopleStore (load on mtime change, atomic 0600 write, add/reissue/set_admin/revoke/match, name rule, corrupt-file handling) + `tests/test_people.py`
- [x] T002 [US1] `src/agentline/api/identity.py`: Identity, `identity_dependency`, `actor_of`, `resolve` + `tests/api/test_identity.py`
- [x] T003 [US1] `src/agentline/api/server.py`: replace `_token_check` with the identity dependency; create the store from `$AGENTLINE_HOME`; `api/app.py` auth/check + restart use identity

## Phase 2: US1 audit rows name the person

- [x] T004 [US1] `src/agentline/api/runs.py`: retry/approve/reject/apply_card use `actor_of(request)`
- [x] T005 [US1] `tests/api/test_identity_audit.py`: every audited route × {person, shared, local}; a guard test that no `api/` module calls `default_actor()` outside identity.py

## Phase 3: US2/US3/US4 manage people

- [x] T006 [US2] `src/agentline/api/people.py` routes + audit rows + redactor; registered in server
- [x] T007 [US3][US4] `tests/api/test_people_api.py`: permissions (403 non-admin, self-only list), revoke → 401 next request, reissue, leak search (responses, ledger, config, logs)
- [x] T008 [US2] CLI `agentline users add|list|token|admin|revoke` + `tests/cli/test_users.py`

## Phase 4: Chat parity

- [x] T009 [US4] `assistant.py` `person` action: ACTION_TYPES, `_check_one`, GUIDE, LIVE STATE (admins only) ; `api/assistant.py` passes identity + tests
- [x] T010 [US4] `web/src/lib/assistant.ts` applyAction/actionTitle; token dialog, not in history + test

## Phase 5: Studio

- [x] T011 [US2] `web/src/api.ts` people calls; `checkToken` identity
- [x] T012 [US2] `components/settings/PeoplePage.tsx` + Settings tab + General "Signed in as"; SignIn copy + vitest
- [x] T013 `npm test`, `tsc --noEmit`, `npm run build`, `lint_hardcodes` if available

## Phase 6: Docs and checks

- [x] T014 `docs/phase-55.md`, `docs/phase-25.md` row, ARCHITECTURE.md §12 decision 90, CLAUDE.md status
- [x] T015 `uv run pytest`, `ruff check`, `ruff format --check`

Dependencies: T001→T002→T003→T004/T006; T006→T008/T009/T011; T011→T012/T010.
