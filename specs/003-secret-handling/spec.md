# Feature Specification: Secret Handling

**Feature Branch**: `N/A (reverse-engineered, no feature branch)`

**Created**: 2026-11-02

**Status**: Draft (as-built, awaiting review)

**Input**: Reverse-engineered from the code at commit `f45743c`. This spec describes how agentline stores credentials, keeps them out of what it writes, passes them to agents, and guards its own entry points. The pre-commit scan described here is on the `secret-scan` branch, which isn't merged to `main` yet.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Store an API key safely (Priority: P1)

A person enters a key in the app or sets it in their shell. The key lands in a private local file or stays in the environment, and it can never be read back through the app.

**Why this priority**: Every integration needs a key, so this is the first thing a user trusts agentline with.

**Independent Test**: Set a key through the API, confirm the file is owner-only, confirm the API reports it as set, and confirm no response holds the value.

**Acceptance Scenarios**:

1. **Given** a key entered through the API, **When** it is saved, **Then** it goes to the secrets file, into the running process's environment, and into the redactor.
2. **Given** a variable already set in the shell, **When** the secrets file holds the same name, **Then** the shell value wins.
3. **Given** a request for a key's status, **When** it is answered, **Then** it says whether the key is set and where from, and never the value.
4. **Given** a key value with a line break, or an empty value, **When** it is saved, **Then** it is refused.
5. **Given** a key in the wrong place, **When** settings are saved with one, **Then** the settings API refuses it and says keys go through the secrets API.

---

### User Story 2 - Keep secrets out of everything written down (Priority: P1)

Anything agentline writes, such as logs, results, errors, transcripts, audit rows and CLI output, has known secret values replaced by a labelled marker first.

**Why this priority**: Agents echo what they see, and stored text gets shared. A leak can't be taken back.

**Independent Test**: Run a pipeline with a fake secret in its input and output, then search the ledger, the files, the logs and the output for it.

**Acceptance Scenarios**:

1. **Given** a known secret in a step's result, **When** the result is stored, **Then** it reads `[REDACTED:<NAME>]`, and successors see that text.
2. **Given** a known secret in an exception, **When** it is logged, **Then** the message and the traceback are both redacted.
3. **Given** a secret added while the engine is running, **When** later text is written, **Then** it is redacted too.
4. **Given** a secret value in a run's payload, **When** the run is created, **Then** the stored payload is redacted.

---

### User Story 3 - Give an agent only the keys it needs (Priority: P1)

An agent runs with a scrubbed environment and receives only the variables declared for it. A container gets those variables by name, so their values never appear in a command line.

**Why this priority**: Agents run untrusted output through tools, so a key they never receive can't leak.

**Independent Test**: Run a runner with extra variables on the host, then confirm they don't reach it.

**Acceptance Scenarios**:

1. **Given** a local agent CLI, **When** it starts, **Then** its environment holds only a short safe list plus its declared names.
2. **Given** a provider's key under a custom name, **When** the agent starts, **Then** the key reaches it under the name the CLI expects.
3. **Given** a container workspace with declared secrets, **When** it starts, **Then** the command line holds only the names.

---

### User Story 4 - Refuse forged requests (Priority: P1)

Inbound webhooks need a valid signature or shared secret, and the API needs a bearer token when one is set.

**Why this priority**: A forged webhook could start a run or resume a wait.

**Independent Test**: Post a webhook with a bad signature, then with a good one.

**Acceptance Scenarios**:

1. **Given** a GitHub webhook with a bad or missing signature, **When** it arrives, **Then** it gets 401.
2. **Given** a webhook endpoint with no secret configured, **When** anything arrives, **Then** it gets 401.
3. **Given** an API token is set, **When** an `/api` request has a wrong or missing token, **Then** it gets 401.

---

### User Story 5 - Stop secrets at commit time (Priority: P2)

A developer who stages a credential file, or a line that looks like a real key, has the commit refused. The hook names the file and never prints the match.

**Why this priority**: It is the last check before something leaves the machine.

**Independent Test**: Stage a file with a fake key shape, then try to commit it.

**Acceptance Scenarios**:

1. **Given** a staged `.env`, `secrets.env`, private key or similar, **When** committing, **Then** the commit is refused, and `*.env.example` is allowed.
2. **Given** an added line matching a known key format, **When** committing, **Then** the commit is refused, naming the file.
3. **Given** a change under `tests/` or `macos/Tests/`, **When** committing, **Then** its contents aren't scanned, because those folders hold made-up keys on purpose.

