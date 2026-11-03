# agentline architecture

Status: Phase 0 design. Sections marked *(Phase N)* are designed here and built in that phase.

## 1. Goals and non-goals

**Goals (v0.1)**
- Run multi-step delivery pipelines (ticket → plan → implement → PR → review loop → gate →
  deploy → test) where steps are AI agents, actions, gates, routers and waits.
- Durable by default: a crash at any point never duplicates an external side effect that has a
  key or a check, never repeats a completed step, and never loses a run.
- Pipelines are plain YAML files; the visual editor reads and writes the same file.
- Agent-agnostic runners; coding steps in per-execution Docker containers with their own worktree.
- Safe by default: per-step tool allow-lists, budgets, secret redaction, human gate before deploy.

**Non-goals (v0.1)**: multi-tenant auth, billing, hosted SaaS, non-GitHub/Jira integrations,
distributed multi-host scheduling, plugin marketplace.

## 2. Components

```
             webhooks (Jira, GitHub)          CLI / editor
                      │                            │
                      ▼                            ▼
              ┌───────────────┐            ┌──────────────┐
              │  API (FastAPI)│◀──────────▶│   Ledger     │◀───────┐
              └───────┬───────┘  submit /  │ (SqliteLedger)│        │
                      │          resume    └──────┬───────┘        │
                      │                           │ claim (lease)  │ commit result +
                      ▼                           ▼                │ successor states
              ┌──────────────────────────────────────────┐        │
              │ Worker (N concurrent, heartbeats)        │────────┘
              │  └ Graph executor: node type → handler   │
              │      agent  → Runner (python-fn, shell, claude-code, …)
              │      action → Action (jira.*, github.*, env.*, shell.run) via EffectGuard
              │      router → pick out-edge by switch value
              │      gate / wait → park run, register correlation key
              └───────┬───────────────────────┬──────────┘
                      ▼                       ▼
            Workspace (docker +          Artifact store
            git worktree from mirror)    (content-addressed + summary)
```

| Component | Responsibility | Phase |
|---|---|---|
| Ledger | Storage interface; `SqliteLedger` default, Postgres later | 1 |
| Worker | Claim ready node executions under a lease, heartbeat, reclaim expired, run N in parallel | 1 |
| Step context (`ctx`) | Gives a step its inputs, budget, logger, and `ctx.effect()` | 1 |
| Graph executor | Compiles YAML into nodes/edges; decides successors after each execution | 2 |
| Runners | `run(inputs, workspace, limits) -> outputs + transcript + cost` | 3 |
| Workspaces | Docker container per node execution, worktree on `agentline/<run_id>` | 3 |
| Actions | jira.get_ticket, jira.comment, github.open_pr, github.read_reviews, env.deploy, env.test, shell.run, image.build, image.propose | 3–4 |
| Webhooks | Verify signatures, map events to submissions and to resuming waits | 4 |
| Artifact store | Content-addressed blobs + JSON, auto summary, lineage | 5 |
| CLI, audit, redaction | `agentline run / runs / approve / resolve`, audit log, structured logs | 6 |
| Editor | React Flow canvas over the YAML, live run view | 7 |

## 3. Execution model: explicit per-node state machine

*Decision (user, Phase 0): explicit state machine, not replay.*

A run is a set of **node executions**. Each execution is one row with an explicit state. The
worker never re-runs a whole handler from the top:

1. Claim one execution in `ready` state (atomic `UPDATE … WHERE status='ready'`, sets
   `lease_owner`, `lease_expires`, increments `lease_epoch`).
2. Load its declared inputs (artifact refs), run the node's handler **once**.
3. In **one SQLite transaction**: store the result (journal + artifacts), mark the execution
   `done`, and create/mark ready its successor executions. The transaction is fenced by
   `lease_epoch` — if the lease was reclaimed meanwhile, the commit is rejected.

A crash before step 3 means the execution's result was never recorded; after the lease expires
it is reclaimed and re-run. That's the only repeat, so paid calls repeat at most once per crash
per in-flight execution. Steps that do external side effects use `ctx.effect` (§6) so that the
re-run doesn't duplicate them.

No determinism rules for user code: a step is a plain async function that sees only its inputs.

Loop iterations are separate executions: `implement#1`, `implement#2` (`iteration` column).

### Python API *(Phase 1)*

```python
app = App(SqliteLedger("agentline.db"))
flow = app.flow("ticket")


@flow.step("plan")
async def plan(ctx: StepContext, inputs: dict) -> dict:
    return {"plan": await llm(ctx, inputs["payload"])}  # journaled once per execution


@flow.step("open_pr", after="plan")
async def open_pr(ctx: StepContext, inputs: dict) -> dict:
    branch = inputs["plan"]["branch"]
    pr = await ctx.effect(
        "create_pr",
        key=f"pr:{branch}",  # idempotency key passed to the API if it supports one
        check=lambda: github.find_pr(branch),  # "did it already happen?" → result or None
        do=lambda: github.create_pr(branch),
        on_unknown="halt",
    )
    return {"pr": pr}


await app.submit("ticket", payload, dedupe_key="jira:PROJ-123:assigned")
```

The YAML executor (Phase 2) compiles a pipeline into the same flow/step/edge model, so there is
one engine with two front-ends.

## 4. Data model

All IDs are text (ULID-like, sortable). Timestamps are UTC epoch seconds (REAL).

| Table | Key columns |
|---|---|
| `runs` | id, pipeline, pipeline_hash, trigger_payload_ref, dedupe_key UNIQUE, status, cost_usd, budget_usd, created_at, updated_at, error |
| `node_executions` | id, run_id, node_id, iteration, attempt, status, lease_owner, lease_expires, lease_epoch, next_attempt_at, correlation_key, cost_usd, started_at, finished_at, error |
| `journal` | execution_id UNIQUE, result_json, transcript_ref, cost_usd, committed_at |
| `effects` | execution_id, name, idempotency_key, status (`intent`/`committed`/`unknown`/`failed`), result_json, advisor_report, updated_at; UNIQUE(execution_id-without-attempt, name) |
| `artifacts` | hash (sha256) PK, kind (`json`/`file`), size, summary, media_type, path |
| `artifact_links` | execution_id, artifact_hash, name, direction (`read`/`wrote`) — lineage |
| `audit_log` | id, at, actor, action (`approve`, `resolve`, `deploy`, `cancel`…), run_id, node_id, detail_json |
| `events` | correlation_key, payload_ref, received_at, consumed_by — inbound webhook/approval events |

Effects are keyed by *(run, node, iteration, name)*, **not** by attempt, so a retried attempt of
the same execution sees the effect committed by an earlier attempt.

Large payloads (transcripts, diffs, files) live in the artifact store; tables hold refs only.

## 5. State machines

### Run

