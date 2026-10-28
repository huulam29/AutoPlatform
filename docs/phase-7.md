# Phase 7: visual editor

Branch `phase-7`. A web UI that edits pipeline YAML as a graph and shows runs, backed by a
small HTTP API. The YAML file stays the only source of truth: the editor stores nothing else
(no layout, no sidecar file), and every edit is a minimal, comment-preserving change to the file.

## Layout

| Path | What |
|---|---|
| `src/agentline/pipeline/roundtrip.py` | `load_document`, `to_model`, `apply_model`, `rebase` |
| `src/agentline/api/editor.py` | `PipelineFiles` (path-safe file access) + editor router |
| `src/agentline/api/runs.py` | run view router (`/runs`, `/runs/{id}`, `/artifacts/{hash}`) |
| `src/agentline/api/server.py` | `create_server(...)`: webhooks + `/api` + the built UI |
| `src/agentline/api/dev.py` | dev entry: `python -m agentline.api.dev` |
| `web/` | Vite + React 18 + TypeScript + `@xyflow/react` UI; `src/lib/` holds the pure logic |
| `tests/pipeline/test_roundtrip.py`, `tests/api/test_editor.py`, `tests/api/test_runs_api.py` | Python tests |
| `web/src/lib/*.test.ts` | vitest: layout, model <-> React Flow, issue mapping, run state |

## Architecture

```
browser (web/)                     server (FastAPI)                       disk / ledger
───────────────                    ─────────────────                      ─────────────
model (JSON graph) ──PUT model──▶  apply_model(file text, model) ──────▶  pipeline.yaml
       ▲                           (ruamel tree edit + line rebase)
       │◀──── {source, model, warnings, issues} ──── to_model(text) + parse/validate
       │
YAML tab ──────PUT source──────▶  saved as-is (even if invalid) ───────▶  pipeline.yaml
every change ──POST /validate──▶  dry-run apply + validate (nothing saved)
run view ──GET /runs/{id} (2 s)─▶ ledger reads + lineage + pipeline file ◀─ SQLite ledger
```

- **The model.** `to_model(text)` returns
  `{name, version, description, trigger, budget_usd, nodes: [{id, type, fields}], edges: [{src,
  dst, when?, max_loops?, on_exhausted?, chain_index}], issues}`. `fields` holds every YAML field
  of the node as plain JSON (`prompt` stays a path when it is a path). A chain `a -> b -> c`
  becomes two edges with the same `chain_index` (the index of the YAML `edges` item).
- **Issues** come from `parse_spec` and `validate_spec` with the default registry (or the one
  given to `create_server`), with path, message, line, column and hint. Each is mapped to
  `node_id` (+ `field`) from `nodes.<id>.<field>` paths, or to `edge_indexes` from `edges[i]`.
  The parser counts YAML items and the validator counts expanded edges; the mapping handles both.
  A cycle error points at the cycle's first node.
- **Layout** is computed in the browser on every load (`web/src/lib/layout.ts`, no library):
  edges with `max_loops` are back edges first, then any edge that closes a DFS cycle; layers
  are the longest path over forward edges; a few barycenter sweeps order each layer; columns
  left to right. Back edges are drawn dashed and animated from bottom handles.
- **Validation on every change**: the UI debounces (400 ms) and calls `POST /validate` with the
  last saved text plus the working model. The server applies the model in memory and validates
  the result, so the badges and the YAML tab preview always match what *Save* would write.
- **Approvals stay CLI only** (decision 6). The run view shows `next_commands`, e.g.
  `agentline approve <run>`, with a copy button and no approve button.

## Round-trip guarantees

`apply_model(text, model) -> (new_text, warnings)`:

1. **Unchanged model, byte-identical text.** Numbers compare by value (the browser sends `5.0`
   back as `5`), booleans don't equal numbers.
2. **Only changed fields are touched.** Field edits happen in place in the ruamel round-trip
   tree; nested maps and lists are edited key by key and item by item, so their style survives.
   Changing one value changes one line.
3. **Unchanged lines keep their exact bytes.** ruamel re-emits some things in its own style
   (`{ a: 1 }` becomes `{a: 1}`). So the tree is dumped before and after the edit, and only the
   lines that differ are replayed onto the original text (`rebase`). A changed line that ruamel
   restyled gets the original spacing back at character level, so `with: { env: eod }` becomes
   `with: { env: staging }`, not `with: {env: staging}`.
4. **Flow style is kept.** A flow map (`{ type: action, ... }`, including a whole node on one
   line) stays a flow map when edited or extended. Quote style is kept (`"..."` stays quoted).
   An inline prompt with newlines becomes a `|` block scalar and back.
5. **Nodes.** New nodes go to the end of `nodes` (block style; short scalar lists and small maps
   inline, like the example). Removed nodes are deleted; the blank line or comment that
   introduced the next node stays. Node order is kept. A rename sent with `prev_id` keeps the
   node's position and comments (the UI sets it).