---

### Edge Cases

- Matching is exact on the literal value. A secret that was encoded, hashed or split across lines is not caught.
- Values shorter than 8 characters are not redacted, because they would match too much.
- Environment variables only count as secrets when their names end in `_TOKEN`, `_SECRET`, `_KEY` or `_PASSWORD`, or are on a short list. Anything in the secrets file counts, whatever its name.
- Deleting a key removes it from the file and the environment, but the redactor keeps it.
- A value in the environment that differs from the file is reported as coming from the environment.
- Editing the secrets file from outside the process is picked up by the live reload. A variable the file set follows the file, and one set in the shell still wins.
- Integration HTTP clients and action output tails use a second, simpler scrubber that writes `***`, for values of 4 or more characters. It covers four named variables in action tails, and each client's own secrets.
- `AGENTLINE_INPUTS` for `shell.run` is written to a temp file holding the node's inputs, and removed afterwards.
- MCP config files for Claude Code are written owner-only, inside the workspace or a temp file, and removed after the run.
- The attachments route is outside the bearer check so that image tags work. Its ids are content hashes, and only images are stored.
- The webhook routes and `/healthz` have their own rules and don't use the bearer token.
- Secrets file writes use `os.fchmod`, which isn't available on Windows with this Python, so saving a key is POSIX-only.

## Requirements *(mandatory)*

### Functional Requirements

**Storage**

- **FR-001**: System MUST keep keys only in environment variables or `$AGENTLINE_HOME/secrets.env` (default `~/.agentline/secrets.env`), as `NAME=value` lines, with names in `A-Z`, `0-9` and `_`.
- **FR-002**: The secrets file MUST be written atomically, with owner-only permissions on the file (0600) and on a folder it creates (0700).
- **FR-003**: Loading the file MUST NOT override variables already set in the environment.
- **FR-004**: The API MUST allow setting and deleting a key and reporting its status, and MUST NOT return a value.
- **FR-005**: Settings writes MUST refuse a table with an `api_key` or `key` field, so keys can't land in `agentline.toml` that way. MCP secrets go to the secrets file, with `${KEY}` in `agentline.toml`.

**Redaction**

- **FR-006**: System MUST replace every known secret value with `[REDACTED:<NAME>]`, matching the longest first, in text, bytes and JSON-like values, including dictionary keys.
- **FR-007**: The redactor MUST learn secrets from the environment at startup, from the secrets file, and from any set while running.
- **FR-008**: System MUST redact run payloads, step results, execution details, errors, effect results, delivered events, attachments, audit rows and run messages before writing them.
- **FR-009**: Log output MUST be redacted, including exception text, on the console handler and on the in-memory lines that the Logs page reads.
- **FR-010**: CLI output and crash reports MUST pass through the redactor.
- **FR-011**: `for_each` task records (item, result, error) and task journal lines MUST be redacted before they are written. A task's stored result is the redacted value, which is also what a retry reuses. (Until 2026-11-02 these were written raw; fixed, with `test_task_records_and_journal_are_redacted`.)

**Agents and subprocesses**

