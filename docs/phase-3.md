# Phase 3: runners and workspaces

Status: built on branch `phase-3`; not merged.

## What was built

**Runners** (`src/agentline/runners/`, registered in `runners/__init__.py:register()`):

| Runner | Module | What it does |
|---|---|---|
| `python` | `python.py` | `with: { function: "pkg.module:fn" }`; calls your sync or async function with the `RunRequest`. It may return a `RunResult` or a plain output. Sync functions run in a thread. |
| `shell` | `shell.py` | `with: { cmd: [...] \| "...", allow_failure, env: {K: v}, pass_env: [K] }`. Runs in the workspace when there is one, else locally. The prompt goes in on stdin and in `AGENTLINE_PROMPT` (also `AGENTLINE_INPUTS`, `AGENTLINE_RUN_ID`, `AGENTLINE_NODE_ID`, `AGENTLINE_ITERATION`). Output is stdout parsed as JSON, else `{"stdout", "exit_code"}`. A non-zero exit raises `RuntimeError` (retryable) unless `allow_failure`. |
| `anthropic` | `anthropic.py` | One Messages API call over stdlib `urllib` (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`). The default model is `claude-sonnet-5-5`. Cost comes from `usage` and a price table, and no prices are hard-coded. |
| `claude-code` | `claude_code.py` | Headless `claude -p <prompt> --output-format json --max-turns N --allowedTools ...`, run inside the workspace (in the container) or locally. |

Shared helpers are in `runners/common.py`:
- `run_process`: an async subprocess. On timeout or cancellation it kills the whole process group.
- `base_env` and `declared_env`: env hygiene.
- `parse_json_text`: parses JSON, a fenced JSON block, or a trailing ```` ```json ```` block.
- `detect_missing_tools`: finds `MISSING_TOOL: x`, `x: command not found`, `sh: 1: x: not found`, `zsh: command not found: x`, and docker's `exec: "x": executable file not found`.
- `workspace_exec`: passes stdin only if the workspace's `exec` accepts `input=`.
- `RunnerError`: a retryable failure that carries `cost_usd`, `transcript` and `meta`.

**Workspaces** (`src/agentline/workspaces/`, registered in `workspaces/__init__.py:register()`):

| Module | What it does |
|---|---|
| `git.py` | `ensure_mirror(repo, root)` clones the mirror (`git clone --mirror`) or fetches it. `create_worktree` makes a worktree on `agentline/<run_id>`. On iteration 2+ (or a retry) it continues the existing branch from its head. Also: leftover auto-commit, diff, removal. |
| `base.py` | Repo selection, the checkout lifecycle, and `GitWorkspace.finalize()`. |
| `local.py` | Provider `local`: a worktree with no container, and `exec` runs subprocesses there. For development and tests. |
| `container.py` | Provider `container`: one container per node execution. Commands run through `docker exec`, and `docker rm -f` runs in `finally`. |
| `images.py` | Image resolution (§11.1), content-hash tags, the idempotent `build_image()`, and the deterministic `propose_image_change()` behind a swappable `set_proposer()` seam. |

**Engine glue** (`pipeline/compile.py`, inside `agent_handler`, plus 2 imports):
- On success, when the node has a workspace, the output is `{**finalize(), "agent": output}`.
  If the output is a dict with `name`, it is `{**finalize(), **output}` instead.
- `missing_tools` → `NeedsReview("missing_tools", {..., "image_proposal": {...}})`.
- `RunnerError` → its transcript is attached and its cost is spent before re-raising, so a
  failed paid call still counts against the budget.

## Contracts

### Runner
`Runner.run(RunRequest) -> RunResult` (unchanged from `registry.py`). A runner:
- reads its own config from `request.params` (`with:`, after templating) and limits from
  `request.limits` (`allowed_tools`, `max_turns`, `budget_usd` = what is left, `timeout`);
- runs inside `request.workspace` via `workspace.exec` when there is one;
- returns `missing_tools` instead of failing when a needed command is absent. The engine
  then parks the node with an image proposal;
- raises `PermanentError` for "retrying can't help" (bad config, missing binary, HTTP 4xx).
  It raises `RuntimeError` or `RunnerError` for retryable failures, and `BudgetExceeded` to refuse
  a call when `budget_usd == 0`;
- never logs or returns secret values.

Adding Codex or Aider means one module with a `run()` plus one line in `register()`.

### Workspace
`Workspace.path` is the checkout as the agent sees it: the worktree for `local`, and
`/workspace` for `container`. `exec(cmd, *, env, timeout, input=None) -> (code, out, err)`.
A missing executable returns 127. The built-in workspaces also have:
- `finalize() -> dict`: the **branch artifact**. It commits leftovers (unless
  `autocommit: false`) and attaches `git diff base_sha..sha` as `diff`.
- `image_profile` and `markers`, which the image proposer reads.

**Branch artifact (Phase 4 contract)**, the `out` of an implement node with a workspace:

```json
{
  "name": "agentline/<run_id>",
  "sha": "<head>",
  "base_sha": "<merge-base with the default branch>",
  "repo": "<repo name in agentline.toml>",
  "mirror": "/…/.agentline/mirrors/<repo>.git",
  "worktree": "/…/.agentline/worktrees/<repo>/<execution id>",
  "start_sha": "<head when this execution started>",
  "changed": true,
  "autocommitted": false,
  "agent": "<runner output, when it isn't a dict with name>"
}
```

`github.open_pr` should push from `mirror` (`git -C <mirror> push origin <name>`). The
worktree directory is **removed** when the node finishes, unless `keep_worktree: true`. The
branch stays in the mirror.

### Node `workspace:` options
`type` (`local` | `container`), `image` (default `auto`), `repo`, `base` (start branch,
default the repo's `default_branch`), `secrets` (host env var names passed in), `autocommit`,
`keep_worktree`. These are container only: `network` (default `bridge`), `user` (`uid:gid`,
default yours), `docker_args`, `docker` (CLI path).

Repo selection: `workspace.repo`, else the single repo in config, else the payload's `repo`.
That value must name a configured repo.

## As-built decisions (proposals for the decision log)

- **P3-1 The mirror is reconfigured after `clone --mirror`.** `remote.origin.mirror` is unset,
  so a plain `git push origin <branch>` from the mirror can never mirror-push or delete every
  remote ref. The fetch refspec becomes `+refs/heads/*:refs/heads/*`,
  `^refs/heads/agentline/*` and `+refs/tags/*:refs/tags/*`, with no prune. As a result, a fetch
  never overwrites or deletes the local `agentline/*` branches agents commit to, even after
  they have been pushed. The cost: branches deleted upstream stay in the mirror.
- **P3-2 One worktree per execution, removed afterwards; the branch is kept.** Path:
  `$AGENTLINE_HOME/worktrees/<repo>/<execution id>`. A stale worktree from a crashed
  attempt that still holds the branch is force-removed before the new one is added.
- **P3-3 `base_sha` is the merge-base with the default branch**, so on iteration 2+ the diff
  and `base_sha` describe the whole branch (PR-like), not just the latest iteration.
  `start_sha` gives the per-iteration starting point.
- **P3-4 Leftover changes are auto-committed by `finalize()`** (`agentline: uncommitted
  changes from <node>#<iteration>`), so an agent that forgets to commit doesn't lose work.
  `autocommit: false` turns this off. Commits use the identity `agentline <agentline@localhost>`
  through `GIT_*` env vars.
- **P3-5 The container mounts the mirror at its host path** as well as the worktree at
  `/workspace`, because a worktree's `.git` file points into `<mirror>/worktrees/`. Without
  this mount, git inside the container doesn't work.
- **P3-6 Container hardening defaults**: `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--user <host uid>:<gid>`, `HOME=/tmp/agentline-home`, no docker socket, labels
  `agentline.run` and `agentline.execution`. Secrets go in as `-e NAME` (value only in the docker
  CLI's env), so values never appear in argv or `ps`.
- **P3-7 Env hygiene everywhere.** Local subprocesses only see PATH, HOME, USER, the locale
  variables, TMPDIR, TERM, SHELL and TZ, plus the env vars that are declared (`env:` for
  claude-code, default `[ANTHROPIC_API_KEY]`; `pass_env:` for shell; `secrets:` for workspaces).
- **P3-8 Prices come from config, never code.** The `anthropic` runner reads
  `with: {input_usd_per_mtok, output_usd_per_mtok}` or `[pricing.<model>] input/output`.
  Cache tokens are billed at the input price. If no price is known, the cost is 0 and
  `meta.warnings` says so. This required `builtins.default_registry` to pass `config` to
  `runners.register` (a one-line change).
- **P3-9 Claude Code budget.** `--max-budget-usd` is passed only with `budget_flag: true`.
  Otherwise the run's `total_cost_usd` is recorded, and the engine's `spend()` raises
  `BudgetExceeded` (needs-review) when it went over. The runner also marks
  `meta.budget_exceeded`. A run can therefore overshoot by one call's cost. Using the CLI
  cost answers open question 4 for now.
- **P3-10 `RunnerError`** is a retryable error that carries the cost and transcript of a failed
  paid call (for example claude-code `is_error`). `agent_handler` spends that cost before
  re-raising.
- **P3-11 Missing tools**: a shell exit of 126 or 127 with a "not found" message, and Claude Code
  `MISSING_TOOL:` or `command not found` lines in its result, give `missing_tools`. So does a
  `claude` binary missing *inside* a container. A `claude` binary missing locally is a
  `PermanentError`.
- **P3-12 Image tags hash the generated Dockerfile** (profile, or `Dockerfile.agentline`
  content), so a profile change gives a new tag. Profiles are `[images.<name>]` with `from`,
  `apt`/`packages`, `run` and `extends`. A combined auto profile such as `node+python` merges
  its parts. There are built-in defaults for python, node and go, and the base image comes
  from `[images.base] from` (default `agentline/runner-base:latest`).
- **P3-13 The image proposer is deterministic**: a table maps each command to its apt
  package (about 45 entries). It returns `{profile_from, profile_to, add_apt, reason, unknown,
  toml, proposer}`, and `toml` is a ready `[images."<profile_to>"]` snippet using `extends`.
  `images.set_proposer(fn)` is the seam where an LLM-backed `image.propose` can replace it.
  Nothing is applied or built from a proposal in this phase.

## Known limits

- **No egress restriction yet**: the container network defaults to `bridge`. The LLM-API-only
  egress proxy is future work. `network: none` works for runners that don't need the API.
- One workspace node at a time per run: parallel workspace nodes in the same run would
  share the branch `agentline/<run_id>`, and the second worktree would evict the first.
- Images are not pinned to a digest on the execution yet (only the ref is recorded in
  `notes.image`), and there's no `allow_images` check on auto-built or devcontainer refs at
  run time (the validator checks explicit `image:` refs).
- `Dockerfile.agentline` builds hash only the Dockerfile, not the files it `COPY`s.
- L2 dependency layers, cache volumes, `agentline mirror` and `agentline approve --proposal`
  (apply and build) are not built yet.
- `docker exec` timeouts kill the CLI client. The process inside the container keeps running
  until the container is removed at the end of the node.
- On a failed attempt, the worker doesn't persist `ctx.notes` (the transcript blob of a
  `RunnerError` is stored but not referenced from the execution). Phase 5 moves attachments
  anyway.
- The python runner's timeout can't stop a sync function's thread. It only stops waiting
  for it.
- Upstream commits pushed to an `agentline/*` branch (for example a reviewer's suggestion) are
  not pulled into iteration 2. Each iteration continues from the local branch head.

## Tests

- `tests/runners/`: the claude-code runner against a fake `claude` executable
  (`fake_claude.py`), covering JSON/text/verbose output, `is_error`, garbage output, missing
  tools, budget refusal and overrun, timeout, env scrubbing and running inside a workspace.
  Also the anthropic runner against a local fake HTTP server, and the shell and python runners.
- `tests/workspaces/`: mirrors and worktrees on temp git repos, the local provider, the container
  provider with a fake docker executor (exact `docker run`/`exec`/`rm` argv, cleanup on error
  and cancellation, idempotent auto build), image resolution and the proposer. Also a YAML
  pipeline end to end (`local` + `shell` commits, then the branch artifact feeds the next node;
  missing tools park with a proposal; a `RunnerError` cost is recorded).
- `tests/smoke/`: real Claude Code and real docker, opt-in. See [smoke.md](smoke.md):

```bash
AGENTLINE_SMOKE=1 uv run pytest -q -rs tests/smoke
```
