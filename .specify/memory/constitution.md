<!--
Sync Impact Report
- Version change: 1.0.0 → 1.1.0 (MINOR: Principles I and III widened by user decisions 78 and
  81 in ARCHITECTURE.md; 1.0.0 was the first version, the same day)
- Modified principles: I. Durable by Default (a call that failed cleanly is decided by the effect
  advisor), III. Secrets Stay Out of Everything (host commands get declared secrets only)
- Added sections: Core Principles I to VII, Security and Credential Constraints, Development
  Workflow, Governance
- Removed sections: none
- Modelled on: the TaxChat constitution 2.0.0 (structure and tone). Left out on purpose: its
  personal-information rules (this project works with the coding lifecycle and does not handle
  personal information), its tax rules, its conversation-first rule, its Graphify rule and its
  Git Bash on Windows rule. Its secrets rules are kept and widened in Principle III.
- Templates: read at runtime, no changes required
- Deferred TODOs: none
- Conflicts with the code found by the first specs: all fixed on 2026-11-02 (the gate check and
  `on_exhausted`, `for_each` task redaction, `env.deploy` manual retries, and an off-localhost
  `serve` with no token, which now creates one). None open.
- Pending: user review. Dates below are the date of this first draft.
-->

# Agentline Constitution

## Core Principles

### I. Durable by Default (NON-NEGOTIABLE)

- A crash at any point MUST NOT lose a run, repeat a completed step, or duplicate an external side
  effect that has an idempotency key or a check.
- An execution's result and its successor transitions MUST commit in one fenced transaction. A
  worker that lost its lease MUST NOT be able to commit.
- Every change of execution state MUST be checked against the single table of allowed transitions.
  Run status MUST be derived from execution statuses, never set independently, except a cancel.
- Every external side effect MUST go through the effect guard, with its intent recorded before the
  call. Reading is not a side effect.
- The engine MUST NOT claim exactly-once delivery. Where a crash leaves an effect's outcome unknown,
  it MUST apply the `on_unknown` policy, and the default MUST stop for a person.
- A call that failed cleanly, with neither key nor check, MAY be repeated only when the effect
  advisor says it is safe. A deploy-class effect, an unclear answer, or no advisor MUST stop for a
  person.

Rationale: delivery pipelines push code and deploy it. A repeated or lost step there costs more
than a pause, so durability cannot depend on nothing going wrong.

### II. People Decide What Cannot Be Undone

- A deploy-class action MUST pass a gate on every path that can reach it, enforced when the pipeline
  is validated.
- AI MUST NOT decide an unknown outcome on a deploy-class effect.
- Proposed changes from agents, reviews or triage MUST stay proposals until a person applies them.
  This covers pipeline edits, prompts, settings and container images.
- Deploy and test commands MUST come from agentline configuration, never from the target repository.
- Approvals, rejections, resolutions, cancels, retries and deploys MUST write an audit row, naming
  who acted. Except for a deploy, that row MUST commit in the same transaction as the action.

Rationale: the engine exists to let agents do more work unattended, which is only safe if the
irreversible steps stay in human hands and leave a record.

### III. Secrets Stay Out of Everything (NON-NEGOTIABLE)

- Secrets MUST come only from environment variables or the local secrets file, referred to by name.
- Secrets MUST NOT appear in `agentline.toml`, the repository, the ledger, logs, transcripts,
  artifacts, reports, CLI output or API responses. Where a value may have reached any of these, it
  MUST be redacted before it is written.
- The API MUST treat secrets as write-only: it may set, delete and report whether one is set, and
  MUST NOT return a value.
- An agent, container or host command MUST receive only the secrets declared for it, by name, and
  never through command-line arguments.
- Secrets MUST NOT be accepted over chat links or sent to a model.
- Redaction guarantees MUST be tested by attempting to violate them, using fake secret values.

Rationale: agents run with real tokens and produce text that gets stored and shared. A leaked
credential can't be recalled, so the default has to be that it never gets written.

### IV. One Generic Engine

- Nodes MUST be configuration. Behaviour is implemented once per node type, and code is written per
  runner and per action, never per node.
- New capabilities MUST extend the interfaces in the registry (runner, action, waiter, workspace
  provider) instead of adding a special path in the scheduler or worker.
