# Phase 8c: Codex and Aider runners, scheduled triggers, pipeline templates

Status: built on branch `phase-8c`; not merged.

## What was built

| Area | Where | What it does |
|---|---|---|
| `codex` runner | `runners/codex.py` | `codex exec --json --sandbox <mode> [--model M] -- <prompt>`, in the workspace or locally. Parses the JSONL events. |
| `aider` runner | `runners/aider.py` | `aider --message <prompt> --yes-always --no-auto-commits ...`, in the workspace or locally. Parses aider's cost lines. |
| Shared CLI plumbing | `runners/agent_cli.py` | `render_args` (user `args:` with placeholders), `exec_cli` (workspace or local, env hygiene, missing binary rules). |
| Cron and schedule loop | `schedules.py` | 5-field cron parser, `Schedule.next_after/prev_before` in UTC or a `zoneinfo` timezone, and `run_schedules(app, pipelines, stop, clock)`. |
| Trigger fields | `pipeline/spec.py`, `pipeline/validate.py` | `trigger.timezone`, `trigger.catchup`. The validator reports a bad cron or timezone with its line. |
| Templates | `templates/` (package data) | `ticket-to-eod`, `nightly-tests`, `pr-review`. |
| CLI | `cli/commands.py`, `cli/__init__.py` | `agentline new`, `agentline templates`, `agentline schedules`. `worker` and `serve` run the schedule loop next to the poller. |

Both runners register in `runners/__init__.py:register()`. `codex` gets the `[pricing]` table
the way `anthropic` does.

## Runners

Both follow the `claude-code` contract (docs/phase-3.md):
- The binary comes from `with: { bin: }`, else `$AGENTLINE_CODEX_BIN` / `$AGENTLINE_AIDER_BIN`,
  else `codex` / `aider`.
- They run through `workspace.exec` when there is a workspace (in the container), else locally.
- **Env hygiene.** Only declared env vars get through (`with: { env: [...] }`). The default is
  `[OPENAI_API_KEY]` for codex and `[OPENAI_API_KEY, ANTHROPIC_API_KEY]` for aider. Locally
  the CLI also sees PATH, HOME and the locale basics. With HOME set, codex can still use a
  `codex login` session stored in `~/.codex`.
- **Missing tools.**
  - A binary missing locally raises `PermanentError`.
  - A binary missing inside the workspace (exit 127 with "not found") returns `missing_tools`.
  - `MISSING_TOOL:` and `command not found` lines also give `missing_tools`. For codex this
    covers the final message and the output of commands that exited 126 or 127. For aider it
    covers its stdout.
- **Failures.** A failure raises `RunnerError` with the cost and transcript: a non-zero exit,
  a codex `turn.failed` or `error` event, or an aider litellm error line. Aider can exit 0
  after an API error. Output that can't be parsed (codex printed no JSON events) is also a
  retryable `RunnerError`.
- **Budget.** `budget_usd == 0` refuses the call with `BudgetExceeded`. An overrun is
  reported in `meta.budget_exceeded`, and the engine's `spend()` sends the node to
  needs-review (as P3-9).