```
pending ──▶ running ──▶ done
              │ ▲  ╲
              ▼ │   ╲──▶ needs-review ──(resolve)──▶ running
            waiting       (unknown effect, loop bound hit, advisor says "human")
              │
 running ──▶ failed ──(backoff)──▶ running        failed ──(attempts exhausted)──▶ dead
 any non-terminal ──(cancel)──▶ cancelled
```

Run status is **derived** from its executions after every commit:
any `running` → running; else any `ready` → pending/running; else any `waiting` → waiting;
any `needs-review` → needs-review; any `dead` → dead; all terminal and the end node(s) done → done.
Allowed `(from, to)` pairs live in one table in code and every transition is checked against it.

### Node execution

```
ready ──claim──▶ running ──▶ done
                   │  ├──▶ waiting ──(event)──▶ done
                   │  ├──▶ needs-review ──(resolve: retry|skip|fail; approve --proposal)
                   │  └──▶ failed ──(attempt < max, after backoff)──▶ ready
                   │                └─(attempt = max)──▶ dead
                   └── lease expires (worker died) ──▶ ready (attempt + 1, same execution)
(not on taken path) ──▶ skipped
```

Retries: `max_attempts` (default 3), exponential backoff with jitter
(`base * 2**attempt`, capped), stored as `next_attempt_at` so no worker sleeps on it.

**As built (Phase 1)** — `src/agentline/scheduler.py`:
- **Joins use tokens.** Each execution counts `tokens_live`/`tokens_skip` from its non-loop
  in-edges; it becomes `ready` when all arrived and at least one is live, `skipped` (and passes
  skips on) when all are skips. So a join after a router never waits for the branch not taken.
- **Crashes don't use attempts.** A reclaimed lease bumps `reclaims`, not `attempt`; a separate
  cap (`max_reclaims`, default 20) sends a crash-looping execution to `dead`.
- **`resolve`** on a needs-review execution: `retry` (same attempt number), `skip`, `fail`,
  `done` with a result (for a router: `{"route": "approved"}`). With an unknown effect, add
  `effect_decision="redo"` (do it again) or `"done"` with the result a human verified.
- **Events** are stored before delivery; a wait that parks later consumes an event received
  after the execution was created, so a webhook racing the park isn't lost.
- **Known limit**: a node inside a loop body must not join with a node outside it (iteration
  2 would wait for a token the outside node never resends). The run then shows up as
  `needs-review` (stuck join) instead of hanging; the Phase 2 validator rejects that shape.

## 6. Effect guard

For every external side effect (`ctx.effect(name, key=?, check=?, do=, on_unknown=)`):

1. Look up the effect record. If `committed` → return stored result, don't call anything.
2. If none → insert `intent` (committed to SQLite **before** the call).
3. If a record was already `intent` (we crashed last time somewhere around the call):
   - if `check` exists → call it. Found → mark `committed` with that result. Not found → go on.
   - elif `key` exists and the API honors idempotency keys → go on (the API dedupes).
   - else → state is **unknown** → apply `on_unknown`.
4. Call `do()` (passing `key` when supported). Mark `committed` with the result.

**`on_unknown`** *(decision: user, Phase 0)*:
- `halt` (default): execution → `needs-review`; run → `needs-review`; human runs `agentline resolve`.
- `retry`: call `do()` again, accepting a possible duplicate (for harmless effects like a comment).
- `ai`: an advisor LLM gets the intent record, the action's docs and any partial evidence and
  answers `retry` / `halt` / `human` with a reason. `retry` proceeds; `halt`/`human` → needs-review
  with the advisor report shown in `runs show`. Advisor calls count against the run budget.
  **Not allowed on deploy-class effects** (validator error).

**Residual crash window (honest version).** The window is between the external system applying
the effect and our `committed` write. If we crash there:
- with a **check**: safe, the check finds it — *unless* the external system is eventually
  consistent and the check runs before the effect is visible (GitHub search can lag; we use
  direct lookups like "list PRs with head=branch", which are consistent).
- with an **idempotency key**: safe only as long as the provider keeps the key (often 24h).
- with **neither**: we cannot know → `on_unknown`. We never claim exactly-once here.
- **LLM/agent calls** are not guarded effects; they are the step's own work. A crash mid-call
  repeats that call once. Accepted cost: at most one repeat per crash per in-flight execution.
- Effects **inside** an agent (e.g. Claude Code running `git push`) aren't visible to the guard;
  that's why agents get a scoped workspace and allowed tools, and PR/deploy happen in actions.

## 7. Leases, heartbeats, fencing

- Claim sets `lease_expires = now + lease_ttl` (default 30 s) and bumps `lease_epoch`.
- A running execution heartbeats every `lease_ttl/3`, extending `lease_expires`.
- A reaper (every worker does it on its poll loop) moves `running` executions with expired leases
  back to `ready` with `attempt + 1`.
- **Fencing**: every write by a worker includes `WHERE lease_epoch = :my_epoch`. A worker that
  was paused (GC, laptop sleep) past its lease can't commit over the new owner's work.
- SQLite runs in WAL mode; claims and commits use `BEGIN IMMEDIATE`. One host, many worker
  processes. (Multi-host needs Postgres, Phase 8.)

## 8. Loops, routers, gates and waits

- **Router**: `switch: review.state` reads a field of an input artifact and follows the out-edge
  whose `when` equals the value; `default` edge if none match; no match and no default → fail.
  Successors on non-taken edges get `skipped`.
- **Bounded loop**: an edge with `max_loops: N` back to an earlier node creates a new execution
  of that node with `iteration + 1`, and fresh executions for the nodes downstream of it. When
  `iteration` would exceed `N + 1`, follow `on_exhausted` (`needs-review` default, or `fail`, or a
  node id). Inputs resolve to the **latest** iteration of each producing node.
- **Optional inputs** (`review?`) resolve to "absent" when no execution has produced them yet.
- **Gate**: execution goes `waiting` with correlation key `gate:<run_id>:<node_id>`. No worker is
  held. `agentline approve <run>` (CLI only in v0.1) writes an event + audit row; the event is
  journaled as the gate's result and the execution goes `done`; successors become `ready`.
  `agentline reject` fails it.
- **Wait** (`type: wait, for: github.review`): goes `waiting` with a correlation key from the
  input (`github:pr:<owner>/<repo>#<n>`). The GitHub webhook writes an `events` row; matching
  waiting executions consume it in a transaction. Also polls as a fallback (`poll_every`) in
  case a webhook is missed. Events that arrive **before** the wait parks are kept and consumed
  when it does, so nothing is lost to a race.

## 9. Pipeline spec (v1)

*Decisions (user, Phase 0): spec changes accepted; deploy/test commands come from agentline config.*

