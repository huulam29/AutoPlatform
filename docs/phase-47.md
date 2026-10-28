# Phase 47: first-run setup uses Add models

Step 1 of first-run setup ("Connect a model API?") is now the same three steps as Settings →
Models → Add models (source → connect → choose models), so first run and Settings look and
behave the same (planned in phase 46, decision #74).

- `components/settings/AddModelsDialog.tsx`: the steps are `AddModelsFlow`; `AddModelsDialog`
  is the modal around it. `embedded` (setup): h3 headings, no inner step line, no Escape
  handler, no Cancel on the first step (setup has Skip / Next).
- `components/SetupDialog.tsx`: step 1 lists the connected sources with model counts
  (`lib/models.modelGroups`), then the flow. Adding models refreshes the list and the setup
  status, and the flow starts over to add another source. The old per-source key cards are
  gone; step 2 (harness) is unchanged. `/api/setup` still reports `sources`, used for "Next:
  pick a harness" vs "Skip this step".
- Fix (Settings too): a custom endpoint is saved before its test, so a failed test used to
  leave the name taken and the retry refused ("Pick a new name"). The connect step now updates
  the source it created.

Checked in the browser on a scratch data folder with a fake OpenAI-compatible server: custom
endpoint with a bad URL (error inline), retry with the right URL, pick a model, the list shows
"homelab 1 model", step 2 offers it for Built-in. `npm test` (183), `tsc`, `lint_hardcodes.py`.

Not changed: a keyless custom endpoint counts as connected as soon as it is saved, even when its
test failed (phase 46 rule: a keyless source you set up is assumed ready).
