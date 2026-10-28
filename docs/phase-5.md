# Phase 5: artifacts and context handoff

Branch `phase-5`. Node results, transcripts and diffs now live in a content-addressed artifact
store. Every read and write is recorded as lineage, and large inputs reach handlers and prompts
as a short reference instead of the full value.

## Data model

Two new tables, both `CREATE TABLE IF NOT EXISTS` in `ledger/sqlite.py` `SCHEMA`. An existing
ledger gets them the next time it is opened, and nothing else changes (see
`tests/artifacts/test_upgrade.py`).

| Table | Columns |
|---|---|
| `artifacts` | `hash` PK (sha256), `kind` (`json`/`file`), `media_type`, `size`, `summary`, `storage` (`inline`/`file`), `path`, `created_at` |
| `artifact_links` | `run_id`, `execution_id`, `node_id`, `iteration`, `name`, `hash`, `direction` (`read`/`wrote`), `at`, `source_execution_id`; UNIQUE(`execution_id`, `name`, `direction`, `hash`) |

- **Hash.** For kind `json` it is the sha256 of the canonical JSON (`ids.canonical_json`: sorted
  keys, compact). For kind `file` it is the sha256 of the bytes. So a result's hash is the same
  `ref` that Phase 1/2 used for its `blobs` row.
- **Storage.** Inline artifacts are stored in the existing `blobs` table, keyed by the hash.
  File artifacts are stored at `<root>/<hash[:2]>/<hash>`. `root` is `$AGENTLINE_HOME/artifacts`
  (default `~/.agentline/artifacts`), or `App(artifact_root=...)`. The root is resolved lazily,
  so nothing touches disk until the first file artifact is written.
- **Atomic writes.** Each file is written to a temp file in the target directory, then fsync,
  then `os.replace`. The file is complete before the row is inserted in the same transaction,
  so a row always points at a complete file. Writes dedupe automatically: an existing row is
  returned and the summarizer is not called again.
- **LedgerTx protocol.** It gains four operations: `put_artifact` (insert if absent),
  `get_artifact`, `add_artifact_link` (insert if absent) and `list_artifact_links(run_id)`.
  Content goes through the existing `put_blob` / `get_blob`. A Postgres ledger (Phase 8) only
  has to implement these four.
- `Artifact` and `ArtifactLink` dataclasses live in `ledger/base.py`. `ArtifactStore`,
  `ArtifactRef` and the summarizers live in `agentline/artifacts.py`.

### Where artifacts are written
- `Scheduler.complete` and the `on_exhausted: <node>` continue path call
  `ArtifactStore.put_json(result)`. `result_ref` is now the artifact hash, and a `wrote` link is
  recorded under the node's `out_name`. This happens in the same fenced transaction as the
  state change. `read_result` / `App.result` load through the store.
- `Scheduler.resolve_inputs` still loads **only declared inputs** (plus `payload`). It records
  a `read` link for each input it loads, with `source_execution_id` set to the producing
  execution, which is how lineage can say "`deploy#2` read `branch` from `implement#2`".
- `ctx.attach(name, data, media_type=None)` writes a kind-`file` artifact and a `wrote` link
  under `name`. It also notes `{ref, size, summary}` in `detail.attachments[name]`, as before
  plus `summary`. The media type is guessed: `text/x-diff` for `diff` (or `*.diff`, `*.patch`),
  `application/json` for `*.json`, `text/plain` for text, otherwise `application/octet-stream`.

## Thresholds

| Setting | Default | Where |
|---|---|---|
| inline storage max (`inline_max`) | 256 KiB | `App(inline_max=)`, `ArtifactStore(inline_max=)` |
| handoff max (`handoff_max`) | 64 KiB | `App(handoff_max=)`, `Scheduler(handoff_max=)` |
| summary length | ≤ 300 chars | `artifacts.SUMMARY_MAX` |

`App(artifacts=ArtifactStore(...))` passes a fully configured store, for example one with a
different summarizer.

## Auto summary

