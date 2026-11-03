# Phase 53: Spec Kit, as-built specs, and the decisions they raised

## What was added

- Spec Kit 1.2.5 for Claude in `.specify/`, with the agentline constitution
  (`.specify/memory/constitution.md`, now 1.1.0). It is modelled on the TaxChat constitution,
  without its personal-information rules.
- Five specs reverse-engineered from the code, `specs/001`–`005`, each with a code reference for
  every requirement. `specs/README.md` lists what is covered and what isn't.

Reading the code for the specs turned up three safety gaps, fixed first, and nine open questions,
which the user decided (ARCHITECTURE.md decisions 77–85).

## Fixes

| Gap | Fix | Test |
|---|---|---|
| A loop's `on_exhausted` could reach a deploy-class action without a gate | The gate check follows `on_exhausted` targets (`pipeline/validate.py`) | `test_exhausted_loop_cannot_reach_a_deploy_around_its_gate` |
| `for_each` task records and journal lines kept raw secrets | Item, result, error and journal detail are redacted (`pipeline/compile.py`) | `test_task_records_and_journal_are_redacted` |
| A manual retry of `env.deploy` reused the recorded failure | The effect is named by attempt key, `deploy#r1.1` after one retry (`actions/env.py`) | `test_manual_retry_redeploys` |

## Decisions 77–85

| # | Behaviour now | Where | Test |
|---|---|---|---|
| 77 | Retrying a cancelled run is refused (409 from the API) | `app.py` `App.retry` | `test_retry_refuses_a_cancelled_run_and_the_journal_shows_claims_and_cancels` |
| 78 | A call that raised is recorded as `failed`. With no key and no check, the effect advisor decides on the next attempt; deploy-class effects stop for a person | `context.py`, `advisor.py`, `serve` installs it | `test_clean_failure_*`, `tests/core/test_advisor.py` |
| 79 | Claims and cancels are journaled | `Scheduler.claim`, `Scheduler.cancel` | as for 77 |
| 80 | `timeout` is refused on gate, wait, router and chat nodes | `pipeline/spec.py` | `test_timeout_only_on_agents_and_actions` |
| 81 | Host commands get basics plus `pass_env` only | `actions/_util.py` `command_env` | `test_host_commands_see_only_basics_and_what_they_declare`, `test_deploy_passes_only_the_env_pass_env` |
| 82 | Off-localhost `serve` with no token creates `AGENTLINE_API_TOKEN` | `api/server.py` `ensure_api_token` | `tests/api/test_api_token.py` |
| 83 | First poll pipeline wins (unchanged, now intended) | `sources/jira_poll.py` | — |
| 84 | Own worktree, branch and container per `for_each` task | `workspaces/base.py`, `workspaces/git.py`, `workspaces/container.py` | `test_for_each_tasks_each_get_their_own_checkout` |
| 85 | Bridge networking stays; §11.1 now says egress control isn't built | `ARCHITECTURE.md` | — |

## Notes for upgrading

- **Host commands lose inherited variables (81).** A `shell.run` command or deploy script that
  read a token or setting from the host environment must now name it: `with: { pass_env: [NAME] }`
  on the node, or `pass_env = ["NAME"]` under `[environments.<env>]`. The `pr-review` template now
  passes `GH_TOKEN` and `GITHUB_TOKEN` to `gh`.
- **`timeout` on a gate, wait, router or chat node is now a validation error (80).** It never did
  anything there, so removing it changes nothing.
- **The advisor calls the Chat's harness (78).** It only runs under `serve`, only for an effect
  with neither key nor check, and its cost counts against the run. `AGENTLINE_ADVISOR=off` turns
  it off. Without it, a clean failure stops at needs-review, as before.
- **`for_each` coding tasks push to their own branches (84).** A node after a `for_each` coding
  node receives a list of branch artifacts, one per task. Merging them is the pipeline's job.
- **An off-localhost `serve` now needs a token (82).** The first start creates it in the secrets
  file and says where. The value is never printed.

## Limits

- Tested on Windows with the Unix-only `fcntl` import stubbed, so the shell-based tests (including
  the old deploy tests) haven't run with these changes. They need a macOS or Linux run.
- The advisor has only been tested with stand-in advisors, not a real model.
- `probe.py` replays a coding node from `detail.workspace`. A `for_each` task's workspace is under
  `detail.workspaces.<n>`, so probing a `for_each` coding node isn't supported.