- **FR-012**: A local agent subprocess MUST receive only the safe list (`PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TERM`, `SHELL`, `TZ`) plus names declared for it or mapped by the harness.
- **FR-013**: A container MUST receive declared secrets by name only, with no host environment and no docker socket.
- **FR-014**: `shell.run`, `env.deploy` and `env.test` commands run on the host with its full environment, plus any `env` the node sets. [NEEDS CLARIFICATION: unlike runners, these actions inherit every secret in the process. `ARCHITECTURE.md` §10 only promises scoping for containers, and an operator's deploy script may need those variables. Is full inheritance intended for `shell.run`, whose command comes from a pipeline file?]

**Entry points**

- **FR-015**: `/api` routes MUST require the bearer token when one is set, compared in constant time. With no token set the API is open, and the default bind address is 127.0.0.1. [NEEDS CLARIFICATION: `serve --host 0.0.0.0` with no token starts normally, which leaves the secrets API and every run action open to the network. Should `serve` refuse a non-local bind with no token?]
- **FR-016**: GitHub webhooks MUST verify an HMAC-SHA256 signature over the raw body, and Jira webhooks a shared secret by header or query string, both in constant time. An unset secret MUST reject everything.

**Commit time**

- **FR-017**: The pre-commit hook MUST refuse staged credential files, and added lines matching known key formats outside the test folders, naming the file and not the match.

### Key Entities *(include if feature involves data)*

- **Secret**: A named value, held in the environment or the secrets file, never in configuration.
- **Redactor**: The set of known secret values and the labels they are replaced with.
- **Declared Environment**: The named variables a runner or workspace may pass on.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Across the ledger, files, logs and CLI output, a fake secret appears 0 times after a run, as `test_secret_never_reaches_ledger_files_logs_or_output` and, for `for_each` tasks, `test_task_records_and_journal_are_redacted` check.
- **SC-002**: 0 API responses contain a key's value.
- **SC-003**: 100% of webhook requests with a bad signature or secret get 401.
- **SC-004**: A staged credential file is refused in 100% of commits with the hook enabled.

## Assumptions

- The user said this project works with the coding lifecycle and most likely has no personal information, so this spec covers credentials only.
- The hook is opt-in per clone, through `core.hooksPath`, and `--no-verify` skips it.
- `triage.py`, `review.py`, `assistant.py`, `chat_harness.py`, `probe.py` and `sources/kafka.py` also call the redactor, but those modules weren't read for this pass. The Chat's handling of keys is outside this spec.
- The Mac app keeps keys in the same secrets file (phase 22). Its Swift code wasn't read.

## Differences From the Documentation

- `ARCHITECTURE.md` §10 says the redactor scrubs everything written. The second scrubber shows that it is not one single path, and FR-011 was a gap until 2026-11-02.
- Decision 30 mentions the Keychain, which decision 60 replaced. `api/settings.py:492` still has a `managed_check` that does nothing.

## Code References

| Requirement | Evidence |
|---|---|
| FR-001 | `src/agentline/secrets.py:21`, `src/agentline/secrets.py:24` |
| FR-002 | `src/agentline/secrets.py:50` |
| FR-003 | `src/agentline/secrets.py:94` |
| FR-004 | `src/agentline/api/settings.py:494`, `src/agentline/api/settings.py:503`, `src/agentline/secrets.py:118` |
| FR-005 | `src/agentline/api/settings.py:358` |
| FR-006 | `src/agentline/redact.py:62`, `src/agentline/redact.py:93` |
| FR-007 | `src/agentline/app.py:115`, `src/agentline/secrets.py:136`, `src/agentline/redact.py:41` |
| FR-008 | `src/agentline/scheduler.py:80`, `src/agentline/scheduler.py:118`, `src/agentline/scheduler.py:156`, `src/agentline/scheduler.py:284`, `src/agentline/context.py:105`, `src/agentline/context.py:326`, `src/agentline/app.py:402`, `src/agentline/runchat.py:113` |
| FR-009 | `src/agentline/logs.py:64`, `src/agentline/logs.py:149`, `src/agentline/logs.py:155` |
| FR-010 | `src/agentline/cli/env.py:46`, `src/agentline/incidents.py:119` |
| FR-011 | `src/agentline/pipeline/compile.py:140`, `src/agentline/pipeline/compile.py:172`, `src/agentline/pipeline/compile.py:182` |
| FR-012 | `src/agentline/runners/common.py:18`, `src/agentline/runners/common.py:71`, `src/agentline/runners/agent_cli.py:58` |
| FR-013 | `src/agentline/workspaces/container.py:122`, `src/agentline/workspaces/container.py:178` |
| FR-014 | `src/agentline/actions/_util.py:48`, `src/agentline/actions/shell.py:64` |
| FR-015 | `src/agentline/api/server.py:54`, `src/agentline/api/server.py:87`, `src/agentline/cli/__init__.py:97` |
| FR-016 | `src/agentline/api/webhooks.py:44`, `src/agentline/api/webhooks.py:51`, `src/agentline/api/webhooks.py:245` |
| FR-017 | `.githooks/pre-commit:17`, `.githooks/pre-commit:29` |

Edge-case evidence: `src/agentline/redact.py:20` (minimum length), `src/agentline/redact.py:21` (name rule), `src/agentline/secrets.py:81` (delete), `src/agentline/secrets.py:103` (sync), `src/agentline/integrations/http.py:36` and `src/agentline/actions/_util.py:21` (second scrubber), `src/agentline/runners/claude_code.py:112` (MCP config), `src/agentline/api/assistant.py:183` (attachments), `src/agentline/api/server.py:111` (no bearer on attachments).

Existing tests: `tests/safety/test_redaction.py`, `tests/ai/test_agent_env.py`, `tests/ai/test_settings_api.py`, `tests/api/test_webhooks.py`, `tests/test_precommit_hook.py`.