`summarize(value_or_bytes, media_type) -> str` is deterministic and uses no LLM.
- **JSON object:** the keys, each with a short value or a length:
  `{summary: "Fix login", steps: [3 items], files: [2 items]}`. Strings show their first line,
  clipped to 40 chars. Nested objects show as `{n keys}`. Keys keep the order of the first write.
- **JSON list:** `[3 items: int, str]`. A JSON string is summarized as text.
- **Text:** the first 3 non-empty lines, then `(N lines)`.
- **Diff** (`text/x-diff`, `text/x-patch`, or text that looks like a git diff):
  `2 files changed, +3 -1: src/login.py, tests/test_login.py`.
- **Binary** or undecodable text: `image/png, 2.0 KiB`.

**Seam:** `ArtifactStore.summarizer` is a `Callable[[value, media_type], str]`. It runs once
per new artifact, inside the writing transaction. An LLM summarizer can replace it later, but
it would have to run outside the transaction (see proposal 6).

## Reference format (large inputs)

When a resolved input's artifact is larger than `handoff_max`, the handler receives this plain
JSON dict in place of the value:

```json
{
  "$artifact": "<sha256>",
  "summary": "{kind: \"text\", body: \"line 0: something happened\"}",
  "size": 131000,
  "media_type": "application/json",
  "path": "/home/me/.agentline/artifacts/ab/ab12…"
}
```

- `path` always exists. An inline artifact is materialized to the file store on demand,
  without changing its row.
- **Templates.** `{{logs}}` renders the reference as `<summary>\n(full content: <path>)`.
  `{{logs.kind}}` (field access) reads the field from the referenced file, so templates,
  routers (`switch: logs.kind`) and `with:` params work the same at any size.
- **Handlers.** `await ctx.load(inputs["logs"])` returns the full value: a JSON value, or bytes
  for file artifacts. A value that is not a reference is returned unchanged, so handlers can
  always call it. `artifacts.is_ref(v)` and `ArtifactRef.from_value(v)` (with `.read_file()`)
  are the helpers.
- Small inputs are passed inline exactly as before, so every Phase 1/2 test is unchanged.

## Lineage and artifact API

- `app.lineage(run_id)`: one entry per execution, in creation order:
  ```python
  {
      "execution_id": "x…",
      "node": "implement#2",
      "node_id": "implement",
      "iteration": 2,
      "status": "done",
      "reads": [
          {"name": "review", "hash": "…", "summary": "…", "size": 52, "from": "await_review#1"}
      ],
      "writes": [{"name": "branch", "hash": "…", "summary": "…", "size": 41}],
  }
  ```
- `app.artifact(hash)`: the metadata (`hash, kind, media_type, size, summary, storage, path,
  created_at`) plus `value`, or `None`.
- `app.artifacts(run_id)`: every artifact the run wrote, in write order, with `name`, `node`,
  `execution_id` and the metadata.

These back `agentline runs show` (Phase 6) and the run view (Phase 7).

## Tests

25 new tests in `tests/artifacts/`. Full suite: **130 passed**. ruff check and format are clean.
- `test_store.py`: inline and file round trips, file layout, dedupe (one row, one file, one
  summarizer call), atomic writes (a failing `os.replace` leaves no file, temp file or row),
  materialization, legacy bare-blob refs, the `AGENTLINE_HOME` default.
- `test_summary.py`: JSON object, list and scalars, text, diff (explicit and sniffed), binary,
  undecodable text, the length cap, media type guessing.
- `test_lineage.py`: the example pipeline with one review loop. `implement#2` read exactly
  `ticket`, `plan` (from `plan#1`) and `review` (from `await_review#1`). `deploy#1` was skipped,
  and `deploy#2` read `branch` from `implement#2` (`sha2`). Also covered: `result_ref` equals
  the artifact hash, attachments are `text/plain` file artifacts, and `app.artifacts`.
- `test_handoff.py`: a ~120 KiB input reaches an agent as a reference. The prompt contains the
  summary plus `(full content: <path>)`, and `{{logs.kind}}` and a router `switch: logs.kind`
  still work through the file. Also: `handoff_max` keeps inputs inline when raised,
  `ctx.load` dereferences, and file storage is used over `inline_max`.
