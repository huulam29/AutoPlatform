# Phase 4: actions and triggers

Branch `phase-4`. Adds the real Jira/GitHub clients, the built-in actions, the
`github.review` waiter, the webhook server and the poller fallback. Tests run against
in-process fake HTTP servers and a local bare git repo; no real API is called unless the
env tokens are set.

## Layout

| Path | What |
|---|---|
| `src/agentline/integrations/http.py` | `JsonHttp` on stdlib urllib: JSON, `Link` pagination, `ApiError` (retryable) / `PermanentApiError` (4xx except 408/429), `MissingCredentials`, token redaction |
| `src/agentline/integrations/jira.py` | `JiraClient`: get issue (ADF description flattened to text), add/list comments (paginated) |
| `src/agentline/integrations/github.py` | `GitHubClient`: list PRs by head, create/get PR, list reviews, list review comments |
| `src/agentline/actions/` | `jira.py`, `github.py`, `shell.py`, `env.py`, `_util.py`; `__init__.register()` adds them all |
| `src/agentline/api/webhooks.py` | `create_app(app, pipelines, *, github_secret, jira_secret, github_client=None) -> FastAPI` |
| `src/agentline/deliveries.py` | `webhook_deliveries` table (own module, `CREATE TABLE IF NOT EXISTS`): replay ids and event ids |
| `src/agentline/poller.py` | `run_poller(app, registry, stop, interval)`, `poll_once(...)` |

## Actions and their effect strategies

| Action | Guard | Strategy | On crash between the external write and our commit |
|---|---|---|---|
| `jira.get_ticket` | none | read-only | repeats the GET (harmless) |
| `jira.comment` | `comment` | **check**: body ends with `[agentline:<run>:<node>#<iter>]`; check lists the issue's comments for it | check finds the comment, no duplicate |
| `github.open_pr`: push | `push` | **key** `push:<repo>:<branch>@<sha>`: pushing the same sha again is a no-op | pushes again (no-op) |
| `github.open_pr`: PR | `open_pr` | **check** + **find-or-create** by `head=<owner>:<branch>` inside `do()` (decision 12) | check finds the open PR; iteration 2 reuses it too |
| `github.read_reviews` | none | read-only | repeats |
| `shell.run` | `run#<attempt>` | **none** by default: unknown, so `on_unknown` (default `halt`). `idempotent: true` makes the default `retry` | needs-review (or reruns when idempotent) |
| `env.deploy` (deploy-class) | `deploy#<attempt>` | **check** when the env has `check = "<cmd>"` (prints the deployed sha; compared with the input sha, prefixes allowed); else **none** | with check: no redeploy if the sha is live. Without: needs-review (`halt`; `ai` is rejected) |
| `env.test` | none | safe to repeat | reruns the tests |

Waiter `github.review`: correlation key `github:pr:<owner>/<repo>#<number>` from the `pr`
input (`repo`, `number`). Its `poll(params, inputs, since=None)` returns the latest approved
or changes_requested review of the PR's current `head_sha`. Without a head sha, it counts only
reviews submitted at or after `since`, the wait execution's creation time.

A YAML `effect: { on_unknown: … }` overrides each action's default; `effect: { key: … }` is
passed to the guard as the idempotency key.

Why `run#<attempt>` / `deploy#<attempt>`: the guard has no "failed" state. A command that
*finished* with a non-zero exit is committed with its exit code, then the action raises. The
retry attempt (attempt + 1) gets a fresh effect record and really runs again. A crash
*during* the command is a lease reclaim, which keeps the attempt number, so the same record
is found in `intent` and the outcome is unknown. A human `resolve --effect redo` works as
before.

### Parameters

- `jira.get_ticket`: `key`, defaulting to the `ticket` input's key, then `payload.issue_key`.
  Returns `{key, summary, description, assignee{account_id,name,email}, status, labels,
  project, type, url}`.
- `jira.comment`: `key` (as above), `body`. Returns `{id, key, marker}`.
- `github.open_pr`: input `branch` `{name, sha, base_sha, repo, mirror}` (the Phase 3
  contract). The repo `owner/name` comes from `with: {repo}`, or from `branch.repo` (a slug,
  or a config name resolved through `[repos.<name>] github = "owner/name"`). Also takes
  `title`, `body`, `base` (default: `branch.base`, then the repo's `default_branch`, then
  `main`), `draft`, `remote` (default `[repos.<name>].remote`, then `origin`), `push: false`
  and `force: true` (force-push). The push is skipped when there is no `mirror`. Returns
  `{number, url, branch, repo, head_sha, base, state}`.
- `github.read_reviews`: `repo`, `number`, defaulting to the `pr` input. Returns
  `{state, reviews, comments}`.
