# Phase 44: only `builtin` is built in; pickers list configured harnesses

User request, for the Harnesses tab:
1. no Chat settings there: the Chat's harness is set under Chat apps;
2. no built-in harness except `builtin`;
3. the Chat settings show only harnesses that are set up.
Asked and decided: migrate what an install already uses; a node with no harness falls back to
`builtin`; picking a harness that isn't set up opens a popup to set it up, and Cancel keeps
whatever was selected before.

## Engine
- `agents.BUILTIN_HARNESSES` is just `builtin`. `HARNESS_PRESETS` holds claude-code, codex and
  aider (their old built-in values). `FALLBACK_HARNESS = "builtin"`; a node with only a model
  is `builtin` too. A preset named but not set up fails validation with "isn't set up: add it
  under Settings > Harnesses". `resolve(..., presets=True)` lets agentline's own uses run one
  anyway (the vision transcriber's `@claude-code`).
- `agents.summary()["presets"]` lists the presets that aren't set up, each with `installed`
  (its program is on PATH).
- The Chat's default harness (no saved choice) and triage's are `builtin`. The Chat guide says
  CLI harnesses must be added with a `setting` action for `harnesses.<name>`.
- `harness_migration.py`, once per data folder (`state/harnesses-v44.done`), run at engine
  start (`CliEnv.config`, the dev server):
  - adds `[harnesses.<name>]` for each preset a pipeline or the saved Chat choice uses;
  - gives pipelines whose agent nodes relied on the old claude-code fallback
    `defaults: { harness: claude-code }` (comment-preserving, in the pipeline history).
  Unused presets are not added.
- Setup step 2 lists the configured harnesses, then installed presets marked `configured:
  false`; choosing one (`PUT /api/setup/choice`) sets it up first.

## Web
- Settings → Harnesses: the "Chat uses" bar is gone (it lives in Chat apps). The toolbar has
  "Add a preset" buttons for presets not set up (marked "not installed" when the program
  isn't on PATH). Only `builtin` is "built-in".
- Every Harness picker lists only configured harnesses plus "+ Add a harness…"
  (`components/AddHarness.tsx`): the node inspector and pipeline defaults (`HarnessSelect`),
  Chat apps → Chat uses (`ChatHarnessBar`) and the Chat panel. The popup picks a preset (and a
  program path when it isn't on PATH); **Save and use** writes it and selects it, **Cancel**
  keeps the previous choice. A pipeline naming a preset that isn't set up shows "(not set up)"
  and a "Set up <name>" link. Saves fire `SETTINGS_CHANGED_EVENT`, so the App and Settings reload
  their lists.

## Tests
`tests/ai/test_builtin_only.py` (marked `builtin_only`): the fallback, presets in the summary,
the migration (adds only what's used, keeps comments, once), setup's add-and-use. Older tests
were written when the presets were built in; `tests/conftest.py` keeps them set up (and the old
claude-code fallback) unless a test is marked `builtin_only`. The chaos demo worker sets up
claude-code for the example pipeline it fakes.
