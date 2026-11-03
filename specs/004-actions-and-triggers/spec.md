# Feature Specification: Actions and Triggers

**Feature Branch**: `N/A (reverse-engineered, no feature branch)`

**Created**: 2026-11-02

**Status**: Draft (as-built, awaiting review)

**Input**: Reverse-engineered from the code at commit `f45743c`. This spec describes the built-in actions and how each keeps a retry from repeating its side effect, then how the outside world starts runs and resumes waits: webhooks, the poller and Jira polling.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start a run from a ticket assignment (Priority: P1)

Assigning a Jira ticket starts the matching pipeline once, whether agentline hears about it from a webhook or by polling.

**Why this priority**: This is how the target workflow begins.

**Independent Test**: Post a signed Jira assignment twice and confirm one run exists.

**Acceptance Scenarios**:

1. **Given** a Jira assignment event with a valid secret, **When** it matches a pipeline's trigger and filter, **Then** a run starts and the response is 202 with its id.
2. **Given** the same event again, **When** it arrives, **Then** no new run starts and the response is 200 `duplicate`.
3. **Given** an event that isn't an assignment, or that no pipeline matches, **When** it arrives, **Then** the response is 200 `ignored`.
4. **Given** a Jira poll trigger, **When** a ticket was assigned before polling was first enabled, **Then** it doesn't start a run.

---

### User Story 2 - Resume on a code review (Priority: P1)

A GitHub review on a pull request resumes the run waiting for it, once, even if the webhook is missed or arrives twice.

**Why this priority**: The review loop is the centre of the delivery pipeline.

**Independent Test**: Deliver a signed review event and confirm the wait resumes. Redeliver it and confirm a 409.

**Acceptance Scenarios**:

1. **Given** an approved or changes-requested review, **When** its signed webhook arrives, **Then** the wait on that pull request resumes with the review's state and comments.
2. **Given** a commented or dismissed review, **When** it arrives, **Then** it is ignored.
3. **Given** a webhook redelivered with the same delivery id, **When** it arrives, **Then** it gets 409.
4. **Given** the poller already delivered a review, **When** the webhook for it arrives under a new delivery id, **Then** it gets 200 `duplicate` and the next iteration's wait isn't resumed by it.
5. **Given** a wait with `poll_every` and a missed webhook, **When** the interval passes, **Then** the poller delivers the latest decisive review of the pull request's head commit.

---

### User Story 3 - Open a pull request once (Priority: P1)

A coding step's branch is pushed and its pull request is opened once, and later loop iterations reuse the same pull request.

**Why this priority**: A crash here must not leave duplicate pull requests.

**Independent Test**: Crash after the pull request is created but before the result is saved, then confirm the retry finds it.

**Acceptance Scenarios**:

1. **Given** a branch input with a mirror and a commit, **When** the action runs, **Then** that commit is pushed to the branch on the configured remote.
2. **Given** a pull request already open for the branch, **When** the action runs, **Then** it is reused.
3. **Given** a crash after creation, **When** the step runs again, **Then** the check finds the pull request.
4. **Given** someone opens the pull request between lookup and create, **When** the create gets a 422, **Then** the action uses the one that exists.

---

### User Story 4 - Deploy with a record of who approved it (Priority: P1)

A deploy runs a command from agentline's own configuration, and each one leaves an audit row naming the approver, the commit and the environment.

**Why this priority**: Deploying is the highest-risk action, and the target repository must not be able to change what it runs.

**Independent Test**: Run a pipeline through a gate to a deploy, then read the audit log.

**Acceptance Scenarios**:

1. **Given** an environment with a deploy command, **When** the action runs, **Then** `{branch}`, `{sha}` and `{env}` are substituted shell-quoted.
2. **Given** a deploy that exits non-zero, **When** the action ends, **Then** it raises, with the exit code and an output tail.
3. **Given** an environment with a `check` command, **When** a crash left the deploy's outcome unknown, **Then** the check decides, by comparing the deployed commit.
4. **Given** no check command, **When** a crash left the outcome unknown, **Then** the default policy stops the node for a person.
5. **Given** a failing test run from `env.test`, **When** it ends, **Then** it is a result with `passed: false`, not an error.

---

### User Story 5 - Run a custom command (Priority: P2)

A pipeline can run a script on the host, pass it the node's inputs and use its JSON output.

**Why this priority**: The project prefers deterministic script steps over AI nodes where they will do.

**Independent Test**: Run a `shell.run` node whose script reads `AGENTLINE_INPUTS` and prints JSON.

**Acceptance Scenarios**:

