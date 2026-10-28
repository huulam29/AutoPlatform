# Phase 32: delete pipelines, one folder per pipeline, a Chat design rule

User requests:
- a Chat rule: design a new pipeline only from this session's requirements;
- "I cannot discard/delete a created pipeline";
- `ticket-to-eod` sat loose in `pipelines/` while new pipelines live in `pipelines/<name>/`.

## What changed
- `chat_instructions.FRESH_DESIGN` is now a default rule and an upgrade (`fresh-design`), so an
  existing instructions file gets it once. The rule: design new pipelines (including from an
  image) only from the requirements gathered in this session; use existing pipelines and
  REFERENCE templates only to check syntax; don't add steps nobody asked for.
- `DELETE /api/pipelines/{name}`: a pipeline in its own folder (`<name>/pipeline.yaml` with no
  other pipeline beside it) is deleted with its folder; a loose `x.yaml` is deleted alone,
  since files next to it may be shared. Pipeline history commits before and after, so it can
  be restored. A restart is noted, because the engine registers pipelines at start. The
  editor toolbar has a Delete button that asks first, and the Chat has a `delete_pipeline`
  action (its own click, never "Apply all").
- One rule for which files are pipelines (`pipeline/files.py pipeline_files`): top level plus
  pipelines' own folders, skipping dot and tool folders. It's used by the CLI/serve, the dev
  server and the editor. **Bug fixed:** `serve` used to load only top-level `*.yaml`, so
  pipelines the Chat or an import created in `<name>/pipeline.yaml` were listed in the editor
  but never registered, and couldn't run.
- `agentline new` (and so the Mac app's first-run seed) writes `<dest>/<name>/pipeline.yaml`
  with its prompts, schemas and scripts inside. Two templates no longer share `prompts/`.
- The user's existing `ticket-to-eod.yaml`, `prompts/` and `schemas/` were moved into
  `pipelines/ticket-to-eod/` (relative paths unchanged), with a history commit.
