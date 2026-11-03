# Feature Specification: Agent Execution

**Feature Branch**: `N/A (reverse-engineered, no feature branch)`

**Created**: 2026-11-02

**Status**: Draft (as-built, awaiting review)

**Input**: Reverse-engineered from the code at commit `f45743c`. This spec describes how an agent node runs a coding CLI or a model: the checkout it works in, the container around it, the limits it gets, and what happens when it needs a tool the image doesn't have.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Give a coding agent its own branch (Priority: P1)

Each coding step works in its own checkout of the repository on a branch for the run, so its changes never touch the host or another run, and later iterations carry on from the same branch.

**Why this priority**: Isolation is what lets several agents work at once, and the branch is what the next step reviews and ships.

**Independent Test**: Run an agent with a local workspace, then confirm the branch holds its commit and the checkout is gone.

**Acceptance Scenarios**:

1. **Given** a repository configured in `agentline.toml`, **When** a coding node starts, **Then** it gets a worktree from a local mirror on `agentline/<run id>`.
2. **Given** a later iteration or retry, **When** it starts, **Then** it continues the branch from its current head.
3. **Given** an agent leaves uncommitted changes, **When** it finishes, **Then** they are committed unless `autocommit` is false, and the diff is attached.
4. **Given** a node finishes, **When** the worktree is removed, **Then** the branch stays.
5. **Given** a run payload that names a repository not in the configuration, **When** the node starts, **Then** it fails permanently, because a payload can name a repository but never supply a remote.

---

### User Story 2 - Contain the agent (Priority: P1)

A containerised step runs with no host access beyond its checkout and mirror, and the container goes away whatever happens.

**Why this priority**: Effects inside an agent can't be seen by the effect guard, so the boundary around it has to hold.

**Independent Test**: Inspect the `docker run` arguments, then force an error and confirm the container is removed.

**Acceptance Scenarios**:

1. **Given** a container workspace, **When** it starts, **Then** it runs as the given user, with all capabilities dropped, no new privileges, and only the worktree and mirror mounted.
2. **Given** a step fails or is cancelled, **When** it ends, **Then** the container is removed.
3. **Given** an earlier crashed attempt left a container, **When** the node starts again, **Then** the old one is removed first.

---

### User Story 3 - Limit what an agent can do (Priority: P1)

A pipeline says which tools an agent may use, and each runner applies that as closely as its CLI allows.

**Why this priority**: The tool list is the main control on what an agent can do.

**Independent Test**: Inspect a runner's command line for each set of tools.

**Acceptance Scenarios**:

1. **Given** a node with no `allowed_tools`, **When** it runs, **Then** its tools default to read-only.
2. **Given** Claude Code, **When** it runs, **Then** agentline's tool names are mapped to its allow-list.
3. **Given** Codex, which has no per-tool list, **When** a node lacks edit and bash, **Then** it runs in a read-only sandbox, and otherwise in a workspace-write sandbox.
4. **Given** Aider with no edit tool, **When** it runs, **Then** it runs in ask mode with a dry run.

---

### User Story 4 - Stop instead of guessing when a tool is missing (Priority: P1)

When an agent hits a missing command, the node stops for a person with a drafted image change, and nothing is built until they approve it.

**Why this priority**: Retrying in the same image can't help, and letting a model install packages would be unreviewed supply chain.

**Independent Test**: Have a fake agent report a missing tool, then read the needs-review detail.

**Acceptance Scenarios**:

1. **Given** an agent reports `MISSING_TOOL: x` or a shell `not found` error, **When** it ends, **Then** the node goes to needs-review with reason `missing_tools`, and no attempt is spent on a retry.
2. **Given** a known command, **When** the proposal is drafted, **Then** it maps the command to an apt package, deterministically.
3. **Given** an unknown command, **When** the proposal is drafted, **Then** it is listed as needing a person.
4. **Given** a person approves the proposal, **When** it is applied, **Then** the local `agentline.toml` gets the change with a `.bak` copy, the change is audited, the image is built if it can be, and the node is retried.

---

### User Story 5 - Choose the image automatically and cheaply (Priority: P2)

