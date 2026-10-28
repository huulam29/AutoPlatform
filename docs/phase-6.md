# Phase 6: operability and safety

Branch `phase-6`. Adds the `agentline` CLI, an audit log, structured (JSON) logs, secret
redaction, one new validator rule, and end-to-end safety tests for budgets and tool permissions.

## Layout

| Path | What |
|---|---|
| `src/agentline/cli/__init__.py` | argparse tree, `main(argv) -> exit code` (`[project.scripts] agentline`) |
| `src/agentline/cli/env.py` | `CliEnv`: db/config/registry/pipelines/App, redacted output |
| `src/agentline/cli/commands.py` | one function per command; `_build_asgi_app()` for `serve` |
| `src/agentline/cli/view.py` | `runs list` / `runs show` / `audit` views (dict for `--json`, text) |
| `src/agentline/cli/proposal.py` | proposal ids, the `[images.<profile>]` TOML section writer |
| `src/agentline/logs.py` | `JsonFormatter`, `TextFormatter`, `RedactFilter`, `setup_logging()` |
| `src/agentline/redact.py` | `Redactor` |
| `tests/cli/`, `tests/safety/` | CLI tests (through `main(argv)`), redaction/budget/tool tests |

## CLI reference

Global options (accepted before or after the subcommand):

| Option | Default |
|---|---|
| `--db PATH` | `$AGENTLINE_DB`, else `$AGENTLINE_HOME/agentline.db` (`AGENTLINE_HOME` = `~/.agentline`) |
| `--config PATH` | `$AGENTLINE_CONFIG`, else `./agentline.toml` (loaded with `Config.load`) |
| `--pipelines DIR` | `pipelines_dir` in the config (relative to the config file), else `./pipelines` |
| `--log-format text\|json` | `text` (logs go to stderr) |
| `--log-level LEVEL` | `INFO` |

Every `*.yaml` / `*.yml` in the pipelines dir is loaded with `default_registry(config)`. An
invalid file is skipped with a warning on stderr (`agentline validate` shows why). The actor
for approvals and audit rows is `$AGENTLINE_USER`, else `$USER`.

Exit codes: `0` ok, `1` error (message on stderr as `error: ...`) or a run that ended dead /
cancelled under `--wait`, `2` `--wait` timed out, `130` interrupted.

```bash
# check pipelines: every issue as file:line:col: path: message (hint: ...); exit 1 if any
agentline validate pipelines/*.yaml
agentline validate -q pipelines/ticket.yaml          # only issues

# submit by name (from --pipelines) or by path; --wait runs an in-process worker + poller
agentline run ticket-to-eod --payload '{"issue_key": "PROJ-1"}' --dedupe-key jira:PROJ-1
agentline run ./pipelines/ticket.yaml --payload-file payload.json --wait --timeout 600

# long-running processes (graceful on SIGINT/SIGTERM)
agentline worker --concurrency 4 --lease-ttl 30
agentline worker --until-idle                         # drain and exit (cron, tests)
agentline serve --host 127.0.0.1 --port 8080          # worker + poller + webhooks (needs [server])

# runs
agentline runs list [--status needs-review] [--limit 50] [--json]
agentline runs show <run> [--json]
agentline runs retry <run>                            # dead executions get fresh attempts
agentline runs cancel <run>

# human decisions (CLI only, decision 6), all audited
agentline approve <run> [--node approve] [--comment "ship it"]
agentline reject  <run> [--node approve] [--comment "not today"]
agentline approve <run> --proposal p-<execution>-<hash8>     # apply image proposal, build, retry
agentline resolve <run> [--node N] --action retry|skip|fail|done [--result JSON] \
                        [--effect redo|done] [--effect-result JSON]

agentline mirror app                                  # ensure/fetch ~/.agentline/mirrors/app.git
agentline audit [--run <run>] [--limit 200] [--json]
```

`--wait` returns when the run is `done`, `dead`, `cancelled`, `waiting` or `needs-review`, and
prints the `next:` commands. `resolve` and `approve` pick the only matching execution; with
several, name one with `--node`.

