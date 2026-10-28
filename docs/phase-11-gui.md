# Phase 11 (GUI): AI agents and model sources

Branch `phase-11-gui` (from `phase-11`). The editor half of "AI agents and model sources": a
Settings page for agents, model sources, models and keys, agent/model pickers on nodes and on
the pipeline defaults, LLM routers, and the effective agent/model on the canvas. The backend
(`agents.py`, `api/settings.py`, `node_types`) is the one from `phase-11`; the only backend
change here is `pipeline/roundtrip.py` carrying `defaults`.

## Files

| Path | What |
|---|---|
| `web/src/lib/ai.ts` | pure logic: `choose` (mirrors `agents.choose`), effective agent/model, compatible models, field edits (agent/runner exclusivity, router mode, defaults), canvas line, presets, form bodies, `submitSecret` |
| `web/src/lib/ai.test.ts` | vitest for all of the above |
| `web/src/components/Settings.tsx` | the Settings page (Agents, Model sources, Models, Keys) |
| `web/src/components/AiPicker.tsx` | `AgentSelect`, `ModelSelect` (named models + free-text `provider:model-id`) |
| `web/src/components/Panels.tsx` | agent node: Agent / Model at the top, runner under Advanced; router: Mode toggle |
| `web/src/components/Editor.tsx` | pipeline panel: Defaults group |
| `web/src/components/PipelineNode.tsx`, `lib/graph.ts` | second line on canvas nodes; `llm router` summary |
| `web/src/api.ts`, `types.ts`, `App.tsx`, `styles.css` | API calls and types, the `#/settings` route, styles + phone-width layout |
| `src/agentline/pipeline/roundtrip.py` | `defaults` in `to_model` / `apply_model` |
| `tests/pipeline/test_roundtrip.py`, `tests/api/test_editor.py` | defaults round trips |

## Screens

**Settings** (`#/settings`, top nav next to Pipelines and Runs). A toolbar with four tabs and the
`agentline.toml` path the server writes to; after every save the server's note ("Saved.
Running workers keep their old settings until restarted.") shows as a banner. Each tab is a
list (left) and an editor form (right); at phone width they stack.

- **Agents**: built-ins marked `built-in`. The form has kind (with the kind's description and
  supported model sources), binary + **Detect** (`POST /agents/{name}/detect`: version, or
  "'claude' is not on PATH"), default model (only models compatible with the form's kind, or
  free text), extra args, env names, description. Editing a built-in creates it in the file;
  deleting a configured agent named after a kind is labelled "Reset to built-in".
- **Model sources**: one-click presets (OpenRouter, Ollama, OpenAI, Anthropic API key, Claude
  subscription) that create the entry immediately, plus a custom form: type, auth (Anthropic
  only: API key / Claude subscription login), base URL (required for openai-compatible), API key
  env var name, extra headers, description. **Test / list models** calls
  `POST /providers/{name}/test` and shows the error or the model list (searchable, free only),
  where "add as model" opens a prefilled new model.
- **Models**: name, model source, model id (free text, or **List models** to pick from the
  source's list with search and a free-only filter; picking a `:free` model fills prices 0/0 and
  suggests a name), prices per MTok, max tokens, temperature, description.