A container gets a suitable image without per-run installs, and a built image is reused until its inputs change.

**Why this priority**: It keeps start-up fast and runs reproducible.

**Independent Test**: Resolve images for repositories with different marker files.

**Acceptance Scenarios**:

1. **Given** an explicit image on the node, **When** it is resolved, **Then** that image is used.
2. **Given** `auto` and no repo setting, **When** it is resolved, **Then** the order is devcontainer image, `Dockerfile.agentline`, marker files, then the base image.
3. **Given** the image's tag already exists, **When** it is needed, **Then** no build runs.

---

### User Story 6 - Know what a run cost (Priority: P2)

Each runner reports what it spent, even when the call failed, and the node's remaining budget is passed to the call.

**Why this priority**: Budgets only work if the costs are real.

**Independent Test**: Have a fake CLI report a cost with an error, then confirm the cost is recorded.

**Acceptance Scenarios**:

1. **Given** a CLI that reports an error and a cost, **When** it ends, **Then** the cost and transcript are kept and the failure is retryable.
2. **Given** no budget left, **When** a runner starts, **Then** it stops for review without calling anything.
3. **Given** a model with no known price, **When** it ends, **Then** the cost is 0, with a warning in the runner's notes.

---

### Edge Cases

- A dict output merges flat over the branch details, with the agent's keys winning. Any other output goes under `agent`.
- Budget is enforced after a CLI ends, by recording the cost, unless `budget_flag: true` passes a limit to Claude Code.
- `max_turns` is enforced for Claude Code only (default 30). Codex and Aider rely on the timeout.
- An edit tool without bash can't be separated on Codex, and both give the same sandbox. Read can't be taken away on Codex or Aider.
- A CLI missing on the host is a permanent failure. A CLI missing in a container is reported as a missing tool.
- A CLI that exits with no parseable result is a retryable failure, with the end of its output in the error.
- With a repair attempt, the node's prompt grows by the schema errors, and both calls are recorded in the transcript.
- A timeout or a cancel kills the whole process group.
- A `shell` runner exit of 126 or 127 with a `not found` message is a missing tool.
- Aider's own commits are off by default, and its cache files are excluded from git, best effort.
- Local workspaces give no isolation beyond their own checkout, and are meant for development and tests.
- Git in a workspace runs with a scrubbed environment, a fixed identity and prompts off.
- Mirrors are cloned once and fetched before use, reconfigured so a fetch never touches `agentline/*` branches and a plain push can't mirror every ref.
- A proposal whose only packages are unknown, or one on the base image, can't be applied, and says to change the image by hand.
- A cost from a harness with no named model is labelled from the runner field, which is empty for harness nodes, so it shows as `None` in the by-model breakdown.

## Requirements *(mandatory)*

### Functional Requirements

**Workspaces**

- **FR-001**: System MUST select the repository from the node, else the only configured one, else the payload's `repo`, and only ever check out a repository configured in `agentline.toml`.
- **FR-002**: System MUST keep one bare mirror per repository, created atomically, and fetched before use.
- **FR-003**: System MUST give each node execution a worktree at a path named by the execution, on `agentline/<run id>`, new from the base branch the first time and continuing the branch after that.
- **FR-004**: `for_each` agent nodes with a workspace share that one path. [NEEDS CLARIFICATION: the worktree path and container name come from the execution id, so parallel tasks of one node collide. The validator allows it. A probe with three parallel tasks had one finish, and two fail once the shared worktree was removed. Should the validator refuse it, should tasks run one at a time, or should each task get its own checkout?]
- **FR-005**: On finishing, System MUST commit leftovers (unless `autocommit` is false), attach the diff against the base commit, and produce the branch artifact: name, commit, base, start commit, repository, mirror, worktree, whether it changed, and whether it auto-committed.
- **FR-006**: System MUST remove the worktree afterwards unless `keep_worktree` is set, and keep the branch.

**Containers**