### `runs show`

```text
run r01…  missing  needs-review
  cost $0.01   created 2026-10-19 00:09:00
executions
  implement#1      needs-review  attempt 1  $0.01  reason: missing_tools
      missing:  psql
      image:    agentline/img-app-3f9c1a2b7d4e (profile: python)
    image proposal  p-x01…-d0d8ecce  (by deterministic)
      profile python → python+psql
        + apt: postgresql-client
      reason: `psql` is provided by apt package postgresql-client
effects
  …  (name, status, key, advice)
lineage
  implement#1      wrote transcript.first: bash: line 1: psql: command not found (1 line)
audit
  …
next: agentline approve r01… --proposal p-x01…-d0d8ecce   (apply, build, retry implement#1)
      agentline resolve r01… --node implement --action skip|fail   (decline)
```

Per execution it shows key, status, attempt, reclaims, cost, reason/error and a `detail`
excerpt: missing tools and the image proposal, gate prompt and approvers, wait key and waiter,
unknown effect and advisor advice, budget scope/spent/limit, loop edge, image used. `--json`
returns the same as one object: `run`, `executions`, `effects`, `lineage` (reads/writes with
summaries, from `App.lineage`), `audit`, `next`.

### `approve --proposal`

A proposal id is `p-<execution id>-<sha256(canonical proposal JSON)[:8]>`, so it is stable and
changes if the proposal changes. For a `needs-review` execution with reason `missing_tools`:

1. **Write the local `agentline.toml` only** (decision 9). The table `[images.<profile_from>]`
   gets the proposal's apt packages added (an existing table keeps its other keys; a new table
   gets the resolved built-in packages plus the new ones). Using `profile_from` (not
   `profile_to`) means `image: auto` and repo `image_profile` both resolve to the changed
   profile, so the retry really gets a new image (the tag is a hash of the Dockerfile).
   The writer replaces the table's body (its header line stays) or appends the table at the
   end; every byte outside that body is unchanged, comments before the next header included.
   The new text is parsed back with `tomllib` and must equal the old config with only that
   table changed, otherwise nothing is written. The old file is copied to `agentline.toml.bak`
   and the new one is written atomically (temp file + `os.replace`).
2. **Audit** an `approve_proposal` row with the unified diff, the config path and the backup.
3. **Build** the image (`images.build_image`, same tag the container provider will compute).
   If docker is missing or the daemon is down, it says so and goes on; the worker builds the
   image on demand when the node retries.
4. **Resolve** the execution with `retry` (audited as `resolve`, with the build outcome).

Refused (nothing written): a proposal with no packages (unknown tools only), a proposal on the
`base` profile (no toolchain profile to extend; write it by hand), a table defined inline or
with dotted keys.

### Test registry hook

`--registry module:function` (hidden) or `$AGENTLINE_REGISTRY=module:function` replaces
`default_registry(config)` with `function(config) -> Registry`. The CLI tests use
`AGENTLINE_REGISTRY=tests.cli.fakes:registry` (built-ins + a scripted `fake` runner, a
`fakebox` workspace and a `fake.flaky` action).

## Audit model

Table `audit_log (id, at, actor, action, run_id, node_id, detail_json)`, created with
`CREATE TABLE IF NOT EXISTS` (old ledgers get it on open). `LedgerTx.add_audit(actor, action,
run_id, node_id, detail, at)` / `list_audit(run_id=None, limit)` (oldest first). Append-only:
nothing updates or deletes rows.

| action | written by | detail |
|---|---|---|
| `approve` / `reject` | `App.approve/reject` | gate key, execution id, comment |
| `resolve` | `App.resolve` | action, execution, node, reason, result / effect decision, extras |
| `cancel` | `App.cancel` | previous run status |
| `retry` | `App.retry` | number of executions put back to ready |
| `approve_proposal` | CLI | proposal id, diff, config path, backup, profile, packages |
| `deploy` | `env.deploy` | env, sha, branch, exit code, `approved_by`, gate, effect, iteration |

