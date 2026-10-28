# Phase 46: Settings show only what is usable

The user found Model sources / Models / Keys / Harnesses hard to use: four tabs for one job,
listing things that couldn't be used (sources with no key, Ollama when it isn't running).
Principle: **only show what is usable**; what isn't set up appears only inside an "Add…" flow.
Ideas from VS Code's Language Models editor (grouped by provider, eye toggle, Add Models),
Zed's external agents and OpenClaw's auth-first onboarding. Layout A of three mockups
(https://claude.ai/artifact/54ko8WW2vYNyUBRP62VTXw). Decision #74.

Settings tabs now: **Models · Harnesses · MCP · Skills · Chat apps · General**.

## Models page (`web/src/components/settings/ModelsPage.tsx`)
- A group per connected source (the ones you set up first): status word, model count, ⋯ menu
  (Add more models, Change key, Claude login, Change URL for your own sources, Disconnect).
- Rows: name, model id, free badge, eye (hide from / show in pickers), × for models you added.
- **Needs attention**: sources you set up (`user_set`) that aren't `connected`, with the reason,
  Reconnect (Add models at its connect step) and Remove.
- Empty state "Connect your first model"; search over name / id / source (`free` finds free
  models). Helpers in `web/src/lib/models.ts` (tested).
- **Add models** (`AddModelsDialog.tsx`): source (OpenRouter, Anthropic API, OpenAI, Claude
  subscription when the claude CLI is installed, Local server, Custom endpoint) → connect (key,
  Claude login, or URL; tested by listing models) → the phase-41 checklist.

## Harnesses
- Rows show "Ready" / "Program not found" / "No model ready" and the default model, said as
  not ready when its source isn't connected.
- **Add harness** (`components/AddHarness.tsx`, also "+ Add a harness…" in pickers): installed
  programs as choices; missing ones in a collapsed "Not installed" list with their install
  command and Check again; "Custom harness…" opens the full form. The presets toolbar is gone.

## Keys
- The Keys tab (and the Mac-only native keys section) is gone. Source keys: Models; MCP
  secrets: the server's form; chat app tokens: their card (now with "Change token"); GitHub:
  General → GitHub. The rest (Jira, your own names) under **General → Other keys**:
  `GET /settings` keys carry `owner` (`provider:x`, `mcp:x`, `chat:x`, `github`, or none).

## Engine
- `[models.x] hidden = true` (`ModelProfile.hidden`): left out of `compatible_models`, so every
  picker drops it. `PUT /api/settings/models/{name}/visibility {hidden}`; a built-in model gets
  an override table, removed again when shown with nothing else changed.
- Providers in `agents.summary` carry `connected` and `user_set`; harnesses carry `installed`.
- `readiness.server_up(base_url)`: a keyless built-in source (Ollama) is ready only while
  `GET <base>/models` answers (cached 15 s). A keyless source you set up is assumed ready.
- Chat action `model_visibility {model, hidden}` (docs/phase-25.md table).
- Messages that said "Settings > Keys" / "Model sources" point to the new places.

## Not done
- (Done in phase 47: first-run setup step 1 reuses the Add models steps.)
- A model added from a list where the source said "free" by price (not a `:free` id) shows no
  free badge, since prices aren't stored on add.
- Mac app: no Swift change; the NativeBridge `keys.*` actions are no longer called by the web.

## Verification
`uv run pytest` (new `tests/ai/test_models_page.py`, parity cases), ruff on changed files,
`npm test` (183), `tsc`, `lint_hardcodes.py`. Browser on a scratch data folder with a fake
OpenAI-compatible server: custom endpoint → choose models → hide one (left
`compatible_models`, written `hidden = true`) → a source with an unset key under Needs
attention → Add harness with Claude Code installed and Codex / Aider under Not installed →
General → Other keys. Fixed on the way: a custom endpoint's models step was empty (the new
source's name wasn't kept), choice cards didn't wrap, the page width jumped with content.
