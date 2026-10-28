# Harnesses and model sources

agentline keeps three things apart, all in the local `agentline.toml`. In the GUI, sources and
models share one page, **Settings → Models**, and harnesses have **Settings → Harnesses**:

| | What it is | Examples |
|---|---|---|
| **Model sources** (`[providers.*]`) | where models come from | your Claude Pro/Max login; the Anthropic API; OpenRouter; OpenAI; Ollama; a private vLLM server |
| **Models** (`[models.*]`) | a named model on a source | `opus` = Claude Opus on your login; `free-llama` = a free OpenRouter model |
| **Harnesses** (`[harnesses.*]`) | what drives the model for a node or the Chat (its loop, tools and context) | Claude Code, Codex, Aider, or `builtin` (agentline's own: needs only an API key) |

Each agent node picks a **harness** and/or a **model**; the pipeline's `defaults:` (the "main
node") fill in what a node leaves out. Routers can also let a model choose the branch.

## Which harness can use which source

| Harness kind | Sources it can use | Notes |
|---|---|---|
| `claude-code` | Anthropic only: your Claude login (subscription) or an Anthropic API key | With no model set, it uses its own default model. |
| `codex` | OpenAI-compatible (OpenAI, OpenRouter, private servers) | Passed as a custom model provider through `-c` config overrides. |
| `aider` | Anthropic API key, or OpenAI-compatible | OpenAI-compatible models go through `--openai-api-base`. |
| `opencode` | Anthropic API key, or OpenAI-compatible | `anthropic/<id>`, or the source declared to OpenCode as provider `agentline` (`OPENCODE_CONFIG_CONTENT`). No model: OpenCode's own default. |
| `hermes` (preset `hermes-agent`) | Anthropic API key, OpenRouter, or OpenAI-compatible | `--provider anthropic` / `openrouter` / `custom --base-url`; the key goes by env, never argv. No model: Hermes's own default. |
| `builtin` | Anthropic API key, or OpenAI-compatible | On nodes: one request, no tools, for small tasks with cheap, free or private models. In the Chat: a small tool loop. |

A pair that doesn't fit is a validation error at the node's line, e.g. Claude Code with an
OpenRouter model:

```
pipeline.yaml:34:1: nodes.fix: harness kind 'claude-code' can't use model 'free-llama' from
openai-compatible provider 'openrouter' (it supports: anthropic). Claude Code uses Anthropic
models only; use harness: builtin (or aider/codex) for OpenRouter or a private server.
```

A Claude subscription login only works through Claude Code. To make sure a Claude Code run
uses your login and not an API key, define the source with `auth = "subscription"`: agentline
then never passes `ANTHROPIC_API_KEY` to the CLI. The `total_cost_usd` Claude Code reports is
recorded as the node's cost (an API-equivalent estimate on a subscription), so budgets still
act as a usage cap.

## Config

```toml
[providers.claude]                 # your Claude Pro/Max login (Claude Code only)
type = "anthropic"
auth = "subscription"

[providers.openrouter]
type = "openai-compatible"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY" # the env var name, never the key
headers = { "X-Title" = "agentline" }

[providers.homelab]                # a private server; no key needed
type = "openai-compatible"
base_url = "http://192.168.1.50:8000/v1"

[models.opus]
provider = "claude"
model = "claude-opus-5-5"

[models.free-llama]
provider = "openrouter"
model = "meta-llama/llama-3.3-70b-instruct:free"
# input_usd_per_mtok / output_usd_per_mtok: prices for cost tracking. Without them, OpenRouter's
# reported cost is used; ":free" models are recorded as 0; others as 0 with a warning.

[harnesses.small-tasks]
kind = "builtin"
model = "free-llama"               # the harness's default model

[harnesses.claude-code]
kind = "claude-code"
# bin = "/opt/homebrew/bin/claude"   # if it isn't on PATH
# model = "opus"                     # pin a model; otherwise Claude Code's default
```

## Built-in defaults (you only add keys)

These exist without any config. Settings shows them as **built-in**; editing one saves an
override in agentline.toml, and deleting the override brings the built-in back.

| Sources | Type | Key |
|---|---|---|
| `claude` | your Claude Pro/Max login (Claude Code only) | none |
| `anthropic` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openrouter` | OpenRouter | `OPENROUTER_API_KEY` |
| `openai` | OpenAI API | `OPENAI_API_KEY` |
| `ollama` | Ollama on this machine (`http://localhost:11434/v1`) | none |

| Models | Source | Model id |
|---|---|---|
| `opus`, `sonnet` | claude (login) | `claude-opus-5-5`, `claude-sonnet-5-5` |
| `sonnet-api`, `haiku-api` | anthropic | `claude-sonnet-5-5`, `claude-haiku-5-5` |
| `free` | openrouter | `openrouter/free` (OpenRouter's free models, $0, rate-limited) |
| `auto` | openrouter | `openrouter/auto` |
| `gpt` | openai | `gpt-6.1-sol` |

Only one harness is built in: `builtin` (default model `free`). It is also what a node runs
when it names no harness and the pipeline has no default. The CLI harnesses are added from
presets (Settings → Harnesses → Add harness, "+ Add a harness…" in any Harness picker, or the
setup):

| Preset | Default model |
|---|---|
| `claude-code` | its own, through your login (no key) |
| `codex` | `gpt` |
| `aider` | `sonnet-api` |

Harness pickers list only the harnesses that are set up. An OpenRouter key alone makes every
`builtin` node and LLM router work at no cost. (Before phase 44 the three presets were built in;
the first start after the update adds the ones an install was using, see `docs/phase-44.md`.) Model ids were checked against the providers' lists
on 2026-10-19; `openrouter/free` doesn't go stale as individual free models change.

## In a pipeline

```yaml
defaults: { harness: small-tasks }     # the "main node"

nodes:
  triage:                              # a model picks the branch
    type: router
    mode: llm
    model: free-llama
    prompt: "Is this a bug report or a question? {{payload.text}}"
  plan:
    type: agent
    harness: claude-code               # Claude Code with its own default model
    prompt: prompts/plan.md
  fix:
    type: agent
    harness: claude-code
    model: opus                        # a named model
    prompt: prompts/fix.md
  summary:
    type: agent                        # inherits defaults: small-tasks -> free-llama
    prompt: "Changelog entry for {{plan.summary}}"
```

- `model:` is a `[models]` name, or `source:model-id` (split at the first colon, so
  `openrouter:qwen/qwen3-8b:free` works), or a bare model id on the harness's default source.
- **Defaults are a pair.** A node that sets only `harness:` gets that harness's own default
  model, not the pipeline's default model (unless it's the same harness). A node that sets only
  `model:` uses the default harness.
- `runner:` still works as a low-level escape hatch (e.g. `runner: python`), but a node uses
  either `harness:` or `runner:`, not both.
- **LLM routers.** The model sees the prompt and the router's edge labels and must answer
  with one label. A wrong answer gets one repair prompt; then the `default` edge if there is
  one, else the attempt fails and is retried. The answer is kept in the router's result.

Full example: [examples/mixed-agents/](../examples/mixed-agents/).

## API keys

Keys are never written to agentline.toml, the ledger or logs. A source names the env var
holding its key (`api_key_env`). Either set it in your shell, or set it in the GUI where it is
used: **Settings → Models** (Add models, or a source's ⋯ menu → Change key), the MCP server's
form, a chat app's card, or **Settings → General → Other keys** for the rest (Jira, your own). That writes it to `~/.agentline/secrets.env` (owner-only, 0600), and the API never shows it
again. At startup agentline loads that file without overriding variables already set in your
shell, and every loaded key is added to the log/ledger redactor.

Without the GUI: `agentline keys set OPENROUTER_API_KEY` (hidden prompt; `--stdin` for
scripts), `agentline keys list` (set / not set, never values), `agentline keys rm NAME`.

Harness CLIs only see the keys their source needs, under the name the CLI expects. For example, a
source with `api_key_env = "WORK_ANTHROPIC_KEY"` reaches Claude Code as `ANTHROPIC_API_KEY`.

## Applying changes

The GUI saves to agentline.toml table by table, so comments and other settings stay as they
are (a `.bak` copy is kept). `agentline serve` reloads them live (phase 42): settings changes
apply to runs already going, pipeline structure changes to new runs only. A separate
`agentline worker` still needs a restart.

## The Models page (phase 46)

Settings → Models shows only what can be used: a group per connected source with its models.
Sources nobody connected appear only in **Add models** (pick a source → connect: key, Claude
login or URL → tick its models). A source you set up that stopped working (key removed, signed
out, server down) shows under **Needs attention** with Reconnect; its models leave every
picker until it's fixed. The eye on a model row hides it from every picker without removing it
(`hidden = true` in `[models.x]`; for a built-in model an override table is written, and
removed again when it's shown).

## Old names (before phase 35)

Harnesses were called agents. Still read, so older files keep working:

- `[agents.<name>]` tables (a settings save renames their headers to `[harnesses.<name>]`, in
  place); the same name in both tables is an error.
- `kind = "llm"` and the harness name `llm` mean `builtin` (unless you defined your own `llm`).
- `agent:` on agent nodes and in `defaults:` means `harness:`; the editor renames the key when
  you change its value. Both on one node is an error.
- Older bundles (`requires.agents`), saved Chat choices and Chat actions (`section: agents`,
  `chat_uses.agent`).