- `test_upgrade.py`: a DB built with the Phase 2 schema, including a done execution with a bare
  result blob, opens and gains the tables. The old result still reads, and new runs record
  lineage.

## Chaos acceptance

`uv run python -m demo.chaos --target pipeline --jobs 50 --seed 1 --expect-clean`

```
chaos report: target=pipeline seed=1 jobs=50 concurrency=4
  kills (SIGKILL)          18   unexpected exits 0
  elapsed                  18.27 s
  runs done / not done     50 / 0
  PRs total / expected     50 / 50
  tickets with dup PRs     0 (extra PRs: 0)
  done without a PR        0
  LLM calls / expected     202 / 150 (extra: 52, bound kills x concurrency: 72)
  deploys / dup tickets    50 / 0
  CLEAN                    yes
  engine                   {'runs_total': 50, 'runs_by_status': {'done': 50}, 'lease_reclaims': 63, 'reviews_sent': 100, 'approvals_sent': 50}
```

## Known limits

- **No garbage collection** of artifact files or blobs. A file written in a transaction that
  later rolls back (stale lease, crash) stays on disk as an orphan. It is harmless because it
  is content-addressed, and a later write of the same content reuses it.
- **Attachment links are not fenced.** `ctx.attach` commits its artifact and `wrote` link
  immediately, so a crashed or failed attempt's transcripts appear in lineage too. This is
  deliberate, because those transcripts are useful for debugging. Result links are fenced.
- **References point into the store, not the workspace.** `path` is under the artifact root.
  ARCHITECTURE §9 says "a path inside the workspace". The Phase 3 runner should copy or mount
  the file into the workspace and rewrite `path` (proposal 3).
- **Actions and runners that index `call.inputs[...]` / `req.inputs[...]` directly** see the
  reference dict for large inputs. They should call `await ctx.load(...)`, or use templated
  `with:` params, which do resolve. No built-in action currently gets inputs over 64 KiB.
- **Same bytes, two kinds.** A file artifact whose bytes equal some value's canonical JSON
  shares that value's hash and row, and the first kind written wins. The bytes are identical,
  so only `load()`'s return type is affected.
- **Summaries follow the first write's key order.** Dedupe makes them stable per hash, but two
  ledgers that wrote the same value with different key orders could store different summaries.
- **Template field access re-reads the file** for each `{{ref.field}}` (no cache). This is fine
  for a handful of references per prompt.
- **Multi-host workers need a shared artifact root.** Today it is a local directory, the same
  constraint as the SQLite ledger (Phase 8).

## Decisions (proposals for the user)

1. **Hash = canonical-JSON sha256 for results.** `result_ref` keeps its Phase 1/2 meaning, so
   old ledgers need no data migration. Old refs without an artifact row are loaded as JSON blobs.
2. **`source_execution_id` on `artifact_links`.** This column is an extra beyond the brief.
   Without it, "read `branch` from iteration 2" could only be inferred by matching hashes,
   which is ambiguous when two iterations produce identical output.
3. **Reference `path` is the store path.** A materialized inline artifact gets a file in the
   store. Phase 3 workspaces should expose it inside the container (read-only mount of the
   artifact root, or a copy into `.agentline/inputs/<name>`).
4. **Field access through references.** `{{x.field}}`, router `switch:` and `with:` read the
   referenced file transparently. Only a bare `{{x}}` renders as summary + path. Otherwise a
   large `review` would break `switch: review.state`.
5. **Handoff applies to all node kinds** (agents, actions, gates, waits, routers). Templating
   resolves references, so routers and gates are unaffected. Python handlers opt in with
   `ctx.load`.
6. **Summarizer seam runs inside the write transaction.** It is fine for the deterministic
   default. An LLM summarizer should run asynchronously after the write and update `summary`
   (needs an `update_artifact_summary` op later), so a slow or paid call never holds the SQLite
   write lock.
7. **Attachments are linked immediately (unfenced)**, so failed attempts' transcripts stay
   visible.
8. **Thresholds:** 256 KiB inline, 64 KiB handoff, 300-char summaries. These are configurable on
   `App`, and not yet exposed in `agentline.toml` (Phase 6 CLI could add `[artifacts]`).