- **FR-007**: A container MUST run with `--cap-drop ALL`, `no-new-privileges`, `--user` set to the host's uid and gid by default, and only the worktree and mirror mounted.
- **FR-008**: Network MUST default to `bridge`, set by the `network` option. [NEEDS CLARIFICATION: `ARCHITECTURE.md` §11.1 says containers get egress only to their LLM API, and no package-registry access. The code gives normal bridge networking and calls the restriction future work. So an agent can reach any host and install tools at run time. Is this acceptable until egress control is built, and should the architecture doc say so?]
- **FR-009**: Every command MUST run through `docker exec`, with environment names on the command line and the values passed through the docker CLI's own environment.
- **FR-010**: The container MUST be removed in all cases, and any leftover of the same name removed first.
- **FR-011**: `docker_args` MAY add arguments to `docker run`, and the `user` option MAY change the user.

**Images**

- **FR-012**: System MUST resolve images in this order: node image, repo image, repo profile, devcontainer, `Dockerfile.agentline`, marker-file profile, base.
- **FR-013**: Built images MUST be tagged by a hash of their inputs, and building MUST be skipped when the tag exists.
- **FR-014**: The default proposer MUST be deterministic, mapping known commands to apt packages. Another proposer MAY be set.
- **FR-015**: Approving a proposal MUST write only the local `agentline.toml`, keep a backup, audit the change with its diff, attempt the build, then retry the node. If the build can't run, the worker builds on demand at the retry.

**Runners**

- **FR-016**: System MUST provide the runners `python`, `shell`, `anthropic`, `llm`, `claude-code`, `codex`, `aider`, `opencode` and `hermes`. Harness kinds resolve to these.
- **FR-017**: `claude-code` MUST run headless, map tools to its allow-list, default to 30 turns, and restrict MCP to the node's servers.
- **FR-018**: `codex` MUST choose its sandbox from `allowed_tools` unless `sandbox` is set, and cost MUST be tokens times the configured price.
- **FR-019**: `aider` MUST run in one-shot mode with commits off by default, and cost MUST come from its printed session total.
- **FR-020**: `llm` MUST make one direct model call with no tools, for Anthropic or OpenAI-compatible services. HTTP 408, 409, 429 and 5xx MUST be retryable, and other HTTP errors permanent.
- **FR-021**: `shell` MUST pass the prompt on stdin and in `AGENTLINE_PROMPT`, and `python` MUST call a named function.
- **FR-022**: Runners MUST report cost, transcript and metadata on failure as well as success. A runner with no budget left MUST stop before calling anything.
- **FR-023**: Missing tools MUST be detected from `MISSING_TOOL:` lines and shell `not found` errors, and send the node to needs-review with a proposal.
- **FR-024**: Timeouts and cancels MUST kill the process group.

### Key Entities *(include if feature involves data)*

- **Workspace**: The checkout an agent works in, local or in a container.
- **Branch Artifact**: What a coding step hands on, read later by `github.open_pr` and `env.deploy`.
- **Mirror**: The local bare copy of a repository.
- **Image Choice**: The resolved image, where it came from, and whether it needs building.
- **Image Proposal**: A drafted image change, shown to a person, with its packages and the tools it couldn't place.
- **Run Result**: Output, transcript, cost, missing tools and metadata from one runner call.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After any node ends, 0 containers remain from it, including on failure and cancel.
- **SC-002**: A built image's second use triggers 0 builds.
- **SC-003**: A missing tool uses 0 retry attempts, and every one is shown with a proposal or a "needs a person" note.
- **SC-004**: Approving a proposal changes only one table of the local `agentline.toml`, and never the target repository.
- **SC-005**: A failed paid call still records its reported cost in 100% of cases.

## Assumptions

- `runners/opencode.py`, `runners/hermes.py`, `runners/anthropic.py`, `runners/function.py` and the harness tables in `agents.py` weren't read for this pass, so those runners are only named in FR-016. `CLAUDE.md` says opencode and hermes are tested against fake CLIs only.
- No real Claude Code run has been recorded yet (`docs/real-run.md`). Runner behaviour comes from the code and its fake CLIs.
- Skills and MCP are outside this first pass, beyond MCP's effect on the Claude Code command line.
- Docker wasn't exercised for this pass. Container behaviour comes from the code. Its tests failed on this Windows machine because `os.getuid` doesn't exist there, so they gave no evidence either way.

