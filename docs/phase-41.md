# Phase 41: add many models at once from a model source

User request: in Settings → Model sources, "Test / list models" showed the source's models as
links; clicking one jumped to the Models tab with a new-model form to fill and save, one model
at a time.

Now the list is a checklist (`ModelCheckList` in `web/src/components/Settings.tsx`):
- check any number of models (the whole row toggles; search, "free only", "Select shown",
  "Clear"), then **Add N models**: they go into the Models tab in one call, named from their
  ids (`model_name_for`, e.g. `meta-llama/llama-3.3-70b-instruct:free` →
  `llama-3.3-70b-instruct-free`);
- models of this source already in Models are checked, greyed and marked "added".

Engine: `PUT /api/providers/{name}/models` takes `add_only: true` (adds the missing ids,
removes nothing), so a stale page can never delete a model. The Chat's `source_models` action
keeps the exact-set behavior. The single-pick list inside the Models form is unchanged.

Tests: `tests/ai/test_settings_api.py::test_add_only_adds_the_checked_models_and_removes_nothing`,
`web/src/lib/ai.test.ts` (checklist helpers). Checked in the browser against a fake source:
two checked models were added, shown as "added", and written to agentline.toml.
