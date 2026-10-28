# Phase 17: setup popup, MCP, import/export, chat links, script steps

## What the user asked
1. Chat links (like OpenClaw): give prompts from a messaging app; agentline asks questions,
   asks permission and reports results there.
2. A popup on first run, or whenever no LLM is available, to connect one quickly.
3. MCP servers in Settings, shared between nodes; a node must allow each one.
4. The assistant must choose node types well: not every node is AI; scripts where they fit,
   written by the assistant.
5. Import / export pipelines.

Decisions: Telegram first; gate approvals from the linked account (audited); MCP for Claude
Code and Codex; the popup in the Mac app and the web editor (ARCHITECTURE §12 #43–#47).

## As built
**Script steps / node types** — the assistant's guide has a "choosing node types" section
(scripts, integrations, agent only for judgment, router, gate, wait, chat steps) and writes
scripts under `scripts/` (made executable on apply). `{{pipeline.dir}}` in templates;
`shell.run` gets `$AGENTLINE_INPUTS` (a JSON file of the node's inputs) and `output: json`.

**First-run setup** (`setup.py`, `api/setup.py`, web `SetupDialog.tsx`, Mac `SetupSheet.swift`)
— options: Claude Code CLI, OpenRouter / Anthropic / OpenAI key, Ollama. `GET /api/setup`
says what works; the choice (`PUT /api/setup/choice`) is the assistant's default agent/model.
The web dialog stays hidden inside the Mac app (`managed`), which shows its own sheet (keys to
the Keychain, engine restart, then the choice).

**MCP** (`mcp.py`; Settings API section `mcp`; `POST /api/mcp/{name}/test`; Mac Settings > MCP)
```toml
[mcp.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
```
Agent nodes: `mcp: [github]`. Literal secret-looking values are refused. claude-code gets a
0600 `--mcp-config` file (in the workspace when there is one, deleted after the run) plus
`--strict-mcp-config` and `mcp__<name>` in `--allowedTools`; codex gets `-c mcp_servers.*`
(its `env` values do go on the argv: a known limitation). The test runs initialize +
tools/list (stdio, or streamable HTTP). Untested against real servers / CLIs.

**Import / export** (`bundle.py`, `api/bundles.py`, `agentline export|import`, editor Export
button and Import dialog). The Mac web view got a Save panel (`agentlineSave` message) and a
file picker so the same buttons work there.

**Chat links** (`chatlink/`: `telegram.py`, `state.py`, `service.py`, `steps.py`; `api/chat.py`;
Mac Settings > Chat). Started by `agentline serve` (not `worker`, so updates aren't read twice);
idles until `TELEGRAM_BOT_TOKEN` is set. State in `$AGENTLINE_HOME/chatlink.json` (0600).
Pairing: Settings > Chat > Link shows a code / t.me link; only that user is answered. Out:
gates (Approve / Reject; a confirm tap when the gate leads to a deploy-class action),
`chat.ask` questions (reply to the message or tap a choice), dead / needs-review runs, finished
runs (off by default); runs that ended before linking aren't reported. In: `/help` commands
and free text to the assistant (Apply buttons for settings, skills and pipelines; keys only in
the app). Pipeline steps:
```yaml
ask:    { type: wait, for: chat.ask, with: { question: "Which env?", choices: [eod, staging] }, out: answer }
report: { type: action, uses: chat.send, with: { text: "Done: {{answer.text}}" } }
```
Tested against a fake Bot API only.

## Not done / follow-ups
- Discord, Slack, WeChat links (the service is Telegram-specific; `state.py` and the loop's
  shape carry over).
- Web Settings page has no Chat / MCP tabs yet (Mac has them; the API is there).
- Real runs: Telegram bot, MCP servers with Claude Code / Codex, the assistant with real
  `claude`.