1. **Given** `output: json` and JSON on stdout, **When** the command exits 0, **Then** a mapping is merged into the result, and anything else goes under `value`.
2. **Given** a non-zero exit, **When** the command ends, **Then** it raises, and with `permanent` it fails with no retries.
3. **Given** a crash mid-command, **When** the node runs again, **Then** the outcome is unknown and the default policy stops for a person, unless the node sets `idempotent: true`.
4. **Given** a manual retry, **When** the node runs again, **Then** the command really runs again.

---

### Edge Cases

- A poller scan handles only waits with `poll_every` and a waiter that has a `poll` function. A failed poll is logged and tried at the next interval.
- A poll waiter is only called once per `poll_every` for each execution, and the state is held in memory, so a restart polls again at once.
- With no head commit on the `pr` input, the poller only counts reviews submitted since the wait was created.
- The GitHub webhook can fetch a review's line comments when a token is set. If that fails, the review body alone resumes the wait.
- A `ping` event gets `pong`, and other event types are ignored, both after the signature check.
- Replay claims commit in the same transaction as the event, so a failed delivery can be retried by the sender.
- `jira.comment` adds a visible last line with the run, node and iteration, because Jira has no hidden text. After a crash, the check looks for that line.
- `github.merge_pr` merges the commit that was reviewed, by default, so a head that moved since isn't merged. After a crash, an already-merged pull request counts as done.
- `jira.get_ticket`, `github.read_reviews` and `env.test` have no effect guard, because repeating them is harmless.
- `env.deploy` runs in the environment's `cwd`, else a throwaway worktree of the commit, else the current folder.
- An unknown key in a Jira webhook trigger's filter stops the server from starting.
- A second copy of a push with the same commit is a no-op, so the push effect relies on its key.
- Jira polling keeps its start time per pipeline in `$AGENTLINE_HOME/state/jira_poll.json`, and it keeps polling after errors.
- A poll trigger's `filter.jql` replaces the query, and `filter.project` narrows it. Other filter keys that the webhook honours, such as `issue_type` and `labels`, are ignored by polling.

## Requirements *(mandatory)*

### Functional Requirements

**Actions**

- **FR-001**: System MUST register these built-in actions: `jira.get_ticket`, `jira.comment`, `github.open_pr`, `github.read_reviews`, `github.merge_pr`, `shell.run`, `env.deploy`, `env.test`, and the `github.review` waiter.
- **FR-002**: `github.open_pr` MUST push under a key built from the repo, branch and commit, then find or create the pull request by head branch under a check.
- **FR-003**: `jira.comment` MUST use a marker in the comment body as its check.
- **FR-004**: `github.merge_pr` MUST use a read of the pull request as its check, and accept only `squash`, `merge` or `rebase`.
- **FR-005**: `shell.run` MUST name its effect per try, including manual retries, so a finished command runs again on any retry, while a crash mid-command is unknown.
- **FR-006**: `env.deploy` and `env.test` MUST take their commands from `[environments.<env>]`, and shell-quote substituted values because branch names come from agents. Commands see only the variables the env lists in `pass_env` (see `003-secret-handling`, FR-014).
- **FR-007**: `env.deploy` MUST be deploy-class, and MUST refuse `on_unknown: ai`.
- **FR-008**: `env.deploy` MUST write one `deploy` audit row per deploy effect, with environment, commit, branch, exit code, approver and gate.
- **FR-009**: `env.deploy` MUST name its effect per try, including manual retries (`deploy#1`, then `deploy#r1.1` after one retry), so a manual retry deploys again, and each deploy is audited under that name. (Until 2026-11-02 it used the attempt number, and a manual retry reused the recorded failure; fixed, with `test_manual_retry_redeploys`.)
- **FR-010**: Clients MUST be built from the environment when an action runs, so registering needs no tokens. A missing credential MUST be a permanent failure.
- **FR-011**: HTTP 5xx, 408, 429 and network failures MUST be retryable, and other 4xx responses permanent.

**Webhooks**

- **FR-012**: `/webhooks/github` MUST resume waits only on `pull_request_review` events with action `submitted` and state approved or changes_requested, keyed by repository and pull request.
- **FR-013**: System MUST reject a repeated GitHub delivery id with 409, and deliver each review at most once across webhook and poller, by its event id.
- **FR-014**: `/webhooks/jira` MUST recognise assignment events, match every pipeline with a Jira `issue_assigned` webhook trigger, and apply its `project`, `assignee`, `issue_type` and `labels` filters.
- **FR-015**: A Jira assignment's dedupe key MUST include the issue, assignee and a change marker, and also the pipeline name when more than one Jira webhook pipeline exists.

**Polling**