- **Output.** The JSON (or trailing ```` ```json ```` block) in the final answer, else the
  text.
- **`args:`** replaces the default flags for CLI versions whose flags differ. The
  placeholders are `{prompt}`, `{model}` and, for codex, `{sandbox}`. An arg that is exactly
  an empty placeholder is dropped together with the `-flag` before it. Without `{prompt}`,
  codex appends the prompt and aider appends `--message <prompt>`. `extra_args:` appends to
  the defaults instead of replacing them.

### Codex: `allowed_tools` mapping, and what it can and can't enforce

Codex has no per-tool allowlist. It only has an OS sandbox, so the mapping is coarse.

| agentline tools | Codex flags | Enforced | Not enforced |
|---|---|---|---|
| no `edit` or `bash` | `--sandbox read-only` | No file writes, no network (OS sandbox) | The agent can still **run shell commands** (read-only ones). `read` can't be removed. |
| `edit` or `bash` | `--sandbox workspace-write` | Writes only in the working directory and temp dirs. Network is off. | `edit` without `bash`: Codex edits by running commands, so both mean the same sandbox. |
| `web` | `-c tools.web_search=true` | Codex's own web search tool is turned on | General network access for commands stays off |
| `with: sandbox:` | as given | Overrides the mapping (for example `danger-full-access` in a container without Landlock/seccomp, relying on the container) | |

`codex exec` never asks for approval, so there is no approval flag. `--skip-git-repo-check` is
added only without a workspace, because the host cwd may not be a repo. `max_turns` has no
Codex equivalent and isn't enforced. The node's `timeout` is.

**Cost.** Codex reports tokens (`turn.completed.usage`, or legacy `token_count`), not money.
Cost = tokens × price, taken from `with: {input_usd_per_mtok, output_usd_per_mtok}` or
`[pricing.<model>]`. Cached input is billed at the input price, which is an overestimate.
When no price is known, the cost is 0 and `meta.warnings` says so.

### Aider: `allowed_tools` mapping, and what it can and can't enforce

| agentline tools | Aider flags | Enforced | Not enforced |
|---|---|---|---|
| no `edit` | `--chat-mode ask --dry-run` | Ask mode proposes no edits, and `--dry-run` writes no files even if it tries | |
| no `bash` | `--no-suggest-shell-commands` | Aider doesn't propose commands, so it can't run them | With `bash` allowed, whether `--yes-always` actually runs proposed commands depends on the aider version |
| no `web` | `--no-detect-urls` | URLs in the prompt aren't fetched | |
| `read` | none | | Aider always reads the repo (repo map and the files it adds) |

`max_turns` isn't enforced (no aider equivalent). The `timeout` is.

**Commit decision.** The runner passes `--no-auto-commits --no-dirty-commits` by default, for
three reasons:
- The workspace's `finalize()` already commits leftovers (P3-4) as one commit per node
  iteration with agentline's identity.
- The branch artifact's `diff` and `base_sha` don't depend on the number of commits.
- Aider's dirty-commit would sweep pre-existing changes into its own commit.

`auto_commits: true` opts in to aider's per-edit commits.

**Keeping aider's files out of the branch:**
- `--no-gitignore` stops aider from editing the repo's `.gitignore`.
- The chat and input history files go to `/dev/null`.
- In a workspace, the runner first appends `.aider*` to the repo's `info/exclude`. This is
  best effort and idempotent, and `exclude_aider_files: false` turns it off. It keeps
  `.aider.tags.cache.*` out of `finalize()`'s auto-commit. For a worktree that file lives in
  the mirror's common dir, so it applies to every worktree of that mirror. That is harmless.
- `--analytics-disable` is set so `--yes-always` never agrees to analytics.

**Cost.** Parsed from the last `Cost: $x message, $y session.` line (the session total). The
`Tokens:` lines are summed into `meta.usage`. If aider prints no cost, the cost is 0 with a
warning.

## Scheduled triggers

```yaml
trigger:
  type: schedule
  schedule: "0 2 * * *"     # or @hourly, @daily, @weekly, @monthly, @yearly
  timezone: Europe/Berlin   # optional, default UTC (IANA names via zoneinfo)
  catchup: latest           # latest (default) | all | none
```

**Cron.**
- Five fields, each `*`, `n`, `a-b`, lists, `*/n`, `a-b/n` or `a/n`. Month and weekday names
  work, and weekday 7 is Sunday.
- When both day-of-month and day-of-week are restricted, a day matching *either* fires
  (classic cron). A field starting with `*` counts as unrestricted.
- The parser rejects impossible dates such as `0 0 30 2 *`.

**DST.**
- A wall time that doesn't exist (spring forward) fires once, shifted forward by the gap:
  02:30 fires at 03:30.
- A repeated wall time (fall back) fires once, on the first pass, unless the hour field is
  `*`. Then it fires on both passes, so `0 * * * *` stays hourly in real time.
- Fire times are UTC instants, so wall times that collapse onto the same instant fire once.
- `next_after`/`prev_before` jump field by field in wall-clock time. They only search a
  ±3 h window when a UTC-offset change is near.

**Loop.** `run_schedules(app, pipelines, stop, clock=utc_now, *, max_sleep=30, grace=None)`
runs in `agentline worker` and `agentline serve`, next to the poller. It does nothing when
no pipeline has a schedule trigger. Each tick works like this:
- It submits every due fire time with `dedupe_key = schedule:<pipeline>:<UTC
  %Y-%m-%dT%H:%M:%SZ>` and the payload `{"scheduled_for": <same time>, "pipeline": <name>}`.
- The ledger's unique dedupe key makes restarts, overlapping ticks and several processes
  on one ledger fire each time once. Only `get_run_by_dedupe` and `submit` are used, so
  this works on any ledger backend.
- It sleeps until the next fire time, at most `max_sleep`, so clock jumps are noticed.
- A failed submit or tick is logged and retried about 1 s later. The cursor doesn't move,
  so nothing is lost.

**Catch-up.**
- **On start**, the loop walks back from now through at most `LOOKBACK` (1000) fire times
  until one already has a run. The ones after it were missed.
- **While running**, a stall (for example a suspended laptop) gives the fire times since the
  last tick.
- **`catchup` policy** decides which missed times fire:
  - `latest` (default): only the most recent one fires.
  - `all`: every missed time fires, at most 1000.
  - `none`: only times still within the grace period fire (max(60 s, 2 × max_sleep)).
- **A pipeline that never fired** (no run within the lookback) starts fresh. Only a fire
  time within the grace period fires, so deploying a new nightly pipeline at 15:00 doesn't
  fire 02:00 at once. A pipeline whose last run is more than 1000 fire times ago is also
  treated as fresh.

`agentline schedules [--count N] [--json]` lists every schedule pipeline with its next fire
times, in UTC and (for other zones) local time. The worker and serve banners print the next
fire time of each.

## Templates

`agentline templates [--json]` lists them. `agentline new <template> <dest_dir> [--name N]
[--force]` copies a template:
- It writes the pipeline as `<dest_dir>/<name>.yaml`, so `dest_dir` can be the pipelines
  directory itself. Prompts and schemas go next to it with their relative paths.
- It replaces the top-level `name:` line (comments survive) and then validates the result.
- It refuses to overwrite any existing file without `--force`, and writes nothing when it
  refuses.

| Template | Trigger | Shape |
|---|---|---|
| `ticket-to-eod` | Jira webhook | Copy of `examples/ticket-to-eod` with prompts and schema |
| `nightly-tests` | `schedule: "0 2 * * *"` | `shell.run` tests → router → `jira.comment` on `failed`, a no-op on `passed` |
| `pr-review` | manual (`{"repo", "number"}`) | `shell` runner fetches `gh pr diff` → `claude-code` reviews with `allowed_tools: [read]` and an output schema → `shell.run` posts `gh pr comment` |

**`nightly-tests`.** `shell.run` raises on a non-zero exit and has no `allow_failure`, so the
command always exits 0 and reports its verdict: `make test >&2 && printf passed || printf
failed`. The test output goes to stderr, so `stdout_tail` is exactly `passed` or `failed` and
the router switches on it. The failure comment includes `stderr_tail` (the last 4000 chars).
A router needs an edge for every value, so `passed` goes to a no-op `shell.run` node that can
be replaced with a notification. The `cwd:` and Jira `key:` lines are marked "change me".

**`pr-review`.** PR values reach the commands through env vars, never through the command
line. The review agent has only `read` and no workspace, so it reads the diff from its prompt.
Run the worker from a directory you don't mind it reading.

All three pass `agentline validate` as shipped (tested, plus a test per template that `new` +
`validate` succeed). Hatchling's wheel target (`packages = ["src/agentline"]`) already
includes the non-Python files. `uv build --wheel` was checked to contain
`agentline/templates/**`, so `pyproject.toml` is unchanged.

## As-built decisions (proposals for the decision log)

- **P8c-1 Coarse tool mapping for codex and aider.** Neither CLI has a per-tool allowlist.
  Codex maps to its OS sandbox modes, and aider to ask, dry-run, no-shell and no-URL flags.
  The tables above say exactly what is enforced. For real isolation, rely on the container
  workspace.
- **P8c-2 `args:` override.** Flag sets change between CLI versions. `args:` replaces the
  defaults, with `{prompt}`/`{model}`/`{sandbox}` placeholders.
- **P8c-3 Aider doesn't commit by default.** `finalize()` makes one commit per iteration.
  `.aider*` goes into `info/exclude`.
- **P8c-4 Codex cost from tokens × configured price.** No prices are hard-coded (as P3-8).
- **P8c-5 Schedule state lives in the runs table.** The dedupe key is the only record of a
  fire time. There is no new table, so the loop works on any ledger that implements
  `get_run_by_dedupe`.
- **P8c-6 Catch-up defaults to `latest`.** A never-fired pipeline starts fresh, and catch-up
  looks back at most 1000 fire times.
- **P8c-7 DST rules.** A nonexistent wall time is shifted forward. A repeated wall time fires
  on its first pass only, except when the hour field is `*`.
- **P8c-8 `new` writes `<name>.yaml`** (not `pipeline.yaml`), so templates drop straight into
  the pipelines directory.

## Known limits

- The real codex and aider CLIs are not installed here. The runners are tested against fake
  executables (`tests/runners/fake_codex.py`, `fake_aider.py`) that mirror the documented
  output formats. There is no smoke test yet.
- Aider's free-text output can only be parsed for JSON blocks and cost lines. A `MISSING_TOOL`
  line that the model merely *talks about* would park the node, as with claude-code.
- The schedule loop's start-up walk-back makes up to 1000 dedupe lookups per schedule
  pipeline. That is fast on SQLite. On a remote ledger it is up to 1000 round trips at start.
- Schedules are re-read only when the process starts. Editing a pipeline's cron needs a
  worker restart, like any pipeline change.
- `catchup: all` is capped at 1000 fire times.

## Tests

- `tests/runners/test_codex.py`, `test_aider.py` (fake CLIs) cover:
  - argv and tool mapping, `args:` overrides and placeholders;
  - JSON and text results, legacy codex events, cost from prices, the pricing table and the
    unknown-price warning, aider cost parsing;
  - errors with cost and transcript, garbage output, missing tools (agent and in-workspace
    binary), budget refusal and overrun, timeouts, a missing binary, env scrubbing;
  - running inside a workspace, aider's `info/exclude` (idempotent, and really hides
    `.aider.tags.cache`).
- `tests/schedules/test_cron.py` covers:
  - fields, aliases and 15 invalid expressions;
  - next and prev, the leap day, month ends and the dom/dow OR rule;
  - timezones, spring-forward and fall-back (fixed and `*` hours), and next/prev
    consistency around both DST changes.
- `tests/schedules/test_loop.py` (fake clock) covers:
  - due fire times, the payload and the dedupe key;
  - a fresh start, each catch-up policy after downtime, and a stall while running;
  - two loops on one ledger, three concurrent async loops, submit-failure retry and
    `next_fire_times`.
- `tests/schedules/test_spec.py` covers the trigger fields, cron and timezone validation with
  line numbers, and parse errors.
- `tests/cli/test_templates_schedules.py` covers:
  - `templates`, and `new` for every template, followed by validate;
  - name substitution, refusal and `--force`, errors;
  - `schedules` in text and `--json`, `validate` of a bad cron;
  - `worker` running the schedule loop.