- The engine core MUST stay on the standard library plus `ruamel.yaml`. FastAPI and Pydantic AI
  belong to the `server` extra. Agent frameworks MUST NOT enter the engine.
- Pipelines MUST stay plain YAML files that a person can read and diff. The visual editor MUST read
  and write that same file.
- Prefer deterministic steps over AI nodes. Use a model only where judgment or language is needed.
- Build for the current phase. Abstractions MUST serve a named need, and additional complexity
  MUST be justified in the plan.

Rationale: one executor keeps every pipeline covered by the same durability tests, and adding a
tool stays a small, reviewable change.

### V. Least Privilege for Agents

- Agent tools MUST default to read-only. Edit and shell tools MUST require a workspace.
- Coding agents MUST work in their own checkout, cloned from a local mirror, on a branch for their
  run. Pushing and opening pull requests MUST happen in guarded actions.
- Containers MUST run as non-root with all capabilities dropped and no docker socket, and MUST NOT
  inherit the host environment.
- Paid work MUST check the run and node budgets, and exceeding one MUST stop for a person.
- Values that come from agents or webhooks MUST be treated as untrusted when they reach a shell or
  a repository choice.

Rationale: effects inside an agent can't be seen by the effect guard, so the boundary around the
agent is the control.

### VI. Tested Before Merged

- Every behaviour change MUST have automated tests. Tests MUST use fakes for Jira, GitHub, LLMs and
  chat platforms, and MUST NOT need real credentials.
- Changes to scheduling, leases, effects or the ledger MUST keep the chaos runs clean.
- A bug fix MUST add a test that fails without the fix.
- `pytest` and `ruff` MUST pass before a merge. Web changes MUST also pass `npm test`, `tsc` and the
  build, and Mac changes `swift test`.
- Automatic triage, run reviews and other loops that call a real model MUST stay off in tests.

Rationale: the durability and safety promises above are only as strong as the evidence for them.

### VII. Honest Scope

- Documentation MUST separate what is built from what is designed, and MUST say what was only
  tested against fakes.
- Specs MUST describe behaviour the code has. Anything unclear MUST be marked as a clarification
  rather than guessed.
- A feature MUST NOT be described as working against a real service until it has been run against
  one.
- When code and documentation disagree, the disagreement MUST be recorded and resolved, not left to
  be rediscovered.

Rationale: several integrations are still fake-tested. Knowing exactly which is part of the product.

## Security and Credential Constraints

- Only clearly fake credentials are allowed in the repository, tests, examples and screenshots.
- Secrets files, private keys, local databases and credential files MUST NOT be committed. The
  pre-commit hook enforces this, and skipping it requires a reason.
- The secrets file MUST stay owner-only, and MUST be written atomically.
- Webhooks MUST verify a signature or shared secret with a constant-time compare, and an endpoint
  with no configured secret MUST reject everything.
- The API binds to 127.0.0.1 by default. Exposing it beyond the local machine MUST require a bearer
  token.
- Crash and failure reports stay local and redacted. Sending one to GitHub MUST be something the
  user has set up.
- Verification that writes reports, crashes or data MUST use a throwaway data folder, never the
  user's real one.

## Development Workflow

- Non-trivial features follow the Spec Kit cycle: specify, plan, tasks, implement, with user
  approval of the spec and the plan before moving on. Non-obvious design decisions go to the user.
- Plans MUST include a constitution check and record any justified exception.
- Work happens phase by phase on a `phase-N` branch, merged into `main` with `--no-ff` when done.
  Each phase writes `docs/phase-N.md`.
- User decisions go in the decision log in `ARCHITECTURE.md`, and `CLAUDE.md` stays current with
  every phase. Read `ARCHITECTURE.md` before changing engine behaviour.
- Every GUI action MUST have a matching Chat action, checked against live state.
- The web UI is the only UI. Anything only the Mac app can do goes through the native bridge.
- Commits are small and carry the project's attribution trailer.

## Governance

This constitution takes precedence over other project practices. Amendments require a documented
reason and user approval. Versions follow semantic versioning: MAJOR for removing or redefining a
principle, MINOR for adding or materially expanding one, PATCH for clarifications. Every plan and
review MUST verify compliance, and Principles I and III have no exceptions.

**Version**: 1.1.0 | **Ratified**: 2026-11-02 | **Last Amended**: 2026-11-02