- **FR-016**: The poller MUST run at the waits' `poll_every` and deliver a result as a webhook would.
- **FR-017**: Jira polling MUST start runs only for assignments to the token's account made after polling was first enabled for that pipeline, using the same dedupe key shape as the webhook. The key has no pipeline name, so when several poll pipelines match one assignment only the first starts a run. That is intended (decision 83).
- **FR-018**: Poll triggers MUST NOT run more often than every 30 s (default 300 s), and MUST follow pipelines added or removed by a live reload.

### Key Entities *(include if feature involves data)*

- **Action**: A named, reusable integration with an effect strategy and a deploy-class flag.
- **Waiter**: Names the correlation key a wait parks on, and optionally polls for its event.
- **Delivery Record**: A seen delivery or event id, used to reject replays.
- **Review Event**: The payload a review wait resumes with, the same from webhook and poller.
- **Poll State**: When polling was first enabled, per pipeline.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `demo.e2e.run --tickets 10 --expect-clean` passes under kill-and-restart chaos, with signed webhooks and redelivery. (Reported in `CLAUDE.md`; this pass did not run it.)
- **SC-002**: A webhook redelivery starts 0 extra runs and resumes 0 extra waits.
- **SC-003**: Each pull request is created at most once per branch across crashes and iterations.
- **SC-004**: Each completed deploy has exactly 1 audit row naming its approver, where a gate preceded it.

## Assumptions

- Jira and GitHub are the only ticket and code hosts. Their clients were read for their error rules only, and tests use fakes. `CLAUDE.md` records that the real Claude Code run is still to do.
- Schedules (`schedules.py`) and the Kafka source are triggers too. They are outside this first pass, as are the `chat.ask` and `chat.send` steps.
- The live-reload machinery is only described where triggers follow it.

## Differences From the Documentation

- `ARCHITECTURE.md` §2 lists `image.build` and `image.propose` as actions. They are not registered actions. Image proposals are built into the agent node and the CLI (see `005-agent-execution`).
- The action table in `src/agentline/actions/__init__.py:3` omits `github.merge_pr`, added in phase 18.

## Code References

| Requirement | Evidence |
|---|---|
| FR-001 | `src/agentline/actions/__init__.py:26` |
| FR-002 | `src/agentline/actions/github.py:101`, `src/agentline/actions/github.py:128`, `src/agentline/actions/github.py:148` |
| FR-003 | `src/agentline/actions/jira.py:18`, `src/agentline/actions/jira.py:70` |
| FR-004 | `src/agentline/actions/github.py:259`, `src/agentline/actions/github.py:265` |
| FR-005 | `src/agentline/actions/shell.py:94`, `src/agentline/context.py:90` |
| FR-006 | `src/agentline/actions/env.py:31`, `src/agentline/actions/env.py:108` |
| FR-007 | `src/agentline/actions/env.py:100`, `src/agentline/actions/env.py:151`, `src/agentline/context.py:246` |
| FR-008 | `src/agentline/actions/env.py:186` |
| FR-009 | `src/agentline/actions/env.py:146`, `src/agentline/scheduler.py:393`, `src/agentline/context.py:252` |
| FR-010 | `src/agentline/actions/__init__.py:13`, `src/agentline/integrations/http.py:32` |
| FR-011 | `src/agentline/integrations/http.py:98`, `src/agentline/integrations/http.py:100` |
| FR-012 | `src/agentline/api/webhooks.py:196`, `src/agentline/api/webhooks.py:206`, `src/agentline/actions/github.py:297` |
| FR-013 | `src/agentline/api/webhooks.py:226`, `src/agentline/deliveries.py:30`, `src/agentline/actions/github.py:191` |
| FR-014 | `src/agentline/api/webhooks.py:75`, `src/agentline/api/webhooks.py:119`, `src/agentline/api/webhooks.py:165` |
| FR-015 | `src/agentline/api/webhooks.py:259`, `src/agentline/api/webhooks.py:267` |
| FR-016 | `src/agentline/poller.py:61` |
| FR-017 | `src/agentline/sources/jira_poll.py:94`, `src/agentline/sources/jira_poll.py:112`, `src/agentline/scheduler.py:76` |
| FR-018 | `src/agentline/pipeline/spec.py:453`, `src/agentline/sources/jira_poll.py:155` |

Edge-case evidence: `src/agentline/actions/env.py:61` (deploy working folder), `src/agentline/actions/github.py:313` (poll rules), `src/agentline/api/webhooks.py:168` (unknown filter keys), `src/agentline/sources/jira_poll.py:82` (poll query).

Existing tests: `tests/actions/test_jira_github.py`, `tests/actions/test_shell_env.py`, `tests/actions/test_clients.py`, `tests/api/test_webhooks.py`, `tests/api/test_poller.py`, `tests/e2e/test_e2e.py`.
