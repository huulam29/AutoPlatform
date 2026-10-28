# Phase 42: live reload (no restart after saves)

Before: the engine compiled every pipeline once at start, with each step's harness and model
resolved into it (runner, model id, URL, key name, price), and built the runner registry from
agentline.toml then. Any settings save showed "Running workers keep their old settings until
restarted", and a pipeline saved in the editor wasn't loaded at all until a restart (no notice).

Now (`src/agentline/live.py`, `LivePipelines`, used by `serve`, `worker` and the dev server):
- **What reloads:** `reload()` reads agentline.toml, rebuilds the registry and recompiles every
  pipeline file, then swaps the graphs into the App (`App.replace_graphs`). It runs after
  every successful write to the API (a middleware in `create_server`: editor and Settings
  saves, Chat applies, imports, restores) and from `watch()`, which checks file modification
  times about once a second (hand edits, the CLI, a separate `agentline worker`).
- **Runs already going (the user's rule):** settings changes apply to their remaining steps and
  retries right away; pipeline *structure* changes (`Graph.structure_hash`: nodes, inputs,
  outputs, attempts, edges) apply to new runs only. Every structure still in use is kept in
  `App._versions` by hash, and `App.graph(flow, run)` picks the one matching the run's
  `flow_hash`. A settings-only reload replaces the graph of the same hash. Old structures are
  forgotten once no unfinished run uses them. (After an engine restart, old structures are
  gone and such a run uses the current graph, as before.)
- **Safe edits:** a pipeline that no longer validates keeps its last good version (reported in
  `live.last["problems"]` and the log); a broken agentline.toml keeps the previous settings; a
  deleted pipeline takes no new runs, but runs already going finish.
- **Triggers follow:** webhooks (Jira targets), the schedule loop (each schedule keeps its
  cursor) and Jira polling (each keeps its `since`) read the live list after a reload.
- **Keys:** runners read a key's env var when they run, so a key saved through the API applies
  at once. A change to `secrets.env` from outside (the Mac app's Keys screen, a hand edit) is
  synced into the environment (`secrets.sync_secrets`; a variable set in the shell still
  wins). The Mac app no longer asks to restart after saving a key.
- Saves are silent (phase 45, user: "the banner is annoying"): with live reload a Settings
  save returns no `note`, so no banner. Only facts the user needs still show (a model kept as a
  harness's default), and the restart reminder appears only where it is still true (no live
  engine: `api/settings.saved_note`). A successful reload clears the restart notice (`/api/status restart_needed`). A restart is still needed to
  change the pipelines folder or the engine itself.

Tests: `tests/test_live.py` (settings reach a waiting run, structure change applies to new runs
only, broken edits keep the last good version, new and deleted pipelines, a broken config, the
secrets file, saves through the API). Also: `tests/conftest.py` now gives every test a
throwaway `$AGENTLINE_HOME`. While writing the secrets test, a run without it created a
stray `~/.agentline/secrets.env` (the folder didn't exist before; it was removed, and the Mac
app's data folder was untouched).