6. **Edges.** A YAML `edges` item whose edges are all still in the model is kept verbatim. A
   changed map item (`{ from:, to:, ... }`) is edited in place (matched by `chain_index`). A
   chain that lost an edge is re-emitted; gaps are bridged with new unconditional edges, so
   inserting `x` between `a` and `b` gives `a -> x -> b -> ...` in the same chain, and renaming
   a node rewrites the chain in place. Leftover new edges are appended, consecutive plain ones
   joined into chains, conditional ones as flow maps.
7. **Warnings.** Every `# comment` of the original that is not in the result is reported
   (`comment '# tail comment' (line 41) could not be kept`). The UI shows them after saving.
8. **Never more blank lines in a row than the original had** (ruamel can keep the blank line
   on both sides of a deleted entry).

Tested by: byte identity (example and a heavily commented file), one-line diffs for scalar,
flow-map, list, quoting and top-level edits, add/remove field/node/edge, insert into a chain,
rename, comment survival, comment-loss warnings, indentation detection, and 40 seeded random
edit sequences (run locally with 1500 seeds) that must parse back to the edited model.

### Known comment-loss cases

- Deleting a node or an edges item deletes comments **inside** it (reported as warnings).
- Deleting every edge (or node) rewrites the list as `[]` (`{}`); comments in it are lost.
- When a chain is split, the first piece keeps the item's end-of-line comment; the others have
  none. Comments on their own line inside the `edges` list stay where ruamel attached them.
- A comment after a flow collection that is the **last** value of a node can end up attached
  to the next key when that node is deleted; ruamel's comment placement for flow collections is
  not uniform. The warning list is computed by comparing comments before and after, so any
  loss is reported rather than silent.
- Replacing a block value with a different shape (map -> scalar) drops comments inside it.
- Raw text edits (`PUT source`) are saved exactly as typed.

## API reference

All under `/api`. JSON in, JSON out. When `token` / `AGENTLINE_API_TOKEN` is set, every `/api`
route needs `Authorization: Bearer <token>` (401 otherwise; the UI asks for it once and keeps
it in `localStorage`). Webhook routes keep their own HMAC / shared-secret auth.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/pipelines` | | `{dir, pipelines: [{name, path, pipeline, nodes, issues}]}` (recursive `*.yaml`) |
| GET | `/pipelines/{name}` | | `{name, path, source, model}`; 404 if missing |
| PUT | `/pipelines/{name}/source` | `{source}` | `{name, source, model, issues}`; saves even when invalid; creates the file if new |
| PUT | `/pipelines/{name}/model` | `{model, base_source}` | `{source, model, warnings, issues}`; **409** `{detail, source}` if the file is not `base_source`; 422 if the file isn't a YAML mapping |
| POST | `/validate` | `{source, name?, model?}` | `{model, issues, source, warnings}`; with `model`, a dry-run apply onto `source` |
| GET | `/node-types` | | `{types: {<type>: {required, fields: [{name, kind, required, options?/source?, help?}]}}, runners, actions, deploy_class_actions, waiters, workspaces, edge}` |
| GET | `/runs?status=&limit=` | | `{runs: [run + terminal]}`, newest first, limit 1–500 (default 50) |
| GET | `/runs/{id}` | | `{run, executions, lineage, effects, costs, pipeline: {name, model} \| null, next_commands: [{node, command, description}]}` |
| GET | `/artifacts/{hash}` | | metadata + `{encoding: json \| text \| base64, value, truncated}`; capped at 1 MiB |

- **Names** are file paths relative to the pipelines directory without `.yaml`
  (`ticket-to-eod/pipeline`). They must match `[A-Za-z0-9_][A-Za-z0-9_.-]*` per segment, may
  not contain `.`/`..` segments, and must resolve (after symlinks) inside the directory;
  otherwise 400. Hidden directories, `node_modules`, `dist` and `.venv` are not listed.
- Writes are atomic (temp file, fsync, rename) and serialized by a lock; the 409 check and the
  write happen under the same lock.
- `executions` carry `status, attempt, reclaims, cost_usd, reason, error, detail` (with
  `detail.attachments` for transcripts), `result_ref` and timestamps.
- `next_commands`: for each waiting gate `agentline approve <run>` / `agentline reject <run>`;
  for each needs-review execution `agentline resolve <run> --retry | --skip | --fail`; `--node
  <id>` is added when there are several; always `agentline runs show <run>`. These strings
  must match the Phase 6 CLI; adjust `api/runs.py::next_commands` if its syntax differs.
- `pipeline` is the file whose `name:` equals the run's flow, read **now** (see limits).

### `create_server` (for the integrator)

```python
from agentline.api import create_server