```yaml
version: 1
name: ticket-to-eod
trigger: { type: webhook, source: jira, event: issue_assigned }
budget_usd: 5.0
nodes:
  read_ticket:  { type: action, uses: jira.get_ticket, out: ticket }
  plan:
    type: agent
    runner: claude-code
    prompt: prompts/plan.md            # template file, relative to this pipeline file
    in: [ticket]
    out: plan
    output_schema: schemas/plan.json   # output is checked before successors run
    allowed_tools: [read]
  implement:
    type: agent
    runner: claude-code
    prompt: prompts/implement.md
    in: [plan, review?]
    out: branch
    allowed_tools: [read, edit, bash]
    workspace: { type: container, image: auto }   # see §11.1
    budget_usd: 2.0
  open_pr:      { type: action, uses: github.open_pr, in: [branch], out: pr }
  await_review: { type: wait, for: github.review, in: [pr], out: review }
  route:        { type: router, switch: review.state }
  approve:      { type: gate, prompt: "Deploy to EOD?" }
  deploy:       { type: action, uses: env.deploy, with: { env: eod }, in: [branch], out: deployment }
  test:         { type: action, uses: env.test, with: { env: eod }, in: [deployment], out: test_report }
edges:
  - read_ticket -> plan -> implement -> open_pr -> await_review -> route
  - { from: route, to: implement, when: changes_requested, max_loops: 3, on_exhausted: needs-review }
  - { from: route, to: approve, when: approved }
  - approve -> deploy -> test
```

`agentline.toml` (not in the target repo):

```toml
[repos.app]
remote = "git@github.com:acme/app.git"   # mirrored to ~/.agentline/mirrors/app.git

image_profile = "python"                 # optional; otherwise `image: auto` detects it (§11.1)

[environments.eod]
deploy = "./ops/deploy.sh eod {branch}"
test   = "./ops/test.sh eod"
deploy_class = true                      # env.deploy is always deploy-class
```

### Node anatomy

There is **one generic executor**. A node is configuration only; behavior comes from its
`type`, and each type is implemented once in the engine. Code is written per *runner* and per
*action* (one integration, reused by every pipeline), never per node.

| Part | Fields | Applies to |
|---|---|---|
| Identity | node id (the YAML key), `type` | all |
| Data contract | `in: [a, b?]`, `out: name` | all (`router`/`gate` usually no `out`) |
| Type config | see below | per type |
| Policy | `retries`, `timeout`, `budget_usd`, `effect: { key, on_unknown }` | all (`effect` on actions) |

| Type | Generic behavior | Type config |
|---|---|---|
| `agent` | load inputs → render prompt → runner → check output → store artifact | `runner`, `prompt`, `output_schema?`, `allowed_tools`, `workspace?`, `max_turns?` |
| `action` | call a registered action through the effect guard | `uses`, `with` |
| `router` | read a field, follow the matching edge | `switch` |
| `gate` | park until `agentline approve` | `prompt` |
| `wait` | park until a matching event | `for`, `poll_every?` |

Custom logic without writing an action: `shell.run`, or the python-function runner pointing at
your own function (`runner: python, function: mypkg.steps:triage`).

### Prompts *(Phase 2 rendering, Phase 3 runners)*
- `prompt:` is a file path (preferred; long prompts stay reviewable in diffs) or inline text
  (`prompt: |`). Files resolve relative to the pipeline file.
- Templating is deliberately tiny: `{{artifact}}` and `{{artifact.field.sub}}`, no logic, no
  Jinja (stdlib only). The validator rejects references to artifacts not listed in `in`.
  Optional inputs render as empty when absent; `{{#review?}}…{{/review?}}` includes a block
  only when the input exists.
- Artifacts over a size limit render as their **summary plus a path** inside the workspace
  (Phase 5), so the agent reads the file only if it needs it.
- The rendered prompt's hash and the template's git blob id are recorded on the execution, so
  you can see exactly which prompt produced an output.

### Output schemas
- `output_schema:` is a JSON Schema file (or inline map). The engine checks the agent's JSON
  output with a small stdlib validator covering the common subset: `type`, `properties`,
  `required`, `items`, `enum`, `minItems`, `additionalProperties`.
- On failure the node gets **one repair attempt**: the same runner is called again with the
  validation errors appended. It counts as an attempt and against the budget. If that fails too,
  the node fails normally (retry policy, then dead).
- Without a schema, the output is stored as-is (text or JSON).

### Changes from the first draft, and why
| Change | Why |
|---|---|
| `version: 1` | Lets the format evolve with migrations. |
| `type: wait` instead of an action `github.wait_review` | Waiting is an engine concept (park, free the worker, resume by correlation key). It should be visible in the type, not hidden inside an action. |
| Router `switch: <artifact.field>`, cases = edge `when` labels | No expression language, no `eval`; trivial to render and edit in the editor. Expressions can be added later. |
| Conditional/loop edges as maps; `a -> b -> c` only for unconditional chains | The `-[label, max_loops: 3]->` mini-syntax is harder to parse, validate and round-trip. |
| `on_exhausted` on loop edges | Says what happens when the bound is hit (default `needs-review`). |
| `implement` reads `review?` (was `review_feedback?`) | Nothing produced `review_feedback`; `review` is absent on iteration 1. |
| `test` reads `deployment` | Makes the data dependency explicit. |
| `idempotent_by` moved into action definitions | Each action declares its strategy (`key` / `check`); YAML may override with `effect: { key: …, on_unknown: … }`. |
| `env.deploy` / `env.test` from config | User decision: commands live in agentline config, so a target repo can't change what deploy runs. |
| `prompt` + `output_schema` on agents | Without a prompt, two agent nodes can't differ; a schema catches malformed output before it reaches the next node. |
| `workspace: { type, image }` map | Room for the image choice (§11.1) and later network/mount options. `workspace: container` stays as shorthand for `image: auto`. |

### As built (Phase 2)
- `agentline/pipeline/`: `spec.py` (parse; every issue has path, line, column, hint),
  `validate.py` (rules below), `compile.py` (one handler per node type), `template.py`,
  `schema.py`. `load_pipeline(path, registry, config)` → `Pipeline(spec, graph)`;
  `App.register_pipeline(p)`; runs are submitted by pipeline `name`.
- A router's result is `{"route": <label>, "switch": <path>, "value": <raw value>}`.
- A gate parks on `gate:<execution id>`; `App.approve/reject(run, node?, actor=)` deliver or
  fail it, honoring `approvers:`. A wait parks on the waiter's correlation key.