## Differences From the Documentation

- `ARCHITECTURE.md` §11.1 describes `image.propose` as a built-in agent that runs automatically. The code uses a deterministic function when the node stops.
- §11.1's egress proxy does not exist. See FR-008.
- §11.1 says the approved proposal retries the node as `attempt + 1`. The code resolves it with retry, which keeps the attempt number.
- §11.1 says the resolved image digest is recorded so a retry uses the same image, and that images are pinned to digests in the ledger. The code records the image reference and its source, with no digest.

## Code References

| Requirement | Evidence |
|---|---|
| FR-001 | `src/agentline/workspaces/base.py:22` |
| FR-002 | `src/agentline/workspaces/git.py:54` |
| FR-003 | `src/agentline/workspaces/base.py:52`, `src/agentline/workspaces/git.py:98` |
| FR-004 | `src/agentline/workspaces/base.py:52`, `src/agentline/workspaces/container.py:163`, `src/agentline/pipeline/compile.py:271` |
| FR-005 | `src/agentline/workspaces/base.py:86`, `src/agentline/workspaces/__init__.py:20` |
| FR-006 | `src/agentline/workspaces/base.py:65`, `src/agentline/workspaces/git.py:141` |
| FR-007 | `src/agentline/workspaces/container.py:86`, `src/agentline/workspaces/container.py:172` |
| FR-008 | `src/agentline/workspaces/container.py:11`, `src/agentline/workspaces/container.py:173` |
| FR-009 | `src/agentline/workspaces/container.py:65` |
| FR-010 | `src/agentline/workspaces/container.py:164`, `src/agentline/workspaces/container.py:195` |
| FR-011 | `src/agentline/workspaces/container.py:172`, `src/agentline/workspaces/container.py:176` |
| FR-012 | `src/agentline/workspaces/images.py:152` |
| FR-013 | `src/agentline/workspaces/images.py:103`, `src/agentline/workspaces/images.py:189` |
| FR-014 | `src/agentline/workspaces/images.py:265`, `src/agentline/workspaces/images.py:314` |
| FR-015 | `src/agentline/cli/commands.py:450`, `src/agentline/cli/proposal.py:116` |
| FR-016 | `src/agentline/runners/__init__.py:20` |
| FR-017 | `src/agentline/runners/claude_code.py:39`, `src/agentline/runners/claude_code.py:63` |
| FR-018 | `src/agentline/runners/codex.py:56`, `src/agentline/runners/codex.py:176` |
| FR-019 | `src/agentline/runners/aider.py:81`, `src/agentline/runners/aider.py:116` |
| FR-020 | `src/agentline/runners/llm.py:38`, `src/agentline/runners/llm.py:132` |
| FR-021 | `src/agentline/runners/shell.py:37`, `src/agentline/runners/python.py:40` |
| FR-022 | `src/agentline/runners/common.py:40`, `src/agentline/pipeline/compile.py:296`, `src/agentline/runners/claude_code.py:143` |
| FR-023 | `src/agentline/runners/common.py:170`, `src/agentline/pipeline/compile.py:310` |
| FR-024 | `src/agentline/runners/common.py:113`, `src/agentline/actions/_util.py:72` |

Edge-case evidence: `src/agentline/runners/agent_cli.py:70` (missing CLI), `src/agentline/runners/claude_code.py:190` (budget after the run), `src/agentline/workspaces/git.py:26` (mirror refspecs), `src/agentline/cli/proposal.py:85` (proposals that can't apply), `src/agentline/pipeline/compile.py:302` and `src/agentline/costs.py:63` (cost label).

Existing tests: `tests/workspaces/`, `tests/runners/`, `tests/safety/test_budgets_tools.py`, `tests/cli/test_proposal.py`, `tests/ai/test_agent_env.py`, `tests/smoke/test_smoke.py` (needs `AGENTLINE_SMOKE=1`, see `docs/smoke.md`).