- `shell.run`: `cmd` (a string goes through `/bin/sh`, a list is exec'd), `cwd`, `env`
  (values to set), `pass_env` (host variables it may see, by name), `timeout`, `idempotent`,
  `permanent`. Returns `{exit_code, stdout_tail, stderr_tail}`. Since decision 81 the command
  sees only PATH/HOME/locale basics plus `pass_env`, not the whole host environment.
- `env.deploy` / `env.test`: `env`, `timeout`. They read `[environments.<env>]`:
  - `deploy`, `test`, and the optional `check`, `cwd` (relative to agentline.toml),
    `timeout` / `test_timeout` and `pass_env` (host variables the commands may see; nothing
    else from the host env reaches them, decision 81).
  - `{branch}`, `{sha}` and `{env}` are substituted **shell-quoted**, because branch names
    come from agents.
  - cwd: the env's `cwd`, else a temporary detached worktree of the sha taken from
    `branch.mirror` (removed afterwards), else the process cwd.
  - Returns: deploy `{env, sha, branch, exit_code, at, output_tail}` (plus
    `verified_by: "check"` when the check found the deploy); test `{env, sha, passed,
    exit_code, output_tail}`. A failing test is a result, not an error.

## Webhooks

| Endpoint | Auth | Maps to | Response |
|---|---|---|---|
| `POST /webhooks/github` | `X-Hub-Signature-256` HMAC-SHA256 of the raw body, `GITHUB_WEBHOOK_SECRET`, `hmac.compare_digest` | `pull_request_review` + `action: submitted` + state `approved`/`changes_requested` → `deliver_event("github:pr:<owner>/<repo>#<n>", {state, comments, reviewer, commit_id, review_id, submitted_at, event_id, source})` | 202 `{status: accepted, correlation_key, run_ids}`; 401 bad/missing signature; 409 replayed `X-GitHub-Delivery`; 200 `ignored` (other events, `commented`, `dismissed`, other actions); 200 `duplicate` (the poller already delivered this review); `ping` → 200 `pong` |
| `POST /webhooks/jira` | `X-Agentline-Secret` header or `?secret=`, `JIRA_WEBHOOK_SECRET`, constant-time | `jira:issue_updated` whose changelog changes `assignee`, or `issue_event_type_name: issue_assigned` (or `webhookEvent: issue_assigned`) → submit every pipeline with `trigger: {type: webhook, source: jira, event: issue_assigned}` whose `filter` matches | 202 `{status: accepted, run_ids, runs}`; 200 `duplicate` (dedupe key seen); 200 `ignored`; 401 |
| `GET /healthz` | none | | `{status: ok}` |

- **`commented` reviews don't resume the wait** (proposal 1). They are ignored.
- GitHub review `comments`: the review body, plus the review's line comments as
  `path:line: body` when a GitHub token is available (fetched from the API). Without a
  token, only the body is used.
- Jira payload `{"issue_key", "assignee", "event": "issue_assigned"}`. `assignee` is the
  accountId, else the name, else the email.
- Jira dedupe key: `jira:<issue>:assigned:<assignee>:<changelog id or timestamp>`. When
  more than one pipeline listens for Jira assignments, `:<pipeline>` is appended so each
  gets its own run (dedupe keys are globally unique in the ledger).
- Jira `filter` keys: `project` (from `fields.project.key`, else the key prefix),
  `assignee` (matches accountId, name, email or display name), `issue_type`, `labels`
  (any). A value can be a string or a list. Unknown keys are rejected by `create_app`.
- Unassignments, issue creation and other updates → `ignored`.
- An endpoint whose secret is unset rejects every request (401).
- Replay ids and review event ids go in `webhook_deliveries`, written in **the same
  transaction** as `deliver_event`. A failed delivery leaves no claim, so GitHub's retry
  still works.

Running it:

```python
import uvicorn

from agentline import App, SqliteLedger
from agentline.api import create_app
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.pipeline import load_pipeline

config = Config.load()
registry = default_registry(config)
app = App(SqliteLedger("agentline.db"))
pipelines = [load_pipeline("examples/ticket-to-eod/pipeline.yaml", registry, config)]
uvicorn.run(create_app(app, pipelines), host="127.0.0.1", port=8080)
```

Workers and the poller run as separate processes or tasks on the same ledger:
`asyncio.create_task(run_poller(app, registry, stop, interval=30))`.

## Poller

`run_poller` scans `waiting` executions whose detail has `waiter` and `poll_every`. Due means
`poll_every` seconds since the last poll, or since the execution started. When due, it calls
the waiter's `poll()` with the execution's resolved inputs, and delivers a non-None result as
the event on the execution's correlation key. Poll errors are logged and retried on the next
interval. A payload with an `event_id` (reviews: `github-review:<repo>#<n>:<review id>`) is
delivered at most once across the webhook and the poller. Without that, a late webhook for a
review the poller already delivered could resume the **next** iteration's wait. The poll
schedule is kept in memory, so a restarted poller polls every due wait once right away.

## Env vars

| Var | Used by |
|---|---|
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | `JiraClient` (jira.* actions) |
| `GITHUB_TOKEN`, `GITHUB_API_URL` (default `https://api.github.com`) | `GitHubClient` (github.* actions, waiter poll, webhook comment fetch) |
| `GITHUB_WEBHOOK_SECRET`, `JIRA_WEBHOOK_SECRET` | `create_app` defaults |

Clients are built when an action runs, so registering actions never needs tokens. A missing
token raises `MissingCredentials`, a permanent error that names the variable. Token values
(and the Jira basic-auth string) are redacted from every `ApiError` message. Shell, deploy
and test output tails also have the values of these env vars scrubbed. `git push` uses the
host's own git credentials (ssh agent or credential helper), never a token from agentline.

## Residual crash windows (honest version)

- **jira.comment**: safe after a crash. One exception: Jira's comment list could lag a
  just-created comment. We have not seen this; the list is read from the issue directly,
  not from search. Someone editing the marker out of a comment defeats the check.
- **github.open_pr push**: the key is per sha. If the branch was force-moved on the remote
  between the crash and the retry, a non-force push fails loudly. It never overwrites
  silently.
- **github.open_pr PR**: "list PRs with head=" is a direct, consistent lookup. If someone
  **closes** the PR between creation and the retry, the check (open PRs only) finds nothing,
  and a second PR is opened. find-or-create can also race a human opening a PR for the same
  branch at the same moment; GitHub then answers 422 and `do()` looks the PR up again and
  reuses it.
- **shell.run**: no key and no check, so a crash mid-command is unknown → `halt`. A command
  that timed out is killed (whole process group) and counts as finished-with-failure, so the
  retry attempt runs it again, even though the killed run may have done part of its work.
- **env.deploy without `check`**: a crash mid-deploy → needs-review, always (deploy-class, no
  `ai`). With a `check`: the check runs *after* the crash, and a deploy that is still
  rolling out may not yet print the new sha. Then the guard deploys again, which is
  acceptable only if the deploy script is idempotent for the same sha. Write `check` to
  report what is actually live.
- **env.test**: unguarded, so tests may run twice after a crash.
- **Webhook → wait**: an event is stored before delivery. A review that arrives **before the
  wait execution exists** (between `open_pr` committing and `await_review` being created) is
  stored but not consumed, per the Phase 1 rule "events received after the execution was
  created". Only the poller (`poll_every`) recovers this. Use `poll_every` on review waits.
- **Poller vs webhook**: both can deliver; `event_id` makes this at-most-once per review.

## Known limits

- The review decision is "latest decisive review", not a per-reviewer rollup: two reviewers
  disagreeing → the last one wins. Branch protection and required reviewers are not consulted.
- No audit-log row for deploys yet: the ledger protocol has no audit API. The deploy target
  is recorded in the execution's `detail.deploy` (Phase 6).
- `github.open_pr` targets same-repo branches only (`head=<owner>:<branch>`); forks are not
  supported.
- GitHub App auth and Jira OAuth are not supported (token / basic auth only).
- Jira has no hidden comment text, so the marker is a visible last line.
- `webhook_deliveries` needs the SQLite ledger (it uses the connection inside `tx()`); a
  Postgres ledger must provide the same table.
- The poller's SQLite fast path queries the `executions` table directly. Other ledgers fall
  back to `list_runs` + `list_executions`.
- `starlette.testclient` emits a deprecation warning recommending `httpx2`. It's harmless; the
  dev group pins `httpx`.
- `demo/pipeline/worker.py` still uses its own fakes. Its fake server has a different API
  from GitHub's, so switching it to the real actions isn't trivial.

## Decisions taken (proposals for the decision log)

1. **`commented` reviews are ignored** by the webhook and the poller. Only `approved` and
   `changes_requested` resume a `github.review` wait. A comment isn't a decision, and the
   router has no edge for it.
2. **Effect names carry the attempt** for command effects (`run#<attempt>`,
   `deploy#<attempt>`). A finished-but-failed command is retried as a new attempt. A crash
   during one (same attempt) is unknown.
3. **Command substitution is shell-quoted** (`{branch}`, `{sha}`, `{env}`): branch names are
   agent output.
4. **The PR artifact carries `head_sha`** (the input sha, updated each iteration). The review
   waiter polls only reviews of that commit, so a loop's earlier review is never re-delivered.
5. **Webhook replay ids and review event ids** live in their own `webhook_deliveries` table.
   They are claimed in the same transaction as the event; review ids dedupe webhook vs poller.
6. **Replayed GitHub delivery → 409**, unknown/irrelevant events → 200 `ignored`, bad auth →
   401, unset secret → reject everything.
7. **Jira dedupe key gets a `:<pipeline>` suffix** only when several pipelines listen for
   Jira assignments, because ledger dedupe keys are global.
8. **Unknown `trigger.filter` keys fail at `create_app`**, not silently at match time.
9. **Unknown environments fail permanently** (no retries).
10. **`env.test` failures are results** (`passed: false`), not errors, so a pipeline can
    route on them.