- Gate/wait `timeout` is recorded but not enforced yet (no expiry poller in v0.1).
- Agent transcripts are stored with `ctx.attach()` (content-addressed blob, ref in the
  execution's `detail.attachments`); Phase 5 moves this to the artifact store.

### Validator rules *(Phase 2)*
- Node ids unique, `type` known, required fields per type, unknown fields rejected (typos).
- **Edges are control flow; `in`/`out` are data.** Every required `in` must be produced by a node
  that precedes it on *every* path; optional inputs (`x?`) on *some* path.
- Cycles allowed only through edges with `max_loops`; any other cycle is an error naming the cycle.
- All nodes reachable from the start; router out-edges have distinct `when`; non-router nodes have
  no `when`.
- Every deploy-class action is **dominated by a gate** (every path to it passes a gate).
- Prompt files and schema files exist; template references are declared inputs; `image:` refs
  are on the registry allow-list.
- `on_unknown: ai` rejected on deploy-class actions.
- Errors carry the YAML line/column and a fix hint.

## 10. Security model

- **Tools**: each agent node has `allowed_tools` (default: read-only). Runners translate it
  (e.g. Claude Code `--allowedTools`). Actions are not agent tools.
- **Budgets**: run `budget_usd` and optional node `budget_usd`. Checked *before* each paid call
  using a reservation (estimated max cost), settled with the actual cost after. Exceeding → node
  fails without retry, run → `needs-review`.
- **Secrets**: only from env vars / a secrets file referenced by name (`secret: GITHUB_TOKEN`).
  Never stored in the ledger. A redactor holds every loaded secret value and scrubs logs,
  transcripts, journal entries and errors before they're written.
- **Containers**: per-execution, no host env passed through except declared secrets, worktree
  mounted read-write, nothing else of the host. Network policy per runner (later).
- **Deploy**: deploy-class actions must be gated (validator), and every deploy writes an audit
  row with run, approver, commit SHA and environment.
- **Webhooks**: GitHub HMAC-SHA256 signature check; Jira shared-secret check; constant-time compare;
  reject replays by delivery id.
- **Approvals**: CLI only in v0.1; actor from config or `$USER`, recorded in the audit log.

## 11. Workspaces *(Phase 3)*

*Decision (user, Phase 0): clone from a local bare mirror.*
- `agentline mirror` keeps `~/.agentline/mirrors/<repo>.git` fetched.
- Each coding node execution: `git worktree add` on branch `agentline/<run_id>` (iteration 2
  continues the same branch), start a container with the worktree mounted, run the runner,
  capture diff + head SHA as artifacts, destroy the container, keep the branch.

### 11.1 Container images

*Decision (user, Phase 0): layered, hash-cached images; a missing tool is shown to the user
and an image proposal is drafted automatically; nothing is built until the user approves.*

Goal: containers start in about a second, yet have the tools the task needs. The approach is to
**build images rarely and cache them by content hash**, and never install tools per run.

**Which image a node gets** (first match wins, and the resolved digest is recorded on the
execution so a retry uses the same image):
1. `workspace.image: <ref>` on the node, e.g. `ghcr.io/acme/agent-py@sha256:…` (digest pinning
   recommended; tags are resolved to a digest at run start).
2. `image` or `image_profile` for the repo in `agentline.toml`.
3. `image: auto` (the default): a **deterministic resolver**, not an LLM:
   `.devcontainer/devcontainer.json` → `Dockerfile.agentline` in the repo → marker files
   (`pyproject.toml`/`requirements*.txt` → python, `package.json` → node, `go.mod` → go;
   several → combined profile) → base image only.

**Layers**, each cached:

| Layer | Contents | Rebuilt when |
|---|---|---|
| L0 `agentline/runner-base` | debian-slim, git, ripgrep, jq, curl, claude-code CLI (later codex/aider), non-root user | agentline version changes |
| L1 toolchain profile | e.g. python 3.12 + uv, or node 22 + pnpm; defined in `agentline.toml [images.<profile>]` or the repo's devcontainer/Dockerfile | profile definition changes |
| L2 repo dependencies (optional) | dependencies installed from the repo's lockfiles | lockfiles change |

The tag is `agentline/img-<repo>-<sha256(layer inputs)[:12]>`. Building is a journaled step
whose check is `docker image inspect <tag>`, so it's naturally idempotent and crash-safe. Only
the first run after a lockfile or profile change pays for a build; every other run starts from
cache. Per-repo cache volumes (pip/uv/npm/go) are mounted so even an L2 rebuild is incremental.
A pipeline may add an explicit `prepare_image: { type: action, uses: image.build }` node in
parallel with `plan`, so a needed build overlaps LLM time instead of delaying `implement`.
Without it, the build runs on demand before the first container node.

**Can the orchestrator decide or build an image?** Yes, as a **proposal**, never silently:
- Agent containers run as non-root with no package-registry network access by default, so an
  agent can't install tools mid-run (that would be slow, non-reproducible and a supply-chain risk).
- When an agent hits a missing tool, the runner records `missing_tools: [...]` (the runner
  instructs the agent to report it, and also detects `command not found` in the transcript).
- The execution then goes to `needs-review` with reason `missing_tools` (retrying in the same
  image can't help, so it doesn't burn attempts), and the engine **automatically** runs the
  built-in `image.propose` agent. It reads `missing_tools`, the transcript excerpt where the tool
  was needed and the repo's marker files, and writes an **image proposal artifact**.
- `agentline runs show <run>` shows both side by side. Nothing is built yet:

  ```
  run 01J…  ticket-to-eod  PROJ-123  needs-review
    implement#1  needs-review  reason: missing_tools
      missing:   psql (at "psql: command not found" running ./scripts/migrate_test.sh)
                 protoc (at "protoc: not found" running make proto)
      image:     agentline/img-app-3f9c1a2b7d4e (profile: python)
    image proposal  p-01J…  (by image.propose, $0.02)
      profile python → python+pg-client
        + apt: postgresql-client, protobuf-compiler
      reason: tests need psql to load fixtures; `make proto` needs protoc
      new tag: agentline/img-app-<hash>  est. build ~90 s, then cached
    next: agentline approve 01J… --proposal p-01J…   (apply, build, retry implement)
          agentline resolve 01J… --skip | --fail       (decline)
  ```
- `agentline approve <run> --proposal <id>` writes the profile change to the local
  `agentline.toml` only (never to the target repo; decision 9)
  (audited: who, when, the diff), runs `image.build`, then retries the node as `attempt + 1` in the
  new image. Declining leaves the config untouched and resolves the node as chosen.
- Proposals are limited to the profile's declared package sources (apt, the language's package
  manager, pinned release downloads with a checksum). A proposal that needs anything else is
  shown as text only, for a human to write.
- If the proposal agent can't work out the tool (unknown command), the run still shows the
  missing tool, with "no proposal" and the transcript excerpt.
- Why not let a model pick or build freely: an image is code that runs with write access to the
  workspace and the agent's credentials. A model-chosen package list is unreviewed supply chain,
  and it changes between runs.

**Security**: `allow_images` registry allow-list in `agentline.toml`; images pinned to digests
in the ledger; agent containers get egress only to their LLM API (enforced by a docker network +
egress proxy in Phase 3); no docker socket inside containers.

*As built*: the egress proxy was never built. Containers use bridge networking by default (the
`network` option changes it), so an agent can reach any host; the user accepted that until egress
control exists (decision 85). Image digests aren't pinned either: the reference and its source
are recorded.

