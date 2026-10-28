# Smoke tests

`tests/smoke/` holds two manual tests against real tools. They are skipped in the normal suite
and only run with `AGENTLINE_SMOKE=1`. Each one also skips itself when its tool is missing.

```bash
AGENTLINE_SMOKE=1 uv run pytest -q -rs tests/smoke
```

## Claude Code (`test_claude_code_real_run`)

Runs the `claude-code` runner once, locally, with a tiny prompt, `--max-turns 2`, read-only
tools and a $0.50 budget. It checks that the JSON result parses and that a session id and a
cost come back.

- Needs the `claude` CLI on `PATH`, or `AGENTLINE_CLAUDE_BIN=/path/to/claude`.
- Auth: either `ANTHROPIC_API_KEY` in the environment (the runner passes only this declared
  variable), or an existing `claude` login under `$HOME` (HOME is passed through).
- Optional: `AGENTLINE_SMOKE_MODEL=<model id>` to pick a model.
- Cost: one short turn, normally well under $0.05.

## Docker container workspace (`test_container_workspace_real_docker`)

Creates a temp git remote, mirrors it, starts a real container with the worktree at
`/workspace`, runs the `shell` runner inside it (prompt on stdin, written to `smoke.txt`),
then checks:

- the command ran in `/workspace` as your uid (`--user <uid>:<gid>`);
- `finalize()` auto-committed the change on `agentline/run1` in the mirror;
- the container `agentline-smoke1` is gone afterwards (`docker rm -f` in `finally`).

Needs a running docker daemon (`docker info` must succeed). The CLI path can be set with
`AGENTLINE_DOCKER`. The image defaults to `alpine:3.20` (it is pulled on first use); override
it with `AGENTLINE_SMOKE_IMAGE=<ref>`. No agentline image build happens in this test.