- `App.approve/reject/resolve/cancel/retry` take an optional `actor` (default `AGENTLINE_USER`
  / `USER`) and write the audit row **in the same transaction** as the state change, so there
  is never an approval without its audit row or the reverse. Approve no longer goes through
  `deliver_event` in a separate transaction; a failed approver check writes nothing.
- **Deploys.** `env.deploy` writes one `deploy` row per deploy effect, also when the command
  exits non-zero, with actor `agentline:<worker id>`. `approved_by` is the `by` of the run's most
  recently finished gate (from its result). This answers "which commit was deployed by whose
  approval": `agentline audit --json | jq '.[] | select(.action=="deploy")'`. A re-run that
  reuses an already committed deploy effect doesn't write a second row.
- Audit details are redacted like everything else.

## Log format

`setup_logging(format, level, redactor=None, stream=None)` installs one handler on the root
logger (replacing one it installed earlier) with a `RedactFilter`. The CLI calls it for every
command (stderr). Records carry context when logged with `extra=`; the worker and scheduler now
pass `run_id`, `node_id`, `iteration`, `execution_id` (and `worker_id` in the worker), and
step code already does through `ctx.log`.

```text
2026-10-19 00:09:00,469 INFO    agentline.scheduler: plan#1 done [r01a15… plan#1]
```

```json
{"ts": "2026-10-19T07:09:00.469Z", "level": "WARNING", "logger": "agentline.scheduler",
 "msg": "check#1 needs review: manual_check", "run_id": "r01…", "node_id": "check",
 "iteration": 1, "execution_id": "x01…"}
```

One JSON object per line; `exc` holds a formatted (redacted) traceback. New scheduler log
lines: `<key> done` (info), `<key> failed|dead: <error>` (warning), `<key> needs review:
<reason>` (warning).

## Redaction

`Redactor` collects secret values and replaces each with `[REDACTED:<NAME>]`:
- from env vars named `*_TOKEN`, `*_SECRET`, `*_KEY`, `*_PASSWORD`, plus `ANTHROPIC_API_KEY`,
  `GITHUB_TOKEN`, `JIRA_API_TOKEN` (case-insensitive), ignoring values under 8 characters;
- plus `add(name, value)`.

All values are matched by one compiled alternation (longest first), rebuilt only when the set
changes; an empty redactor returns its input untouched.

Where it applies (`App(redactor=...)`, default `Redactor.from_env()` at App creation, held as
`app.redactor` and `scheduler.redactor`):
- `Scheduler.complete` results and details (walks JSON, keys too), `fail` / `needs_review` /
  `reject` errors, `needs_review` / `park` details, `submit` payloads, `deliver_event`
  payloads, resolved effect results;
- effect records written by `ctx.effect` (`result`);
- `ctx.attach` (transcripts, diffs) before hashing and storing;
- audit details; logs (`RedactFilter`); all CLI output.

So results reaching successor nodes are redacted too: a step that needs a secret reads it from
the environment, not from an upstream artifact.

**Limits.** Only literal values are caught: a secret that was base64- or URL-encoded, split
across lines or chunks, hashed, or partially printed is not. Secrets not in the environment
at App creation (and not `add()`ed) are not known. Values under 8 characters are ignored. A
`Scheduler` built directly (not through `App`) has an empty redactor. Run ids, node ids and
correlation keys are not redacted. Existing rows written before Phase 6 are not rewritten.

## New validator rule

An agent node whose `allowed_tools` include `edit` or `bash` must have a `workspace`;
otherwise its edits would land in the agentline host's working directory:

```text
p.yaml:4:1: nodes.a.allowed_tools: agent with edit tools has no workspace, so its edits would
land in the agentline host's working directory (hint: add workspace: { type: container, repo:
<name> } (or type: local), or drop edit/bash from allowed_tools)
```