## 11.2 As built (Phases 3–5)

Detailed as-built notes, contracts and known limits live next to the code's phase docs:
- `docs/phase-3.md` — runners (`python`, `anthropic`, `shell`, `claude-code`), workspaces
  (`local`, `container`), mirrors, image resolution and proposals. Smoke tests: `docs/smoke.md`.
- `docs/phase-4.md` — Jira/GitHub clients, actions and their effect strategies, webhooks,
  poller, residual crash windows per action.
- `docs/phase-5.md` — artifact store, summaries, lineage, large-input references.
- `docs/phase-6.md` — CLI reference, audit model, logs, redaction, safety rules.
- `docs/phase-7.md` — editor architecture, round-trip guarantees, editor/runs API.
- `docs/phase-8a.md`, `docs/phase-8b.md`, `docs/phase-8c.md` — Postgres ledger, Kafka source,
  codex/aider runners + schedules + templates.
- `docs/phase-9.md` — end-to-end acceptance under chaos and its results.
- `docs/real-run.md` — Phase 10: the real Claude Code run kit (`scripts/real_run.sh`).

Branch artifact contract (implement → open_pr): `{name: agentline/<run_id>, sha, base_sha,
repo, mirror, worktree, start_sha, changed, autocommitted, agent?}`; open_pr pushes `sha` from
`mirror` to the repo's configured remote.

## 11.3 AI agents and model sources *(Phase 11)*

`agentline/agents.py` turns a node's `agent`/`model` (after `defaults:`) into a runner and
runner params. Each agent kind declares the provider types it supports. Keys reach agent CLIs
through `env_map` (child var ← host var), so a source's key arrives under the name the CLI
expects, and nothing else from the host env leaks. The `llm` runner calls OpenAI-compatible
`/chat/completions` or the Anthropic Messages API. Settings API: `api/settings.py`; secrets:
`secrets.py`; TOML table edits: `tomlwrite.py`. User guide: `docs/harnesses-and-models.md`.

## 12. Decision log