server = create_server(
    app,  # agentline.App
    pipelines,  # compiled Pipelines (webhook triggers); may be []
    pipelines_dir="pipelines/",  # where the editor reads/writes *.yaml
    github_secret=None,  # default $GITHUB_WEBHOOK_SECRET
    jira_secret=None,  # default $JIRA_WEBHOOK_SECRET
    token=None,  # default $AGENTLINE_API_TOKEN; None/"" = open /api
    static_dir=None,  # default <repo>/web/dist when it has index.html
    registry=None,  # validation registry (default: built-ins)
    config=None,  # agentline.toml Config for validation
)
uvicorn.run(server, host="127.0.0.1", port=8787)  # binding is the caller's job
```

It builds the Phase 4 webhook app (`/healthz`, `/webhooks/github`, `/webhooks/jira`, unchanged)
and adds the editor and runs routers under `/api` and the UI at `/`. `registry` and `config` are
optional additions to the brief's signature so `agentline serve` validates with the same
registry it runs with.

## Running it

```bash
uv sync --extra server                    # uvicorn (fastapi is already in the dev group)
cd web && npm ci && npm run build && cd .. # builds web/dist (served at /)
uv run python -m agentline.api.dev --pipelines examples --db /tmp/x.db --port 8787
# open http://127.0.0.1:8787/   (--worker also runs a worker in-process)
```

Frontend development with hot reload: run the dev server as above, then `cd web && npm run
dev` and open http://localhost:5173. Vite proxies `/api` and `/healthz` to 127.0.0.1:8787
(`web/vite.config.ts`).

Production: `agentline serve` (Phase 6) calls `create_server` and binds; build `web/dist` first
or pass `static_dir`. Set `AGENTLINE_API_TOKEN` whenever the bind address is not loopback: the
editor API can write files in the pipelines directory.

Checks: `uv run pytest`, `uv run ruff check . && uv run ruff format --check .`, and in `web/`:
`npm test`, `npx tsc --noEmit`, `npm run build`.

### Verified

- Dev server on the example pipelines (copied to a scratch dir) and a ledger seeded with two
  example runs (fakes): `curl /healthz`, `/api/pipelines`, `/api/runs` and `/` all answer.
- An edit through `PUT /api/pipelines/ticket-to-eod/pipeline/model` (change `deploy.with.env`,
  add a `notify` node and edge) produced exactly 7 added/changed lines, nothing else touched.
- The built page loaded in a browser with no console errors; editing `implement.budget_usd`
  in the property panel and clicking Save changed exactly that line. The run view showed the
  waiting run colored by status, `×2` iteration badges, the approve/reject commands, and
  `implement#2`'s inputs (`review` from `await_review#1`) expanded to the full artifact.

## Tests

- Python: 327 passed, 2 skipped (was 242 + 2): 85 new in `tests/pipeline/test_roundtrip.py`
  (incl. 40 random seeds) and `tests/api/`.
- JS: 20 vitest tests in `web/src/lib/` (layout layers, back edges, barycenter order,
  model <-> React Flow, issue indexing and focus target, edits, run state).

## Decisions (proposals, numbered for the log)

1. **Rebase over ruamel output.** Edits go into the ruamel tree, but the file is written by
   replaying the dump diff onto the original text, because a plain ruamel dump restyles flow
   maps. This is what makes "one field changes one line" hold.
2. **`prev_id` for renames.** The model carries an optional `prev_id` so a rename keeps the
   node's position and comments instead of being a delete plus an append.
3. **Chain bridging.** Broken chains are re-joined through new plain edges (BFS, ≤ 4 hops) so
   common graph edits (insert, rename, delete a middle node) keep one chain line.
4. **`POST /validate` takes an optional `model`** for a dry-run apply. The UI validates the
   exact text it would save and shows it as the YAML preview.
5. **Pipeline names are relative paths** (`ticket-to-eod/pipeline`), listed recursively, so
   the example layout (one directory per pipeline with prompts) works unchanged.
6. **`create_server` returns the Phase 4 app with routers added**, rather than mounting it, so
   webhook paths and their auth are untouched. It adds optional `registry` / `config`.
7. **Open API without a token.** No token means no auth on `/api`; the dev entry binds to
   127.0.0.1 only. Production should set `AGENTLINE_API_TOKEN`.
8. **Artifacts over 1 MiB are cut**, JSON over the cap is returned as text; binary as base64.
9. **The run view draws the current pipeline file**, not a stored copy (there is none).
10. **Hash routing** (`#/pipelines/...`, `#/runs/...`) so StaticFiles needs no SPA fallback.
11. **Tooling versions**: React 18.3, @xyflow/react 12, Vite 8, Vitest 5, TypeScript 5.6
    (`npm audit` clean). `@types/react*` are dev-only type packages.

## Known limits

- The run graph comes from the pipeline file as it is now; if the file changed after the run
  started, nodes may be missing or extra (the executions table still lists everything).
- No auth roles: anyone with the token can edit every pipeline file under the directory.
- Positions are not stored; dragging a node is local and lost on the next change (by design).
- Trigger, `defaults` and `description` are edited in the YAML tab (the graph panel edits
  budget only); unknown top-level keys are never touched by model saves.
- The dev entry registers only pipelines that validate at startup; a pipeline fixed in the
  editor needs a server restart to receive webhooks (editing works regardless).
- `next_commands` strings assume the Phase 6 CLI syntax described in ARCHITECTURE.md.
- No browser end-to-end tests in CI; the UI was checked by hand (see Verified).