`examples/ticket-to-eod` and the existing tests already comply.

## Tests

- `tests/cli/test_flow.py`: run → `--wait` (parks at the gate) → `approve` → `worker
  --until-idle` → done, with `approve` and `deploy` audit rows (`approved_by`, sha, env, exit
  code); dedupe; run by path / payload file; reject/retry/cancel audited; approver check writes
  no audit; exit codes (dead 1, timeout 2); global options after the subcommand.
- `tests/cli/test_proposal.py`: TOML writer (replace keeps other bytes, append, quoting,
  header on the last line, sub-tables, refusals), `approve --proposal` with docker unavailable
  and with a fake docker (built tag equals what `resolve_image` computes afterwards).
- `tests/cli/test_resolve.py`: done with result, skip, fail, retry, argument errors, an unknown
  effect resolved with `--effect done --effect-result`.
- `tests/cli/test_validate_logs.py`: validate output (positions, hints, deploy without gate),
  invalid pipelines skipped, JSON log lines with run context, text format + redaction filter.
- `tests/cli/test_serve.py`: `_build_asgi_app` serves `/healthz`, the missing-extra error,
  serve with a stand-in uvicorn (uvicorn is not a dev dependency).
- `tests/safety/test_redaction.py`: a fake `AGENTLINE_TEST_TOKEN` in the payload, an agent's
  output, transcript, log line and error, and an approval comment; the raw bytes of
  `agentline.db`, `-wal`, `-shm` and every artifact file, plus all stdout/stderr, never contain
  it. Redactor rules, including the base64 limit.
- `tests/safety/test_budgets_tools.py`: run and node budgets through YAML (needs-review
  `budget_exceeded`, no retries burned, successors don't run); the `anthropic` runner refuses at
  0 remaining (run and node budget) without calling the API; `allowed_tools` reach the fake
  `claude` argv and the default is read-only; edit/bash without a workspace rejected by
  `validate` and `run`.

281 passed, 2 skipped (smoke) on this branch.

## Known limits

- A running `agentline worker` keeps the config it started with; after `approve --proposal`
  restart workers so the retry resolves the new image (the CLI says so).
- The TOML writer drops comments inside the replaced table body; multi-line strings
  containing lines that look like table headers are not supported.
- `serve` hands SIGINT/SIGTERM to uvicorn during `server.serve()`; ours only set the stop event
  for the worker and poller.
- Gate/wait timeouts are still not enforced (unchanged).

## Decisions (proposals for the user)

1. **Approve/reject route through one transaction** with the audit row (`scheduler.
   deliver_event` / `scheduler.reject` called inside the same tx as `add_audit`).
2. **Proposal writes extend `[images.<profile_from>]`**, not a new `[images.<profile_to>]`
   table, so the retry resolves the changed image without also editing `[repos.*]`.
   `profile_to` is only shown. Proposals on `base` are refused.
3. **Proposal id = `p-<execution id>-<hash8>`**; `resolve retry` clears the execution's
   detail, so an applied proposal id no longer resolves (a second approve is refused).
4. **`approve --proposal` resolves with `retry` (same attempt number)**, as `resolve retry`
   does, rather than `attempt + 1` as §11.1 says; missing tools don't use up attempts.
5. **Deploy audit actor is `agentline:<worker id>`**, the approver goes in `approved_by`; one
   row per deploy effect (`deploy#<attempt>`), including failed deploys.
6. **Redaction happens at write time in the scheduler/context**, not in the ledger, so the
   ledger stays a dumb store; values passed to successors are redacted too.
7. **Root-logger handler** for `setup_logging`, so uvicorn and library logs get the same
   format and redaction.
8. **Exit codes**: `--wait` returns 0 for done/waiting/needs-review, 1 for dead/cancelled,
   2 on timeout.
9. **`worker --until-idle`** is a public flag (drain and exit), useful for cron and tests.
10. **Registry override** via hidden `--registry` / `$AGENTLINE_REGISTRY` (`module:function`
    taking the config).
