# Specs

Feature specs in the standard Spec Kit layout (`specs/NNN-short-name/spec.md`). The constitution is
at `.specify/memory/constitution.md`.

## As-built specs

Specs 001 to 005 were reverse-engineered from the code at commit `f45743c`. Spec Kit has no
reverse-engineering command, and neither does the TaxChat setup these specs follow, so they use the
normal spec template with three sections added:

- **Differences From the Documentation**: where the code and `ARCHITECTURE.md` disagree. The code is
  what the spec describes.
- **Code References**: file and line evidence for every functional requirement.
- **`[NEEDS CLARIFICATION]`** markers, where the code shows what happens but not whether it is what
  was intended. Resolving a marker is a decision for the user.

Three confirmed safety gaps were fixed on 2026-11-02, each with a regression test: the gate check
missed `on_exhausted` targets (002 FR-012), `for_each` task records weren't redacted (003 FR-011),
and a manual retry of `env.deploy` reused the recorded failure (004 FR-009). The user then decided
all 9 open questions (decisions 77-85 in `ARCHITECTURE.md`), and the specs describe the result.
No clarification markers remain.

| Spec | Covers | Open questions |
|---|---|---|
| [001-durable-run-execution](001-durable-run-execution/spec.md) | Run and execution states, leases, fencing, effects, events, routing, loops, budgets, artifacts, audit, journal | 0 |
| [002-pipeline-definition](002-pipeline-definition/spec.md) | YAML format, validation rules, safety checks, templates, schemas, start point, `for_each` | 0 |
| [003-secret-handling](003-secret-handling/spec.md) | Secrets file, redaction, environment scoping, webhook and API auth, pre-commit scan | 0 |
| [004-actions-and-triggers](004-actions-and-triggers/spec.md) | Built-in actions and their effect strategies, webhooks, the poller, Jira polling | 0 |
| [005-agent-execution](005-agent-execution/spec.md) | Mirrors, worktrees, containers, images and proposals, runners, tool limits, costs | 0 |

## Not covered yet

These areas have code but no spec. Nothing below was read closely for this pass.

- The Chat: `assistant.py`, `chat_harness.py`, `conversations.py`, `chat_instructions.py`,
  `vision.py`, `api/assistant.py`
- Learning from runs: `triage.py`, `review.py`, `probe.py`, `lessons.py`, `fixit.py`
- The chat node in depth: `runchat.py`
- Problems and reports: `incidents.py`, `issues.py`, `github_setup.py`
- Chat links: `chatlink/`
- Settings and models: most of `agents.py`, `config.py`, `readiness.py`, `setup.py`,
  `harness_migration.py`, most of `api/settings.py`
- Skills and MCP: `skills.py`, `skill_sources.py`, `mcp.py`, `mcp_detect.py`
- Pipeline tooling: `pipeline/roundtrip.py`, `api/editor.py`, `dryrun.py`, `history.py`, `bundle.py`,
  `templates/`, `live.py`
- Other sources: `schedules.py`, `sources/kafka.py`, `sources/consumer.py`
- Other storage: `ledger/postgres.py`
- Runners: `anthropic.py`, `opencode.py`, `hermes.py`, `function.py`
- The CLI as a whole (`cli/`), beyond the parts the specs cite, and `costs.py`
- The web UI (`web/`), the Mac app (`macos/`), and `demo/`

## How these were checked

- Every file behind a requirement was read in full, except where a spec's Assumptions say otherwise.
- Every code reference was checked by machine against the line it names.
- The behaviours in the open questions marked "a probe confirmed" were reproduced with throwaway
  scripts outside the repository. Those scripts called the real engine, except that `fcntl` was
  stubbed in memory, because the engine imports it and it doesn't exist on Windows.
- With the same stub, the related test suites gave 268 passed, 54 failed and 35 skipped on Windows.
  `tests/core`, `tests/ledger`, `tests/artifacts`, the pipeline tests and the webhook and poller
  tests all passed. The failures were in `tests/actions`, `tests/runners`, `tests/safety` and
  `tests/workspaces`. Those sampled were POSIX-only causes (`.sh` fake binaries, `os.killpg`,
  `os.getuid`, shell commands, Windows paths in test YAML), but not all 54 were examined. They need
  a run on macOS or Linux before they count as evidence. Phase 55 (`docs/phase-55.md`) later
  made the whole fast suite pass on native Windows, with no stub.
- The chaos runs (`demo.chaos`, `demo.e2e`) were not run. Where a success criterion cites one, it is
  quoting `CLAUDE.md`.
