# Phase 36: the built-in Chat harness (works with just an API key)

Problem: the Chat needed a CLI harness (Claude Code, Codex, Aider). With only an API key it ran
the `llm` runner: one call, `max_tokens` 2048 (a pipeline action was cut off, so its JSON broke
and the reply came back as raw text), no tool calling or JSON mode, no retry on broken JSON,
the conversation flattened into one message, and no way to look anything up.

## What it is
`src/agentline/chat_harness.py`, used when the Chat's harness kind is `builtin` (phase 35's new
name for `llm`). It is a small tool loop built on **Pydantic AI** (`pydantic-ai-slim[openai,
anthropic]`, in the `server` extra; locked at 2.54.0). We looked at the alternatives first:
- CLI harnesses that work with any provider (OpenCode, Cline CLI, OpenHarness) need an install,
  which is the thing API-only users don't have. OpenCode is still a good *pipeline-node* harness
  kind for later.
- Pydantic AI gives us both wire formats (OpenAI-compatible `/chat/completions` and Anthropic
  Messages), typed tools, a typed `reply` output with validation retries, request limits, usage,
  and a prompted-JSON output mode for models without tool calling. The engine stays stdlib-only:
  the Chat runs in `serve`, which already needs the `server` extra. `pydantic-ai-harness`
  (file/shell/sandbox tools for coding agents) isn't needed for the Chat.

## A turn
- Instructions: the GUIDE, the Chat instructions and the context (`assistant.system_text`), plus
  `chat_harness.HARNESS_NOTE`. Earlier turns are real user/assistant messages; image transcripts
  are added to the user turn as before.
- Tools (read-only; errors come back as `error: ...` text): `read_pipeline_file` (under the
  pipelines folder, `_safe_rel`), `run_details` (run, executions, last 200 journal lines),
  `engine_logs` (redacted ring), `read_skill`, `cant_do`.
- The answer is the `reply` tool ({message, choices, actions}). The output validator runs
  `assistant.check_actions`; the first failure goes back to the model as a retry (the old
  "repair round", now inside the loop). A second failure is accepted, with the issues on the
  cards, as for other harnesses.
- `max_tokens` is the model's setting or 16000. Non-OpenAI servers get `max_tokens` rather than
  `max_completion_tokens`, which OpenRouter, Ollama and vLLM don't read.
- At most 5 lookups. The same lookup 3 times, or a 6th, gets "answer now" instead of a result.
  At most 9 model requests per turn.
- A model that rejects tool calling (an HTTP 400/404/422 naming tools/functions) gets the same
  turn as prompted JSON, and is remembered for the process (`_NO_TOOLS`).
- No SDK retries: the Chat is interactive, and a rate limit is shown to the user.
- The result adds `steps` (shown as "Looked at: ..." under the reply) and `report`.

## Self-improving: App reports
What the harness can't handle becomes an incident of kind `chat-harness`. `triage.
triage_incidents` turns it into an App report ("Chat harness report"), which can be sent to
GitHub (automatically with `[report] auto = true`). Repeats share a fingerprint, so they count
up one report and one issue rather than opening new ones. One report per turn, first match wins:

| Class | When |
|---|---|
| `gap` | the model called `cant_do(what, why)` (the GUIDE note says to, instead of improvising) |
| `unknown` | a proposed action `type` that doesn't exist |
| `loop` | a repeated lookup, the lookup budget spent, or the request limit hit |
| `invalid` | actions still failing the checks after the retry |
| `unparseable` | no usable reply even after the fallbacks |
| `error` | an exception in the harness or a tool (with traceback and frames) |

A report carries the trace: tool names, argument sizes, action types, issue counts, the model id,
provider type and harness name. It never carries the conversation, file contents or lookup
results, because a GitHub repo may be public. The store's redactor runs on top. Not reported,
because the user fixes these: a missing key, 401/402/403/404/408/429, an unreachable server. The
Chat shows those as errors.

## Tests
`tests/ai/test_chat_harness.py` covers a lookup then a reply (tools sent, instructions, max
tokens, the tool result sent back), invalid actions retried once then reported, a `cant_do` gap
without the conversation in it (and the repeat counts up), the same lookup three times, the
request limit, an unknown action type, prompted JSON for a model without tools (remembered),
401/429/missing key not reported, lookup errors as text, a bug in a tool reported as `error`,
the Anthropic wire format (tool_use, tool_result, cost) and the history building. `tests/ai/fakes.py`
`FakeLLM` can now answer with tool calls (`tool_calls(...)`).

The vision tests now use `aider` as "a harness that can't see images", because `builtin` no
longer goes through a runner.

Checked in the browser against a scripted fake model: a pipeline card with "Looked at", and a
gap reported and listed under App reports. **Not tried against a real provider yet.**

## First-run setup in two steps
The first-run popup was one list, "Connect a model", that mixed a harness (Claude Code) with
API keys, and it never showed in the Mac app. The web dialog hid itself there (`managed`), and
the app's native `SetupSheet.swift` still decoded the pre-phase-35 `agent` field, so the setup
status failed to decode and the sheet never opened.

Now (`setup.py`, `web/src/components/SetupDialog.tsx`, `web/src/lib/setup.ts`):
1. **Connect a model API?** One card per model source (`sources` in `GET /api/setup`): a key
   field (saved to the secrets file, then checked by listing the source's models), the Claude
   login (the claude CLI), or a keyless local server (Ollama: ready once it lists models). It
   can be skipped ("Skip this step").
2. **Which harness should the Chat use?** One card per harness (`harnesses`): whether it can
   run (its CLI is installed; the built-in one needs a working source from step 1) and the
   models it can use **with what is set up now** (the preferred one first, e.g. `free`).
   "Use for the Chat" saves the choice (`PUT /api/setup/choice`).

It shows on the first run and whenever no harness can run, in the browser and in the Mac app.
The native sheet is gone, following the rule that the Mac app's UI is the web UI. Settings >
General has "Run setup again", which starts at step 1. `options` in the setup status was
replaced by `sources` + `harnesses`. Tests: `tests/ai/test_setup.py`, `web/src/lib/setup.test.ts`.