- **Keys**: every key name from `keys` with set / not set and its source. **Set key** opens a
  password input (`autocomplete=new-password`); **Remove** only for keys from the secrets file.
  "Add" sets a key under another name (e.g. a private server's key).

**Agent node panel**: Agent and Model selects first. Agent: "(pipeline default: X)" plus every
agent (built-ins marked). Model: "(pipeline default: m)" or "(agent default: m)", the agent's
compatible named models, and "other: provider:model-id…" for free text. The runner moves into an
**Advanced** disclosure (open when a runner is set). Agent/model issues from validation (the
validator reports them on the node) are shown under the pickers instead of the panel top.

**Router node panel**: Mode switch | LLM. Switch mode shows `switch`; LLM mode shows the prompt
(inline or file, like agent prompts) and a model ("(pipeline default: m)" when empty).

**Pipeline panel** (nothing selected): a Defaults group with agent, model and max_turns, which
edit the top-level `defaults:` map; `defaults` issues show inline.

**Canvas**: agent nodes get a second line `agent · model`, e.g. `cheap · free-llama`; parts that
come from defaults (or an agent's own default model, or the built-in fallback) are muted italic.
Agent nodes with a low-level `runner` keep the old summary. LLM routers show `llm router` and
their model.

## Rules mirrored from the backend

- `choose(node_agent, node_model, default_agent, default_model)` is copied from
  `agents.choose`: defaults are a pair, so a node that picks a different agent gets that agent's
  own default model, not the pipeline's.
- With no agent anywhere, `resolve` uses `claude-code`, or `llm` when only a model is set. The
  node's Model list uses the node's agent, else the default agent, else `llm`.
- The Agents form computes compatible models in the browser for the kind being edited
  (`compatibleForKind`); a test checks it agrees with the server's `compatible_models`.

## Round trip of `defaults`

`to_model` returns `defaults` (the plain map, or `null`). `apply_model` treats it like the other
top-level fields: changed keys are edited in place (one line per key, comments kept), `null` or
`{}` removes the section (comments inside it are reported as warnings), and a model without the
`defaults` key leaves the file alone. A new section is written in block style before `nodes`;
the blank line that introduced `nodes` stays before `nodes` (this now applies to any new
top-level key, e.g. `description`).

## Verified

Dev server on a scratch copy of `examples/ticket-to-eod`, `AGENTLINE_HOME` and
`AGENTLINE_CONFIG` in a scratch dir (an `agentline.toml` with comments), `npm run build` first,
driven in the desktop app's browser pane:

- Presets created `openai` and `openrouter`; **Test** on OpenRouter without a key showed
  "OPENROUTER_API_KEY is not set (add it under Keys)" (no request left the machine).
- A custom source `private` at a local fake server (`python -m http.server` serving a static
  `/v1/models`), a key `MY_SERVER_KEY` set under Keys (a throwaway value; it went to the
  scratch secrets file, the input was cleared, and it is in neither the server log nor any API
  response), **List models** with free only, picked the Llama `:free` model as `free-llama`.
- Agent `cheap` (kind llm, default model `free-llama`); Detect on claude-code showed "not on
  PATH" in that environment.
- Plan node: agent claude-code + model `free-llama` showed the backend's Claude-Code-is-
  Anthropic-only error inline and on the canvas; switching to `cheap` cleared it and removed
  `runner`. Defaults: agent `cheap`, max_turns 15. Save produced exactly:
  `+defaults: / +  agent: cheap / +  max_turns: 15`, `-    runner: claude-code`,
  `+    agent: cheap`. `agentline.toml` kept all its comments.
- Deleting `free-llama` returned 409 "model 'free-llama' is the default of agents: cheap".
- Router mode toggle to LLM showed prompt + model and the "llm routers need 'prompt'" issue.
- No console errors. At 375 px wide, Settings (all tabs) and the editor have no horizontal
  scroll.

## Tests

- Python: 9 new tests (8 in `test_roundtrip.py`, 1 in `test_editor.py`).
- JS: 24 new vitest tests in `web/src/lib/ai.test.ts` (44 in total).

## Decisions (proposals)

1. **Presets create immediately** (one click), with the first free name: Anthropic presets take
   `anthropic` while it is implicit (so `claude-code` uses it without a model), otherwise
   `anthropic-api` / `claude-subscription`; others get `-2`, `-3`.
2. **Router mode switch drops the other mode's fields** (`switch` vs `prompt`/`model`), so the
   file never carries fields the parser rejects. Switching back starts empty.
3. **Picking an agent removes `runner` and vice versa**, in the model, so the YAML never holds
   both. The new `agent:` key is appended to the node (the round trip edits by key, so it does
   not take the runner's line).
4. **The Agent/Model choice is shown, not stored, when inherited**: empty selects describe what
   they mean ("(pipeline default: cheap)", "(agent default: opus)").
5. **Node types are reloaded after every Settings save** so the pickers see new agents/models
   without a page reload; validation already uses the server's refreshed config.
6. **Keys are write-only in the UI too**: the draft is cleared after a successful save and kept
   only when the save failed, so the user can retry; it is never in app state otherwise.
7. **No rename** of agents, sources or models: create the new name and delete the old one
   (references in pipelines and other entries would need rewriting).

## Limits

- The Agent/Model labels don't account for a node's low-level `runner` (the model label still
  describes the agent default); the panel says the runner is in use.
- **Detect** and **Test** run against saved settings; save a new entry first.
- The model list from **Test** is capped at 1000 by the server and 200 rows in the UI (search
  narrows it).
- Editing a flow-style collection that has a trailing comment on its line can lose the space
  before `}` (pre-existing in `rebase`, e.g. `{ a: b }  # c` -> `{ a: x}  # c`); not touched.
- No browser end-to-end tests in CI; the pages were checked by hand as above.