| # | Decision | By | When |
|---|---|---|---|
| 1 | Spec v1 changes in §9 | user | Phase 0 |
| 2 | Explicit per-node state machine instead of replayed handlers; `flow.step` API | user | Phase 0 |
| 3 | `on_unknown`: `halt` default, `retry`, `ai` advisor (reports to user when a human must decide) | user | Phase 0 |
| 4 | Workspaces clone from a local bare mirror | user | Phase 0 |
| 5 | Deploy/test commands from agentline config (`env.deploy`, `env.test`) | user | Phase 0 |
| 6 | Approvals via CLI only in v0.1 | user | Phase 0 |
| 7 | One generic executor; nodes are config. Agents get `prompt` (file or inline, tiny templating) and optional `output_schema` with one repair attempt | user | Phase 0 |
| 8 | Container images: explicit `image`, repo profile, or deterministic `auto`; layered and cached by content hash; missing tools are shown to the user and `image.propose` drafts the change automatically; applied and built only after `agentline approve` | user | Phase 0 |
| 9 | Approved image proposals are written to local `agentline.toml` only, never to the target repo | user | Phase 0 |
| 10 | `ruamel.yaml` is the one runtime dependency of the pipeline layer (stdlib has no YAML; it gives line/column for errors and comment-preserving round-trip for the editor). Ledger/scheduler stay stdlib-only. Pipelines stay YAML-only (no TOML/JSON pipeline format) | user | Phase 2 |
| 11 | Extension points are `Runner`, `Action`, `Waiter`, `WorkspaceProvider` in `agentline/registry.py`; built-ins register from `agentline/{runners,actions,workspaces}/__init__.py:register()` | agent | Phase 2 |
| 12 | `github.open_pr` is find-or-create by branch inside `do()`, so loop iterations reuse the PR (the effect guard alone is per iteration) | agent | Phase 2 |
| 13 | Phase 3 as-built choices P3-1…P3-13 (mirror config, worktree lifecycle, `base_sha` = merge-base, auto-commit leftovers, container hardening `--cap-drop ALL` / no-new-privileges, env hygiene, prices from config only, Claude Code budget flag, `RunnerError` carries cost, missing-tool detection, Dockerfile-hashed image tags, deterministic image proposer) — see `docs/phase-3.md` | agent | Phase 3 |
| 14 | Phase 4 as-built choices 1–10 (`commented` reviews ignored, attempt-scoped command effects, shell-quoted substitution, PR carries `head_sha`, `webhook_deliveries` table, 409 on replay, Jira dedupe key, strict trigger filters, unknown env is permanent, failed tests are results) — see `docs/phase-4.md` | agent | Phase 4 |
| 15 | Phase 5 as-built choices 1–8 (result hash = canonical-JSON sha256, `source_execution_id` on links, reference path in the store, field access through references, handoff for all node kinds, summarizer in the write tx, unfenced attachment links, thresholds 256 KiB / 64 KiB / 300 chars) — see `docs/phase-5.md` | agent | Phase 5 |
| 16 | Phase 6 as-built choices 1–10 (argparse CLI, audit rows in the same tx as the action, deploy audit with approver, JSON logs, env-derived redactor applied at write time so successors see redacted values, `approve --proposal` extends `[images.<profile_from>]` with a `.bak`, edit/bash agents need a workspace, test registry hook) — see `docs/phase-6.md` | agent | Phase 6 |
| 17 | Phase 7 as-built choices 1–11 (server-side ruamel edits replayed line-by-line so unchanged bytes survive, comment-loss warnings, no stored layout, top-to-bottom auto layout, `prev_id` renames, pipelines named by relative path, `/api` bearer token, run view draws the current pipeline file, approvals stay CLI-only) — see `docs/phase-7.md` | agent | Phase 7 |
| 18 | Phase 8a: `PostgresLedger` (psycopg 3, optional `postgres` extra): `claim_next` uses `FOR UPDATE SKIP LOCKED` for concurrent multi-host claims; other transactions serialize per schema with `pg_advisory_xact_lock` at READ COMMITTED (no SERIALIZABLE replays); ledger contract suite for every backend; `ledger_from_url`. **Not yet run against a real Postgres** — see `docs/phase-8a.md` | agent | Phase 8 |
| 19 | Phase 8b: `KafkaSource` — run identity `kafka:<topic>:<partition>:<offset>` as dedupe key; manual, contiguous commits only after terminal runs; pause on full window or a waiting/needs-review head; poison messages become dead runs. Real adapter via optional `kafka` extra — see `docs/phase-8b.md` | agent | Phase 8 |
| 20 | Phase 8c: `codex` and `aider` runners (coarse tool mapping, documented), cron schedules with dedupe key `schedule:<pipeline>:<UTC time>` and `catchup: latest|all|none`, templates `ticket-to-eod`, `nightly-tests`, `pr-review` — see `docs/phase-8c.md` | agent | Phase 8 |
| 21 | Phase 9: v0.1 acceptance runs real processes (`agentline serve`, CLI approvals, signed webhooks with redelivery, real git) with SIGKILL chaos — see `docs/phase-9.md` | agent | Phase 9 |
| 22 | A node with a workspace merges a **dict** output flat over the branch info (`{**finalize(), **output}`); only non-dict output goes under `agent`. Found by the Phase 10 kit: `{{plan.summary}}` broke when `plan` had a workspace | agent | Phase 10 |
| 23 | AI agents (kinds `claude-code`, `codex`, `aider`, `llm`), model sources (`[providers.*]`: `anthropic` with `api-key` or `subscription`, `openai-compatible`) and named models (`[models.*]`) are configured separately in agentline.toml and editable in the GUI; every agent node and the pipeline `defaults:` pick agent and/or model | user | Phase 11 |
| 24 | API keys entered in the GUI go to `$AGENTLINE_HOME/secrets.env` (0600), write-only through the API, loaded at startup without overriding the shell, and fed to the redactor | user | Phase 11 |
| 25 | Routers get `mode: llm`: a model picks one of the edge labels from a prompt (one repair, then the `default` edge) | user | Phase 11 |
| 26 | Claude Code uses its own default model or Anthropic models only (login or API key); OpenRouter and private servers are for `llm` nodes, LLM routers, Codex and Aider | user | Phase 11 |
| 27 | Pipeline defaults are an agent+model pair: a node that picks another agent gets that agent's default model; `runner:` stays as the low-level escape hatch, exclusive with `agent:` | agent | Phase 11 |
| 28 | Phase 11 GUI as-built choices 1–7 (Settings page with presets, Anthropic presets claim the implicit `anthropic` name, router mode switch drops the other mode's fields, agent/runner mutually exclusive in the UI, inherited values shown muted) — see `docs/phase-11-gui.md` | agent | Phase 11 |
| 29 | Approvals: CLI **and the Mac app** (local, token-protected API, audited with `via: app` and the macOS user); decision 6 otherwise stands (no web/remote approvals) | user | Phase 12 |
| 30 | Mac app: native SwiftUI shell that runs the engine as a private child process and reuses the React Flow editor in a WKWebView; keys in the macOS Keychain (`AGENTLINE_KEYS_MANAGED=keychain`); built for the user only (ad-hoc signed) | user | Phase 12 |
| 31 | Trigger `type: poll` (Jira, `issue_assigned`): polls for tickets newly assigned to you since polling was enabled; same dedupe key as the webhook | user | Phase 12 |
| 32 | Phase 12 Mac app as-built choices (see `docs/phase-12-mac.md`), plus: the engine stops itself when `AGENTLINE_PARENT_PID` disappears | agent | Phase 12 |
| 33 | Crash and failure reports are local only (never uploaded), one redacted JSON per distinct problem in `$AGENTLINE_HOME/incidents/`, shared by engine and Mac app; pipeline outcomes (dead/needs-review runs) are not incidents | user | Phase 13 |
| 34 | Phase 13 Mac as-built choices (fingerprint matches the engine's, unclean exits share one record, `engine_exit` keyed by signal/exit code, AppKit-caught exceptions not captured yet: `NSApplicationCrashOnExceptions` off pending the user) — see `docs/phase-13-mac.md` | agent | Phase 13 |
| 35 | Built-in sources/models/agents so a new install only needs keys (`docs/agents-and-models.md`); config overrides by name | user | Phase 14 |
| 36 | Keys can always be set: the Mac app tries the Keychain, then the secrets file; `/api/secrets` works in the app too (secrets file) | user | Phase 14 |
| 37 | Data-flow validation: only router edges, or edges from skippable nodes, can deliver a skip; other incoming edges always bring their source's artifacts | agent | Phase 14 |
| 38 | Editor chrome (Phase 14): floating `+` add-node menu (palette hidden until called), every side panel collapses to a rail (state per panel in localStorage, narrow screens separate), minimap small until hover/focus/tap — see `docs/phase-14-web.md` | user | Phase 14 |
| 39 | Problems become tracked **issues**: one local issue per incident or dead/needs-review run (`$AGENTLINE_HOME/issues/`), statuses open / in progress / fixed / won't fix with notes and history; a fixed issue reopens when its problem recurs. No remote tracker | user | Phase 16 |
| 40 | Agent skills come from GitHub (addyosmani/agent-skills, MIT), fetched on demand into `$AGENTLINE_HOME/skills/`, never during a run; agent nodes pick them with `skills:`; claude-code with a workspace gets `.claude/skills/` (git-excluded), other agents get them in the prompt | user | Phase 16 |
| 41 | Editor assistant: a chat in the pipelines view that runs a configured agent (claude-code default, read-only) and **proposes** settings, keys, skill fetches and whole pipelines as cards; the user applies each. Key values are typed into a password field and never reach the model; invalid pipelines get one repair round | user | Phase 16 |
| 42 | Model sources: a failed Test opens a fix-it sheet (set the key, fix the URL, then retest); a per-source Models… checklist makes only checked models available (nothing imported by default); a Claude subscription source lists the known Claude Code models instead of failing | user | Phase 16 |
| 43 | Pipeline building prefers deterministic script steps (`shell.run` + generated `scripts/`) over AI nodes; agents only where judgment or language is needed. `{{pipeline.dir}}` is a template built-in; `shell.run` gets `AGENTLINE_INPUTS` and `output: json` | user | Phase 17 |
| 44 | First-run setup: a "Connect a model" popup (Mac sheet; web dialog outside the Mac app) while no LLM route works and once on the first run; the pick is the assistant's default (`$AGENTLINE_HOME/setup.json`) | user | Phase 17 |
| 45 | MCP servers are configured once (`[mcp.*]`, secrets as `${KEY}` filled at run time) and shared; an agent node gets only those it lists (`mcp: [...]`), for claude-code (private `--mcp-config` + `--strict-mcp-config`) and codex (`-c mcp_servers.*`) only | user | Phase 17 |
| 46 | Pipeline import/export: a .zip of the pipeline's folder plus a manifest of what it needs and the non-built-in settings tables (never key values); import never overwrites unless asked and can add the missing settings | user | Phase 17 |
| 47 | Chat links: Telegram first (long polling, no public URL), paired to one user with a code. Gates can be approved from it by that user (amends #29; audited `via: telegram:@user`, a second tap for gates before deploy-class steps); `chat.ask` / `chat.send` steps; free text goes to the assistant; keys are never accepted over chat | user | Phase 17 |
| 48 | `for_each` on agent/action nodes: one task per list item (`max_parallel`), recorded in a `tasks` table; a node retry reruns only unfinished tasks. Every execution status change, task event and retry goes to an append-only `journal` table | user | Phase 18 |
| 49 | Run view colors: done green, failed/dead/needs-review red, running yellow, pending/ready/waiting grey, skipped/cancelled greyed out; per-node task counts; retry a single node from the run view, CLI, chat or an assistant proposal | user | Phase 18 |
| 50 | Manual retries count per execution (`detail.retries`) and attempt-named effects use it (`run#r1.1`), so a retry reruns instead of reusing the recorded failure | agent | Phase 18 |
| 51 | Pipeline history is a private git repo in the pipelines folder (`.agentline-history`, separate git dir); every save, assistant apply, import and restore is a commit | user | Phase 18 |
| 52 | Dry run = the real engine with stubbed agents/actions/workspaces/waits, scripted routers, auto-approved gates; an optional input used outside its `{{#x?}}` block is now a validation issue | agent | Phase 18 |
| 53 | Costs: spend by day/pipeline/model/node from the ledger; a monthly budget (`$AGENTLINE_HOME/budget.json`) with one chat alert per threshold (50/80/100%) per month | user | Phase 18 |
| 54 | Own skills in `$AGENTLINE_HOME/skills/local` win over the fetched library on a name clash and work without it | user | Phase 18 |
| 55 | Slack (Socket Mode) and Discord (Gateway) links through a stdlib websocket client; platform adapters translate events into the Telegram-shaped updates the one chat-link loop handles; state per platform, notify settings shared | user | Phase 18 |
| 56 | One UI: the web app (Agentline Studio) is the Mac app's only window; native SwiftUI screens are retired and the app keeps only what a browser can't do behind a WebKit message bridge (Keychain keys, engine restart, app log, folder picker, Finder, crash-report export) | user | Phase 20 |
| 57 | The Chat's agent/model is shown and set explicitly ("Chat uses" in Settings → Agents and Chat apps); "Assistant" is called Chat everywhere in the UI | user | Phase 20 |
| 58 | UI theme "Flight Deck" (dark sci-fi console): one token source `web/src/theme.css` (primitives then semantic), styles use semantic tokens only, gated by the ux-ui kit's `lint_hardcodes.py`; lucide-style inline SVG icons; node kinds by icon + label, not color. See `docs/redesign-proposal.md` | user | Phase 21 |
| 59 | Status colors are set by the design system (replaces #49's green/red/yellow): running cyan with an edge sweep, done mint, failed/dead coral red, needs-review/waiting gates amber (a person must act), pending/ready steel, skipped/cancelled dimmed; always icon + word | user | Phase 21 |
| 60 | Mac app keys live only in `<home>/secrets.env` (0600, folder 0700), which the engine loads itself; the app no longer reads the Keychain, so rebuilds never prompt. Earlier Keychain keys are copied once (one last prompt; Keychain values win over a same-named file value). Trade-off accepted by the user: a private file instead of an encrypted Keychain item | user | Phase 22 |
| 61 | The Chat can do everything the GUI can: every GUI action has a Chat action, proposed as a card and applied through that page's own API; the server checks it against config and LIVE STATE (runs, gates, issues, history, detected MCP servers) with one repair round. MCP servers detected in other tools' configs are imported with secrets split into the secrets file (`${KEY}` in agentline.toml; a key holding another value is never overwritten); `enabled = false` turns a server off for every node | user | Phase 25 |
| 62 | The Chat has user-editable general instructions (`$AGENTLINE_HOME/chat-instructions.md`, defaults written on first use: "When the user asks to build a new pipeline, invoke the `interview-me` skill first") and the skill library: Claude Code runs the Chat in a folder with every skill in `.claude/skills/` plus the `Skill` tool; other agents get the skills the instructions name in the prompt. agentline ships `interview-me` as a built-in skill with the lowest priority (the library and your own skills win) | user | Phase 25 follow-up (27) |
| 63 | Failure triage: every failed run / node (and agent nodes with an empty result) is triaged **automatically** by the Chat's agent as ai-instruction, infrastructure or external, with its reasoning. Fixes stay **proposal cards** the user applies (pipeline edit, a lesson line for the Chat instructions, keys/settings, retry of the node); infrastructure gets a redacted report on the run's local issue and, when `[report] github_repo` + `GITHUB_TOKEN` are set, a GitHub issue card (`github_auto = true` creates it at once). Triage cost **counts against the run's budget_usd**; triage is skipped while it is used up. See `docs/phase-28.md` | user | Phase 28 |
| 64 | Problems shows only what can be fixed in the app (failed runs: pipelines, settings, config, keys). agentline's own problems (crash / error reports, failures triaged as infrastructure) are **app reports**: each crash report is triaged as infrastructure without a model call, with a redacted report and a GitHub issue (card, or automatic with `github_auto`) when `[report]` is set, otherwise "Copy as GitHub issue"; they live on App reports (Logs, Settings → General). The Chat instructions get "report app/engine problems on GitHub with the failure-triage procedure, then remove them from Problems" (default, and appended once to existing files). Amends #39. See `docs/phase-29.md` | user | Phase 29 |
| 65 | GitHub for reports is set up in Settings → General: sign in with the GitHub CLI's browser login (`gh auth login --web`, works with SSO; agentline uses `gh auth token`), or paste a token (wins over the CLI); the repo is picked from detected SSH / HTTPS git remotes or typed, and written to `[report]`; Test checks the repo with the token. SSH keys aren't used to authenticate (the issues API takes tokens only). See `docs/phase-31.md` | user | Phase 31 |
| 66 | Chat images: Claude Code reads attached images itself (copied into its working folder for the turn). For any other Chat agent, a vision model transcribes each image once into structured text (steps, arrows, groups, unclear parts), cached per image; the agent builds from the transcript, shown as a card the user can correct (their edit wins) or have read again. The transcriber is "Images" under Chat uses: `@auto` (Claude Code if installed, else an Anthropic API model with a key), `@claude-code`, or a `[models]` name; its cost counts in the turn | user | Phase 33 |
| 67 | "Agents" (the CLI or loop + model profile a node or the Chat runs on) are **harnesses**, as in VS Code: `[harnesses.*]`, node `harness:`, `defaults: {harness}`, Settings → Harnesses. `llm` is renamed `builtin` ("Built-in": agentline's own harness, needs only an API key). Only the harness meaning is renamed: node `type: agent`, agent skills, runner names and router `mode: llm` stay. Every old name is still read (`[agents]`, `agent:`, `kind`/name `llm`, setup choices, Chat actions, bundles); settings saves rename `[agents.X]` headers in place and the editor renames `agent:` once its value changes. See `docs/phase-35.md` | user | Phase 35 |
| 68 | The Chat on the built-in harness runs a small tool loop on **Pydantic AI** (`pydantic-ai-slim`, `server` extra only; the engine stays stdlib): read-only lookups, a typed reply checked by `check_actions` with one retry, 16k max tokens, a lookup budget, prompted JSON for models without tools. Chosen over writing the loop by hand, over agent frameworks in the engine, and over CLI harnesses (OpenCode, OpenHarness), which need an install; OpenCode stays a candidate harness kind for pipeline nodes | user | Phase 36 |
| 69 | Self-improving Chat: what the built-in harness can't handle (a `cant_do` gap, a loop, actions still invalid after the retry, no usable reply, an unknown action type, a crash) is recorded as an App report (incident kind `chat-harness`) that can become a GitHub issue; repeats share one report. Reports carry the trace only, never the conversation or file contents; user-side errors (keys, rate limits, unreachable server) are not reported | user | Phase 36 |
| 70 | First-run setup asks two things in order: 1. connect a model API (keys checked by listing models, a Claude login, a local server), 2. the harness the Chat uses and its model, from what works now. Each step can be skipped; Settings → General runs it again. It is the web dialog in the browser and the Mac app alike (the native sheet is retired, per #56). Amends #44 | user | Phase 36 |
| 71 | Run review and harness learning (from Harness²): a finished run with signals (review loops, gate rejections, retries, needs-review, empty output, cost/time spike, triage verdicts) is **reviewed automatically** by the Chat's harness (Review button for any run), giving per-node rows with verdicts (prefer "confounded"), edit cards and at most one lesson. Proposed edits to **any agent node** can be **probed**: the node is replayed on its recorded inputs (coding nodes in a remote-less clone at the commit it started from, MCP off, no tokens) and compared with what it did. Lessons go to a **per-pipeline library** (`lessons.md`) that later reviews and the Chat read; prompts change only through applied cards. Adopted edits are **checked on the next runs** (helped / hurt / no effect; hurt offers a revert). Costs count against the run's budget. See `docs/phase-38.md` | user | Phase 38 |
| 72 | No restart after saves: the engine reloads settings and pipelines in place (`agentline.live`) after API writes and file changes. Settings changes apply to runs already going (remaining steps, retries); pipeline structure changes apply to new runs only (runs keep the structure of their `flow_hash`). A broken edit keeps the last good version; triggers follow the reload; secrets-file changes are synced into the environment | user | Phase 42 |
| 73 | Only `builtin` is a built-in harness and the fallback for a node without one; claude-code, codex and aider are presets the user adds (Settings, setup, or "+ Add a harness…" in any picker; Cancel keeps the previous choice). Pickers, including the Chat's, list only configured harnesses. A one-time migration adds the presets an install already used and pins `defaults.harness: claude-code` on pipelines that relied on the old fallback. Amends #67 | user | Phase 44 |
| 74 | Settings show only what is usable (VS Code Language Models style, layout A): one **Models** page replaces Model sources + Models + provider keys: a group per connected source, "Needs attention" for sources the user set up that stopped working, **Add models** (source → connect → choose) as the only place unconnected sources appear, and an **eye** that hides a model from every picker without removing it (`hidden`, an override table for built-ins). The Keys tab goes: keys live with their feature, the rest under General → Other keys. Add harness lists installed programs; missing ones sit collapsed with their install command. A keyless built-in local source (Ollama) counts as connected only while it answers | user | Phase 46 |
| 75 | Chat conversations are kept on the server, one JSON file each under `$AGENTLINE_HOME/assistant/conversations/` (not the ledger: the Chat is not a run; not localStorage: the Mac app loses it), and the Chat panel has a History view. A conversation with no activity (message or Unarchive) for more than 7 days is **archived automatically**: hidden from the main list, still searchable and openable, with Unarchive; one can also be archived by hand. Conversations are never deleted. The Chat can archive/unarchive (`conversation` action) | user | Phase 48 |
| 76 | A pipeline names its **start point** (`start: {node, input, template}`): the node that takes a run's input as **json** (the Start run popup is pre-filled with `template`, by default the `{{payload.…}}` fields the templates read), **text** (`{"text": …}`; an agent start node without `{{payload.text}}` gets it as `## Input`) or **chat**. Chat is a new node type `chat`, the **gatekeeper**: the only start node, on the host (no workspace, no edit/bash), harness + model like an agent. It converses through a **resumable wait** (`ctx.wait(key, resume=True)`: the event moves the execution WAITING → READY to run again instead of becoming its result) until it hands off (`output` must match `output_schema`) or cancels. The conversation is the run's thread (`run_messages` in the ledger; message 0 is the payload). It **stays for the whole run**: the loop in `serve` posts notes for failures, waiting gates and the end, the gatekeeper explains them and answers questions with the run's live state, and proposes approve / reject / retry / cancel as **cards that run only after a Confirm** (audited `via`). Talk to it in the Run view's Conversation and in linked chat apps (reply to its message or `/say`). Turn costs count against the run. See `docs/phase-52.md` | user | Phase 52 |
| 77 | Retrying a cancelled run is refused (a retry used to run its dead steps while it still showed cancelled) | user | Phase 53 |
| 78 | An effect whose call raised is recorded as `failed`, not left as an unknown intent. With neither key nor check, the next attempt asks the **effect advisor** (`agentline.advisor`: the Chat's harness and model) whether repeating it is safe: `retry` redoes it, anything else stops at needs-review. Deploy-class effects never ask (a person decides), `on_unknown: retry` redoes without asking, and no advisor means needs-review. `serve` installs the advisor, which also makes `on_unknown: ai` (#3) work; `AGENTLINE_ADVISOR=off` in tests. Amends #3 | user | Phase 53 |
| 79 | The journal records claims (`claimed -> running`) and cancels too, so it has every execution status change (#48) | user | Phase 53 |
| 80 | `timeout` is refused on gate, wait, router and chat nodes, where it did nothing; it stays on agents and actions | user | Phase 53 |
| 81 | `shell.run`, `env.deploy` and `env.test` commands get only PATH/HOME/locale basics plus the host variables they declare (`with: { pass_env }`, `[environments.<env>] pass_env`), not the whole host environment; `git push` also gets the SSH agent variables | user | Phase 53 |
| 82 | Off localhost the API always needs a bearer token: `serve` on a non-loopback address with none set creates `AGENTLINE_API_TOKEN` in the secrets file (never printed); the web UI asks for it | user | Phase 53 |
| 83 | When several Jira poll pipelines match one assignment, only the first starts a run (the poll dedupe key has no pipeline name, unlike the webhook's): intended | user | Phase 53 |
| 84 | Each task of a `for_each` agent node with a workspace gets its own worktree, branch (`agentline/<run>-task<n>`) and container; operations on one mirror take turns | user | Phase 53 |
| 85 | Containers keep bridge networking (any host is reachable) until egress control is built; §11.1 says so | user | Phase 53 |

## 13. Open questions

1. Postgres ledger: Phase 8 as planned, or earlier if workers on several hosts become needed?
2. Review loop feedback: pass the whole `review` artifact or only unresolved comments?
3. Should `needs-review` runs notify somewhere (Jira comment) or is `runs list` enough for v0.1?
4. Cost data for Claude Code: trust its reported `total_cost_usd`, or compute from tokens?
