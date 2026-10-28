# Phase 49: OpenCode and Hermes Agent harnesses

Two more CLI harness presets in Settings → Harnesses → Add harness (and "+ Add a harness…" in
every picker), next to Claude Code, Codex and Aider. Like those, they are offered when their
program is on PATH; otherwise they sit under "Not installed" with the install command:

| Preset | Kind / runner | Program | Install |
|---|---|---|---|
| `opencode` | `opencode` | `opencode` | `npm install -g @opencode/cli@latest` |
| `hermes-agent` | `hermes` | `hermes` | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |

Both use Anthropic API keys or any OpenAI-compatible source (not the Claude subscription,
which stays Claude Code's), and with no model they use the CLI's own default.

## OpenCode (`runners/opencode.py`)
- `opencode run --format json [--model p/m] [--agent a] [--auto] -- <prompt>`.
- Model: `anthropic/<id>`; an OpenAI-compatible source is declared to OpenCode as provider
  `agentline` (`@ai-sdk/openai-compatible`, its URL and headers) in `OPENCODE_CONFIG_CONTENT`,
  with the key as `{env:AGENTLINE_PROVIDER_KEY}`, so the user's opencode.json is untouched.
- `allowed_tools`: missing `edit` / `bash` / `web` become `deny` in `OPENCODE_PERMISSION`;
  `edit` or `bash` add `--auto` (nobody is there to approve).
- Events: `text` (the last is the answer), `tool_use` (failed / not-found commands feed the
  missing-tool check), `step_finish` (tokens and cost, summed), `error`.

## Hermes Agent (`runners/hermes.py`)
- `hermes chat --query=<prompt> --oneshot --format stream-json [--provider ...] [--base-url
  ...] [--model ...] [--toolsets ...] [--yolo] [--max-turns N]`. `--query=` keeps a prompt that
  starts with `-` from being read as a flag.
- Provider: `anthropic`, `openrouter` (a source on openrouter.ai), else `custom` with
  `--base-url`; keys reach it as ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY via
  `env_map` (a keyless local server gets `OPENAI_API_KEY=none`).
- `allowed_tools` → toolsets: read/edit → `file`, bash → `terminal` (+ `--yolo`), web → `web`.
  `file` reads and writes, so read-only can't be enforced; `toolsets:` overrides.
- Events: the final `result` event (`text`, `exit_code`, `usage` / cost when present).

## Also
Setup step 2 lists them (`setup.HARNESS_ORDER`), the Chat GUIDE names the kinds, docs table in
`docs/harnesses-and-models.md`.

## Not done / not verified
- Tested against fake CLIs only (`tests/runners/test_opencode_hermes.py`): neither program is
  installed on the build machine. The event formats follow the docs (opencode.ai/docs/cli,
  hermes-agent.nousresearch.com/docs/reference/cli-commands); OpenCode's JSON event schema
  isn't documented there, so its parser skips unknown events.
- MCP servers on nodes stay claude-code / codex only (`mcp.MCP_KINDS`).
- Images in the Chat go through the vision transcript for these kinds, like codex / aider.
