# agentline

Open-source, self-hosted platform for running multi-agent software-delivery pipelines.
Each box in a pipeline is a step (an AI agent, an action, an approval gate, a router or a
wait), linked into a graph defined as YAML in your repo, with a visual editor on top. The
engine is durable: runs survive crashes without duplicating side effects or repeating paid
LLM calls.

```yaml
nodes:
  plan:      { type: agent, harness: claude-code, prompt: prompts/plan.md, in: [ticket], out: plan }
  implement: { type: agent, harness: claude-code, prompt: prompts/implement.md,
               in: [plan, review?], out: branch, workspace: { type: container } }
  open_pr:   { type: action, uses: github.open_pr, in: [branch], out: pr }
  await_review: { type: wait, for: github.review, in: [pr], out: review }
  route:     { type: router, switch: review.state }
  approve:   { type: gate, prompt: "Deploy to EOD?" }
  deploy:    { type: action, uses: env.deploy, with: { env: eod }, in: [branch] }
edges:
  - plan -> implement -> open_pr -> await_review -> route
  - { from: route, to: implement, when: changes_requested, max_loops: 3 }
  - { from: route, to: approve, when: approved }
  - approve -> deploy
```

Full example: [examples/ticket-to-eod/](examples/ticket-to-eod/). Design:
[ARCHITECTURE.md](ARCHITECTURE.md).

## What's in v0.1
- **Durable engine**: per-node state machine on SQLite (or Postgres), leases with fencing,
  retries with backoff, budgets, an effect guard so a crash never opens a second PR or
  deploys twice, waits and human gates that park without holding a worker.
- **Pipelines as YAML**: agents, actions, routers, bounded loops, gates, waits; a validator
  with line numbers that enforces "a gate before every deploy".
- **Runners**: Claude Code (headless), Codex, Aider, Anthropic API, Python, shell.
  **Workspaces**: a git worktree per step, locally or in a Docker container.
- **Integrations**: Jira and GitHub actions, signed webhooks, polling fallback, Kafka
  source, cron schedules.
- **Artifacts**: content-addressed, summarized, with lineage; large inputs passed by path.
- **Operations**: `agentline` CLI, audit log, JSON logs, secret redaction, a React Flow
  editor that round-trips YAML with comments, and a live run view.

## Install the `agentline` command

```bash
uv tool install --editable '.[server]'   # from the repo root; follows code changes
uv tool update-shell                     # puts ~/.local/bin on your PATH (new terminal after)
```
Or run it without installing: `uv run agentline ...` from the repo. The Mac app
(`macos/README.md`) bundles its own engine and doesn't need either.

## Quickstart

```bash
uv sync --extra server
agentline templates
agentline new ticket-to-eod pipelines
agentline validate pipelines/ticket-to-eod/pipeline.yaml
agentline serve                   # webhooks + editor + run view on http://127.0.0.1:8080
```

Proof that it holds up: [demo/README.md](demo/README.md) (a naive worker duplicates PRs
under `kill -9`; agentline doesn't) and [docs/phase-9.md](docs/phase-9.md) (the full
pipeline end to end under chaos). One real Claude Code run: [docs/real-run.md](docs/real-run.md).

## Development

```bash
uv run pytest                     # ~520 tests (~70 s); chaos and e2e runs included
uv run ruff check . && uv run ruff format --check .
cd web && npm ci && npm test && npm run build
```

See [CLAUDE.md](CLAUDE.md) for layout, conventions and gotchas.
